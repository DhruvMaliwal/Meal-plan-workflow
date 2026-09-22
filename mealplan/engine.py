"""Generation engine: pre-filter -> score -> assemble (LLM+Epicure or heuristic) -> validate -> repair -> explain."""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field, asdict
from typing import Callable

from .attributes import AttributeStore, DishAttributes
from .config import load_config
from .history import PlanHistory
from .inventory import Inventory
from .llm import LLM, PlanChoice
from .profiles import HouseProfile
from .quantities import Ledger, dish_needs, missing_essentials, perishable_use_score, to_shelf_units
from .repo import Dish, RecipeRepo
from .rules import DayState, RuleEngine, HERO_PROTEINS

SLOTS = ["Breakfast", "Lunch", "Dinner"]
CUISINE_BUCKETS = {
    "South Indian": "South Indian",
    "North Indian": "North Indian", "Other Indian": "North Indian",
    "Indo-Chinese": "Experimental/Asian", "Asian": "Experimental/Asian", "Continental": "Experimental/Asian",
    "Mediterranean": "Experimental/Asian", "Middle Eastern": "Experimental/Asian", "Mexican": "Experimental/Asian",
    "Fusion/Other": "Experimental/Asian",
}
PROTEIN_RICH = re.compile(r"\b(chicken|fish|egg|paneer|dal|moong|masoor|toor|chana|rajma|chole|lobia|sprouts?|tofu|soya|tempeh|peanut|curd|yogurt|greek|quinoa|oats|besan|sattu)\b", re.I)
GREENS = re.compile(r"\b(spinach|palak|methi|amaranth|broccoli|beans|peas|drumstick|moringa|lettuce|kale|coriander|mint|bok choy|greens|gongura|zucchini|capsicum|bell pepper)\b", re.I)


@dataclass
class Candidate:
    dish: str
    slot: str
    component: str
    score: float
    perishable_score: float
    perishables: list[dict]
    missing: list[str]
    flags: list[str]
    in_last_plan: bool
    attrs: DishAttributes
    cuisine_bucket: str

    def brief(self) -> dict:
        return {"dish": self.dish, "component": self.component, "cuisine": self.attrs.cuisine, "diet": self.attrs.diet,
                "gravy": self.attrs.gravy, "protein": self.attrs.protein_group,
                "est_minutes": self.attrs.est_minutes, "score": round(self.score, 2),
                "perishables_used": [f"{p['ingredient']} {p['need']:g}{p['unit']}" for p in self.perishables if p.get("need")],
                "missing_essentials": self.missing, "in_last_plan": self.in_last_plan,
                "soak_flag": self.attrs.needs_soaking, "flags": [f for f in self.flags if "estimate" not in f]}


@dataclass
class PlannedDish:
    name: str
    component: str
    diet: str
    cuisine: str
    gravy: bool
    protein_group: str
    est_minutes: int | None
    est_minutes_source: str
    slot_eligibility: list[str]
    flags: list[str]
    perishables: list[dict]          # [{ingredient, qty, unit, remaining_after}]
    missing: list[str]
    youtube: str | None
    rationale: str = ""
    in_last_plan: bool = False


@dataclass
class MealSlot:
    day: int                          # 1-based
    date: str | None
    slot: str
    dishes: list[PlannedDish] = field(default_factory=list)
    rationale: str = ""
    warnings: list[str] = field(default_factory=list)
    est_minutes_total: int | None = None
    planned: bool = True                  # False = slot not served by the cook (self-managed)

    def names(self) -> list[str]:
        return [d.name for d in self.dishes]


@dataclass
class Plan:
    house_id: str
    house_name: str
    servings: int
    days: int
    start_date: str | None
    slots: list[MealSlot]
    source: str                        # llm+epicure | llm | heuristic
    tradeoffs: str = ""
    warnings: list[str] = field(default_factory=list)
    repairs: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    llm_calls: list[dict] = field(default_factory=list)

    def get(self, day: int, slot: str) -> MealSlot:
        return next(s for s in self.slots if s.day == day and s.slot == slot)

    def all_dishes(self) -> list[tuple[str, str]]:
        return [(f"D{s.day} {s.slot}", d.name) for s in self.slots for d in s.dishes]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Plan":
        slots = [MealSlot(day=s["day"], date=s.get("date"), slot=s["slot"], rationale=s.get("rationale", ""),
                          warnings=s.get("warnings", []), est_minutes_total=s.get("est_minutes_total"),
                          planned=s.get("planned", True),
                          dishes=[PlannedDish(**pd_) for pd_ in s["dishes"]]) for s in d["slots"]]
        return cls(house_id=d["house_id"], house_name=d["house_name"], servings=d["servings"], days=d["days"],
                   start_date=d.get("start_date"), slots=slots, source=d.get("source", ""), tradeoffs=d.get("tradeoffs", ""),
                   warnings=d.get("warnings", []), repairs=d.get("repairs", []), violations=d.get("violations", []),
                   llm_calls=d.get("llm_calls", []))


class Planner:
    RESERVE_MIN = 15   # placeholder minutes reserved per still-to-pick required dish (heuristic assembler)

    def __init__(self, repo: RecipeRepo, attrs: AttributeStore, profile: HouseProfile, inventory: Inventory,
                 history: PlanHistory | None, llm: LLM, servings: int | None = None, start_date: dt.date | None = None,
                 progress: Callable[[str], None] | None = None):
        self.repo, self.attrs, self.p, self.inv = repo, attrs, profile, inventory
        self.history = history or PlanHistory()
        self.llm = llm
        self.cfg = load_config()
        self.plan_cfg = self.cfg["plan"]
        self.servings = servings or profile.servings
        self.start_date = start_date
        self.rules = RuleEngine(profile, self.history)
        self.rules.start_date = start_date
        self.rules.set_history(self.history, repo.dishes)
        self.planned_slots = [s for s in SLOTS if s in profile.planned_slots] or list(SLOTS)
        self.per_cats = set(self.cfg["perishables"]["categories"])
        self.assumed = {a.lower() for a in self.cfg["perishables"].get("assumed_pantry", [])}
        self.always = {a.lower() for a in profile.always_in_stock}
        self.progress = progress or (lambda m: None)
        self.days = int(self.plan_cfg.get("days", 3))
        self.weights = self.plan_cfg.get("weights", {})
        self.last_dishes = self.history.recent_dishes()
        self.last_heroes = self.history.recent_ingredient_hits(repo)
        self._shelf = inventory.by_canonical()

    # ------------------------------------------------------------- helpers
    def date_for(self, day: int) -> dt.date | None:
        return self.start_date + dt.timedelta(days=day - 1) if self.start_date else None

    def _unplanned(self, day: int, slot: str) -> MealSlot:
        return MealSlot(day=day, date=self.date_for(day).isoformat() if self.date_for(day) else None, slot=slot,
                        rationale="Not planned: this meal is self-managed by the residents (see profile.planned_slots).", planned=False)

    def cook_off(self, day: int) -> bool:
        d = self.date_for(day)
        return bool(d and d.strftime("%A") in self.p.cook.days_off)

    def _components_for(self, spec: str) -> set[str]:
        spec = spec.rstrip("?")
        comps = set()
        for part in spec.split("|"):
            comps.add(part)
        if "staple" in comps:
            comps.add("bread")  # rice or roti both complete a lunch
        return comps

    # ---------------------------------------------------------- scoring
    def score_dish(self, dish: Dish, a: DishAttributes, slot: str, ledger: Ledger, cuisine_counts: dict[str, int],
                   total_so_far: int, flags: list[str]) -> Candidate:
        w = self.weights
        ps, detail = perishable_use_score(dish, ledger, self.servings, self.per_cats)
        missing = missing_essentials(dish, ledger, self.servings, self.assumed, self.always)
        miss_names = [m.canonical for m in missing]
        # perishable-category missing essentials (must be bought fresh) weigh more than dry goods
        order_pen = sum(1.0 if m.category in self.per_cats else 0.35 for m in missing)
        bucket = CUISINE_BUCKETS.get(a.cuisine, "Experimental/Asian")
        target = self.p.cuisine_split_target.get(bucket, 1 / 3) if self.p.cuisine_split_target else 1 / 3
        have = cuisine_counts.get(bucket, 0) / max(1, total_so_far)
        cuisine_fit = max(-0.5, min(1.0, (target - have) * 3)) if total_so_far else target
        names = dish.ingredient_names_lower() + " " + dish.name.lower()
        nutrition = (0.6 if PROTEIN_RICH.search(names) else 0) + (0.4 if GREENS.search(names) else 0)
        if re.search(r"\b(deep.?fried|fried|pakora|bhajiya|kurkuri|vada|puri|bhatura)\b", dish.name, re.I):
            nutrition -= 0.6
        liked = sum(1 for k in self.p.liked_ingredients if re.search(rf"\b{re.escape(k.lower())}\b", names)) * 0.5
        soft_dislike = sum(1 for k in self.p.disliked_ingredients_soft if re.search(rf"\b{re.escape(k.lower().split(' (')[0])}\b", names)) * 0.6
        heroes_repeat = sum(1 for i in dish.ingredients if i.cls == "Hero" and i.canonical in self.last_heroes and i.category in self.per_cats)
        in_last = dish.name in self.last_dishes
        score = (w.get("perishable_use", 1.0) * ps
                 - w.get("order_penalty", 0.6) * order_pen
                 + w.get("cuisine_fit", 0.35) * cuisine_fit
                 + w.get("nutrition_fit", 0.3) * nutrition
                 + w.get("liked_ingredient", 0.25) * liked
                 - 0.3 * soft_dislike
                 - w.get("variety", 0.4) * (heroes_repeat * 0.5 + (1.5 if in_last else 0))
                 + (0.35 if a.source in ("llm", "operator") else 0.0) * min(1.0, a.confidence))
        return Candidate(dish=dish.name, slot=slot, component=a.component, score=score, perishable_score=ps, perishables=detail,
                         missing=miss_names, flags=flags, in_last_plan=in_last, attrs=a, cuisine_bucket=bucket)

    def candidates(self, slot: str, day: int, ledger: Ledger, state: DayState | None = None,
                   cuisine_counts: dict[str, int] | None = None, total_so_far: int = 0,
                   components: set[str] | None = None) -> list[Candidate]:
        out = []
        for name, dish in self.repo.dishes.items():
            a = self.attrs.get(name)
            if components and a.component not in components:
                continue
            if a.component in ("beverage", "dessert", "snack") and not (components and a.component in components):
                continue
            v = self.rules.check_dish(dish, a, slot, day - 1, state)
            if not v.ok:
                continue
            out.append(self.score_dish(dish, a, slot, ledger, cuisine_counts or {}, total_so_far, v.flags))
        out.sort(key=lambda c: c.score, reverse=True)
        return out

    def candidate_summary(self) -> dict[str, dict[str, int]]:
        """Legal candidate counts per slot/component on day 2 (soaking allowed) - for the UI."""
        ledger = Ledger(self.inv)
        summary = {}
        for slot in self.planned_slots:
            cs = self.candidates(slot, 2, ledger)
            by = {}
            for c in cs:
                by[c.component] = by.get(c.component, 0) + 1
            summary[slot] = {"total": len(cs), **dict(sorted(by.items()))}
        return summary

    # ----------------------------------------------------- heuristic build
    def _pick(self, cands: list[Candidate], used: set[str], exclude_heroes: set[str] = frozenset()) -> Candidate | None:
        for c in cands:
            if c.dish in used:
                continue
            d = self.repo.dishes[c.dish]
            heroes = {i.canonical for i in d.ingredients if i.cls == "Hero" and i.category in self.per_cats}
            if heroes and heroes <= exclude_heroes:
                continue
            return c
        return None

    def assemble_heuristic(self, fixed: dict[tuple[int, str], list[str]] | None = None) -> Plan:
        """Greedy assembly honouring every hard rule; `fixed` pins (day, slot) -> dish names."""
        fixed = fixed or {}
        ledger = Ledger(self.inv)
        plan_state = DayState()
        cuisine_counts: dict[str, int] = {}
        total = 0
        slots: list[MealSlot] = []
        one_pot_used = 0
        for day in range(1, self.days + 1):
            day_proteins: set[str] = set()
            day_heroes: set[str] = set()
            for slot in SLOTS:
                if slot not in self.planned_slots:
                    slots.append(self._unplanned(day, slot))
                    continue
                ms = MealSlot(day=day, date=self.date_for(day).isoformat() if self.date_for(day) else None, slot=slot)
                if self.cook_off(day):
                    ms.warnings.append(f"{self.date_for(day).strftime('%A')} is the cook's day off - confirm cover or keep it simple")
                state = DayState(proteins_today=set(day_proteins), gravies_in_meal=0, minutes_in_meal=[],
                                 dishes_in_plan=set(plan_state.dishes_in_plan), cap_counts=dict(plan_state.cap_counts),
                                 reused_from_last=plan_state.reused_from_last)
                chosen: list[Candidate] = []
                if (day, slot) in fixed:
                    for name in fixed[(day, slot)]:
                        d = self.repo.get(name)
                        if not d:
                            ms.warnings.append(f"pinned dish not in repo: {name}")
                            continue
                        a = self.attrs.get(d.name)
                        v = self.rules.check_dish(d, a, slot, day - 1, state)
                        c = self.score_dish(d, a, slot, ledger, cuisine_counts, total, v.flags)
                        if not v.ok:
                            ms.warnings.append(f"pinned {d.name}: {'; '.join(v.reasons)}")
                        chosen.append(c)
                        self._apply(c, state, ledger, f"D{day} {slot}")
                    ms.rationale = "Operator-pinned."
                else:
                    comp_spec = getattr(self.p.meal_composition, slot)
                    allow_one_pot = self.p.meal_composition.allow_one_pot.get(slot, True)
                    all_c = self.candidates(slot, day, ledger, state, cuisine_counts, total)
                    # decide on a one-pot meal
                    one_pot = None
                    if allow_one_pot and slot != "Breakfast" and one_pot_used < 1:
                        op = [c for c in all_c if c.component == "one_pot"]
                        mains = [c for c in all_c if c.component in self._components_for(comp_spec[0])]
                        if op and (not mains or op[0].score > mains[0].score + 0.8):
                            one_pot = self._pick(op, state.dishes_in_plan, day_heroes)
                    if one_pot:
                        chosen.append(one_pot)
                        self._apply(one_pot, state, ledger, f"D{day} {slot}")
                        one_pot_used += 1
                        # light accompaniment
                        acc = self._pick([c for c in self.candidates(slot, day, ledger, state, cuisine_counts, total, {"accompaniment", "salad", "soup"})
                                          if self._pairs(one_pot, c)], state.dishes_in_plan)
                        if acc and acc.score > -0.5:
                            chosen.append(acc)
                            self._apply(acc, state, ledger, f"D{day} {slot}")
                    else:
                        required_specs = [sp for sp in comp_spec if not sp.endswith("?")]
                        for idx, spec in enumerate(comp_spec):
                            optional = spec.endswith("?")
                            comps = self._components_for(spec)
                            # reserve time for the required components still to come (placeholder minutes each)
                            remaining_req = len([sp for sp in comp_spec[idx + 1:] if not sp.endswith("?")])
                            reserve_state = DayState(proteins_today=state.proteins_today, gravies_in_meal=state.gravies_in_meal,
                                                     minutes_in_meal=state.minutes_in_meal + [self.RESERVE_MIN] * remaining_req,
                                                     dishes_in_plan=state.dishes_in_plan, cap_counts=state.cap_counts,
                                                     reused_from_last=state.reused_from_last)
                            cs = [c for c in self.candidates(slot, day, ledger, reserve_state, cuisine_counts, total, comps)
                                  if all(self._pairs(x, c) for x in chosen)]
                            if not cs and not optional and remaining_req:
                                # fall back: ignore the reservation rather than leave the slot empty
                                cs = [c for c in self.candidates(slot, day, ledger, state, cuisine_counts, total, comps)
                                      if all(self._pairs(x, c) for x in chosen)]
                            if slot == "Breakfast" and "breakfast_main" in comps:
                                cs = [c for c in cs if c.component in ("breakfast_main", "one_pot")] or cs
                            pick = self._pick(cs, state.dishes_in_plan, day_heroes if not comps & {"staple", "bread"} else set())
                            if pick is None:
                                if not optional:
                                    ms.warnings.append(f"no legal {spec} candidate left for {slot}")
                                continue
                            if optional and not self._wants_accompaniment(chosen, pick):
                                continue
                            chosen.append(pick)
                            self._apply(pick, state, ledger, f"D{day} {slot}")
                    ms.rationale = self._heuristic_rationale(chosen)
                for c in chosen:
                    d = self.repo.dishes[c.dish]
                    day_proteins |= {c.attrs.protein_group} if c.attrs.protein_group in HERO_PROTEINS else set()
                    day_heroes |= {i.canonical for i in d.ingredients if i.cls == "Hero" and i.category in self.per_cats}
                plan_state.dishes_in_plan = state.dishes_in_plan
                plan_state.cap_counts = state.cap_counts
                plan_state.reused_from_last = state.reused_from_last
                for c in chosen:
                    cuisine_counts[c.cuisine_bucket] = cuisine_counts.get(c.cuisine_bucket, 0) + 1
                    total += 1
                ms.dishes = [self._planned(c, ledger_snapshot=None) for c in chosen]
                slots.append(ms)
        plan = Plan(house_id=self.p.id, house_name=self.p.display_name, servings=self.servings, days=self.days,
                    start_date=self.start_date.isoformat() if self.start_date else None, slots=slots, source="heuristic")
        self.finalize(plan)
        return plan

    def _wants_accompaniment(self, chosen: list[Candidate], acc: Candidate) -> bool:
        if not chosen:
            return False
        main = chosen[0]
        if main.component == "one_pot" and acc.component == "accompaniment":
            return "dosa" in main.dish.lower() or "idli" in main.dish.lower() or "paratha" in main.dish.lower()
        n = main.dish.lower()
        return bool(re.search(r"(dosa|idli|uttapam|appam|paratha|pongal|upma|vada|adai|pesarattu|neer|puttu|pathiri|chilla|akki|thepla|puri|poha)", n)) and acc.score > -0.3

    def _pairs(self, a: Candidate, b: Candidate) -> bool:
        """Cheap pairing sanity: same cuisine family for Indian accompaniments; avoid two rice dishes."""
        if a.component == "staple" and b.component == "staple":
            return False
        if b.component == "accompaniment" or a.component == "accompaniment":
            ia, ib = CUISINE_BUCKETS.get(a.attrs.cuisine), CUISINE_BUCKETS.get(b.attrs.cuisine)
            if "Indian" in (ia or "") and "Indian" in (ib or ""):
                return True
            return ia == ib
        return True

    def _apply(self, c: Candidate, state: DayState, ledger: Ledger, where: str) -> None:
        d = self.repo.dishes[c.dish]
        hits = ledger.consume(d, self.servings, where)
        shelf = self._shelf
        c.perishables = [{"ingredient": k, "need": q, "unit": ledger.units.get(k, ""), "remaining": r}
                         for k, q, r in hits if shelf[k].perishable and q]
        state.dishes_in_plan.add(c.dish)
        if c.attrs.protein_group in HERO_PROTEINS:
            state.proteins_today.add(c.attrs.protein_group)
        if c.attrs.gravy:
            state.gravies_in_meal += 1
        if c.attrs.est_minutes is not None:
            state.minutes_in_meal.append(c.attrs.est_minutes)
        for cap in self.rules.cap_hits(d):
            state.cap_counts[cap] = state.cap_counts.get(cap, 0) + 1
        if c.in_last_plan:
            state.reused_from_last += 1

    def _planned(self, c: Candidate, ledger_snapshot=None) -> PlannedDish:
        d = self.repo.dishes[c.dish]
        return PlannedDish(name=c.dish, component=c.component, diet=c.attrs.diet, cuisine=c.attrs.cuisine, gravy=c.attrs.gravy,
                           protein_group=c.attrs.protein_group, est_minutes=c.attrs.est_minutes, est_minutes_source=c.attrs.est_minutes_source,
                           slot_eligibility=list(c.attrs.slots), flags=[f for f in c.flags if "estimate" not in f],
                           perishables=c.perishables, missing=c.missing, youtube=d.youtube, in_last_plan=c.in_last_plan)

    def _heuristic_rationale(self, chosen: list[Candidate]) -> str:
        if not chosen:
            return "No legal combination found."
        parts = []
        for c in chosen:
            per = ", ".join(f"{p['ingredient']} {p['need']:g}{p['unit']}" for p in c.perishables[:3])
            bit = f"{c.dish} ({c.component}, {c.attrs.cuisine})"
            if per:
                bit += f" clears {per}"
            if c.missing:
                bit += f"; order: {', '.join(c.missing[:3])}"
            parts.append(bit)
        return "Heuristic pick by perishable coverage + variety. " + " | ".join(parts)

    # ----------------------------------------------------------- LLM build
    def assemble_llm(self) -> Plan:
        n = int(self.plan_cfg.get("llm_candidates_per_slot", 45))
        ledger = Ledger(self.inv)
        cand_lists = {}
        for slot in self.planned_slots:
            cs = self.candidates(slot, 2, ledger)  # day 2 => soaking allowed; soak_flag is passed so the LLM can respect day 1
            # ensure each component is represented
            by_comp: dict[str, list[Candidate]] = {}
            for c in cs:
                by_comp.setdefault(c.component, []).append(c)
            picked: list[Candidate] = []
            quota = {"breakfast_main": 18, "one_pot": 10, "accompaniment": 8, "curry": 12, "dal": 8, "dry_veg": 12,
                     "protein_main": 10, "staple": 4, "bread": 4, "salad": 4, "soup": 3}
            for comp, lst in by_comp.items():
                picked.extend(lst[:quota.get(comp, 3)])
            picked = sorted({c.dish: c for c in picked}.values(), key=lambda c: c.score, reverse=True)[:max(n, 30)]
            cand_lists[slot] = [c.brief() for c in picked]
        inv_rows = [{"item": s.item, "canonical": s.canonical, "category": s.category, "qty": s.quantity, "unit": s.unit,
                     "perishable": s.perishable and not s.track_only, "notes": s.notes} for s in self.inv.to_frame().to_dict("records") and
                    [i for i in self.inv.items]]
        inv_rows.sort(key=lambda r: (not r["perishable"], r["category"]))
        last = [{"day": e.day, "slot": e.meal_slot, "dish": e.matched_dish or e.dish_text} for e in self.history.entries]
        tw = self.cfg["time_windows"]
        caps = "; ".join(f"{c.name}: max {c.max_per_plan} per plan" + (" (0 if served last plan)" if c.block_if_in_last_plan else "") for c in self.p.frequency_caps) or "none"
        prompt_vars = dict(
            days=self.days, servings=self.servings, max_gravy=self.plan_cfg.get("max_gravy_per_meal", 1),
            same_protein_note="" if not self.p.allow_same_protein_same_day else " (this household is fine with it)",
            no_seafood_slots=", ".join(self.plan_cfg.get("no_seafood_slots", [])) or "none",
            frequency_caps=caps, rotation_pct=int(float(self.plan_cfg.get("rotation_reuse_pct", 0.15)) * 100),
            soak_day=self.cfg["lead_time"].get("soaking_allowed_from_day", 2),
            time_mode=tw.get("mode"), time_windows=json.dumps(tw.get("windows")) + f" (aggregation: {tw.get('aggregation')})" if tw.get("mode") != "relaxed" else "relaxed - not enforced",
            house_context=self.p.soft_context_text(),
            meal_composition=json.dumps({k: v for k, v in self.p.meal_composition.model_dump().items() if k in self.planned_slots or k == "allow_one_pot"}, indent=0)
            + (f"\nOnly plan these slots: {', '.join(self.planned_slots)}. Leave the others out." if len(self.planned_slots) < 3 else ""),
            inventory=json.dumps(inv_rows, ensure_ascii=False),
            last_plan=json.dumps(last, ensure_ascii=False) or "none",
            candidates=json.dumps(cand_lists, ensure_ascii=False),
        )
        schema = PlanChoice.model_json_schema()
        self.progress("Asking Claude to assemble the plan (with Epicure pairing tools)...")
        choice, raw = self.llm.assemble_plan(prompt_vars, schema)
        used_epicure = bool(self.llm.calls and self.llm.calls[-1].get("epicure_calls"))
        plan = self._plan_from_choice(choice, source="llm+epicure" if used_epicure else "llm")
        plan.llm_calls = list(self.llm.calls)
        return plan

    def _plan_from_choice(self, choice: PlanChoice, source: str) -> Plan:
        fixed: dict[tuple[int, str], list[str]] = {}
        rationale: dict[tuple[int, str], str] = {}
        unknown = []
        for sc in choice.slots:
            slot = sc.slot.strip().title()
            if slot not in self.planned_slots or not (1 <= sc.day <= self.days):
                continue
            names = []
            for n in sc.dishes:
                m = self.repo.match_dish_name(n, threshold=88)
                if m:
                    names.append(m)
                else:
                    unknown.append(n)
            fixed[(sc.day, slot)] = names
            rationale[(sc.day, slot)] = sc.rationale
        # Build through the same machinery so ledger/perishables/flags are computed identically
        plan = self._materialize(fixed, rationale, source)
        plan.tradeoffs = choice.tradeoffs
        if unknown:
            plan.warnings.append("LLM proposed dishes not in the repo (dropped): " + ", ".join(unknown))
        return plan

    def _materialize(self, fixed: dict[tuple[int, str], list[str]], rationale: dict[tuple[int, str], str], source: str) -> Plan:
        ledger = Ledger(self.inv)
        slots = []
        cuisine_counts: dict[str, int] = {}
        total = 0
        plan_state = DayState()
        for day in range(1, self.days + 1):
            day_proteins: set[str] = set()
            for slot in SLOTS:
                if slot not in self.planned_slots:
                    slots.append(self._unplanned(day, slot))
                    continue
                ms = MealSlot(day=day, date=self.date_for(day).isoformat() if self.date_for(day) else None, slot=slot,
                              rationale=rationale.get((day, slot), ""))
                if self.cook_off(day):
                    ms.warnings.append(f"{self.date_for(day).strftime('%A')} is the cook's day off - confirm cover or keep it simple")
                state = DayState(proteins_today=set(day_proteins), dishes_in_plan=set(plan_state.dishes_in_plan),
                                 cap_counts=dict(plan_state.cap_counts), reused_from_last=plan_state.reused_from_last)
                chosen = []
                for name in fixed.get((day, slot), []):
                    d = self.repo.dishes.get(name)
                    if not d:
                        continue
                    a = self.attrs.get(name)
                    v = self.rules.check_dish(d, a, slot, day - 1, state)
                    c = self.score_dish(d, a, slot, ledger, cuisine_counts, total, v.flags)
                    self._apply(c, state, ledger, f"D{day} {slot}")
                    chosen.append(c)
                    if a.protein_group in HERO_PROTEINS:
                        day_proteins.add(a.protein_group)
                    cuisine_counts[c.cuisine_bucket] = cuisine_counts.get(c.cuisine_bucket, 0) + 1
                    total += 1
                plan_state.dishes_in_plan, plan_state.cap_counts, plan_state.reused_from_last = state.dishes_in_plan, state.cap_counts, state.reused_from_last
                ms.dishes = [self._planned(c) for c in chosen]
                slots.append(ms)
        return Plan(house_id=self.p.id, house_name=self.p.display_name, servings=self.servings, days=self.days,
                    start_date=self.start_date.isoformat() if self.start_date else None, slots=slots, source=source)

    # ------------------------------------------------------- validation
    def validate(self, plan: Plan) -> list[str]:
        """Return human-readable violations for every hard rule (empty = valid)."""
        viol: list[str] = []
        seen: dict[str, str] = {}
        cap_counts: dict[str, int] = {}
        reused = 0
        total = 0
        for day in range(1, plan.days + 1):
            proteins: dict[str, str] = {}
            for slot in SLOTS:
                ms = plan.get(day, slot)
                where = f"D{day} {slot}"
                if not ms.planned:
                    continue
                if not ms.dishes:
                    viol.append(f"{where}: empty slot")
                    continue
                comp_spec = getattr(self.p.meal_composition, slot)
                required = [s for s in comp_spec if not s.endswith("?")]
                comps = {d.component for d in ms.dishes}
                if "one_pot" not in comps:
                    gaps = [spec for spec in required if not comps & self._components_for(spec)]
                    if gaps:
                        note = f"composition: no {', '.join(gaps)} dish fits (time window / rules) - meal is lighter than the house default"
                        if note not in ms.warnings:
                            ms.warnings.append(note)
                gravies = 0
                mins = []
                for d in ms.dishes:
                    total += 1
                    dish = self.repo.dishes.get(d.name)
                    if not dish:
                        viol.append(f"{where}: {d.name} is not in the repo")
                        continue
                    a = self.attrs.get(d.name)
                    v = self.rules.check_dish(dish, a, slot, day - 1, None)
                    for r in v.reasons:
                        viol.append(f"{where}: {d.name}: {r}")
                    if d.name in seen and not self.plan_cfg.get("allow_dish_repeat_within_plan", False):
                        viol.append(f"{where}: {d.name} repeated (also {seen[d.name]})")
                    seen.setdefault(d.name, where)
                    if a.protein_group in HERO_PROTEINS:
                        if a.protein_group in proteins and proteins[a.protein_group] != where and not self.p.allow_same_protein_same_day:
                            viol.append(f"{where}: {d.name}: same protein '{a.protein_group}' already at {proteins[a.protein_group]}")
                        proteins.setdefault(a.protein_group, where)
                    gravies += 1 if a.gravy else 0
                    if a.est_minutes is not None:
                        mins.append(a)
                    for cap in self.rules.cap_hits(dish):
                        cap_counts[cap] = cap_counts.get(cap, 0) + 1
                    if d.name in self.last_dishes:
                        reused += 1
                if gravies > int(self.plan_cfg.get("max_gravy_per_meal", 1)):
                    viol.append(f"{where}: {gravies} gravy dishes in one meal")
                tw = self.rules.time_window_check(slot, mins)
                if tw:
                    viol.append(f"{where}: {tw}")
        for cap in self.p.frequency_caps:
            if cap_counts.get(cap.name, 0) > cap.max_per_plan:
                viol.append(f"cap '{cap.name}': {cap_counts[cap.name]} > {cap.max_per_plan} per plan")
        allow = self.rules.rotation_allowance(total)
        if reused > allow:
            viol.append(f"rotation: {reused} dishes reused from last plan > allowance {allow} ({int(float(self.plan_cfg.get('rotation_reuse_pct', .15))*100)}% of {total})")
        return viol

    # ------------------------------------------------------------ repair
    def repair(self, plan: Plan, max_rounds: int = 6) -> Plan:
        """Swap offending dishes for the next-best legal candidate until the plan validates."""
        prev: list[str] | None = None
        for _ in range(max_rounds):
            viol = self.validate(plan)
            if not viol or viol == prev:
                break
            prev = viol
            fixed = {(s.day, s.slot): s.names() for s in plan.slots}
            rationale = {(s.day, s.slot): s.rationale for s in plan.slots}
            changed = False
            for v in viol:
                m = re.match(r"D(\d) (\w+): (.+?): ", v)
                if m:
                    day, slot, dish = int(m.group(1)), m.group(2), m.group(3)
                    if dish in fixed.get((day, slot), []):
                        repl = self._replacement(plan, day, slot, dish, fixed)
                        fixed[(day, slot)] = [repl if n == dish else n for n in fixed[(day, slot)] if not (repl is None and n == dish)]
                        plan.repairs.append(f"D{day} {slot}: {dish} -> {repl or 'removed'} ({v.split(': ', 2)[-1]})")
                        changed = True
                        continue
                m2 = re.match(r"D(\d) (\w+): (\d+) gravy dishes", v)
                m3 = re.match(r"D(\d) (\w+): meal time", v)
                m4 = None
                m5 = re.match(r"D(\d) (\w+): empty slot", v)
                if m2 or m3:
                    mm = m2 or m3
                    day, slot = int(mm.group(1)), mm.group(2)
                    names = fixed[(day, slot)]
                    # drop the lowest-value gravy / longest dish and let the slot rebuild
                    attrs = [(n, self.attrs.get(n)) for n in names]
                    victim = None
                    if m2:
                        g = [n for n, a in attrs if a.gravy]
                        victim = g[-1] if g else None
                    else:
                        victim = max(attrs, key=lambda t: t[1].est_minutes or 0)[0]
                    if victim:
                        repl = self._replacement(plan, day, slot, victim, fixed)
                        fixed[(day, slot)] = [repl if n == victim else n for n in names if not (repl is None and n == victim)]
                        plan.repairs.append(f"D{day} {slot}: {victim} -> {repl or 'removed'} ({v.split(': ', 1)[-1]})")
                        changed = True
                elif m4 or m5:
                    mm = m4 or m5
                    day, slot = int(mm.group(1)), mm.group(2)
                    # rebuild this slot heuristically
                    fixed.pop((day, slot), None)
                    plan.repairs.append(f"D{day} {slot}: rebuilt heuristically ({v.split(': ', 1)[-1]})")
                    changed = True
                elif v.startswith("cap ") or v.startswith("rotation"):
                    # remove the last offending dish and rebuild that slot
                    for s in reversed(plan.slots):
                        for d in reversed(s.dishes):
                            dish = self.repo.dishes.get(d.name)
                            if not dish:
                                continue
                            if (v.startswith("cap ") and any(f"cap '{c}'" in v for c in self.rules.cap_hits(dish))) or (v.startswith("rotation") and d.name in self.last_dishes):
                                repl = self._replacement(plan, s.day, s.slot, d.name, fixed)
                                fixed[(s.day, s.slot)] = [repl if n == d.name else n for n in fixed[(s.day, s.slot)] if not (repl is None and n == d.name)]
                                plan.repairs.append(f"D{s.day} {s.slot}: {d.name} -> {repl or 'removed'} ({v})")
                                changed = True
                                break
                        else:
                            continue
                        break
            if not changed:
                break
            new = self.assemble_heuristic(fixed=fixed)
            for s in new.slots:
                r = rationale.get((s.day, s.slot), "")
                if s.names() == fixed.get((s.day, s.slot)) and r:
                    s.rationale = r
                elif r:
                    s.rationale = (r + " [auto-repaired]").strip()
            new.source, new.tradeoffs, new.repairs, new.llm_calls = plan.source, plan.tradeoffs, plan.repairs, plan.llm_calls
            new.warnings = list(dict.fromkeys(plan.warnings + new.warnings))
            plan = new
        plan.violations = self.validate(plan)
        return plan

    def _replacement(self, plan: Plan, day: int, slot: str, dish: str, fixed: dict) -> str | None:
        """Best legal same-component alternative given everything else in the plan."""
        a_old = self.attrs.get(dish)
        others = {n for (d, s), names in fixed.items() for n in names if n != dish}
        ledger = Ledger(self.inv)
        for (d, s), names in fixed.items():
            for n in names:
                if n != dish and n in self.repo.dishes:
                    ledger.consume(self.repo.dishes[n], self.servings, f"D{d} {s}")
        state = DayState(dishes_in_plan=set(others))
        for n in fixed.get((day, slot), []):
            if n != dish:
                a = self.attrs.get(n)
                if a.gravy:
                    state.gravies_in_meal += 1
                if a.est_minutes is not None:
                    state.minutes_in_meal.append(a.est_minutes)
        for s in SLOTS:
            for n in fixed.get((day, s), []):
                if n != dish and self.attrs.get(n).protein_group in HERO_PROTEINS:
                    state.proteins_today.add(self.attrs.get(n).protein_group)
        for (d, s), names in fixed.items():
            for n in names:
                if n != dish and n in self.repo.dishes:
                    for cap in self.rules.cap_hits(self.repo.dishes[n]):
                        state.cap_counts[cap] = state.cap_counts.get(cap, 0) + 1
        comps = {a_old.component} | ({"curry", "dal"} if a_old.component in ("curry", "dal") else set())
        cs = [c for c in self.candidates(slot, day, ledger, state, {}, 0, comps) if c.dish not in others and not c.in_last_plan]
        return cs[0].dish if cs else None

    # ---------------------------------------------------------- finalize
    def finalize(self, plan: Plan) -> Plan:
        """Fill per-slot time totals, rotation stats, violations."""
        for s in plan.slots:
            mins = [d.est_minutes for d in s.dishes if d.est_minutes is not None]
            s.est_minutes_total = self.rules.aggregate_minutes(mins) if mins else None
        total = sum(len(s.dishes) for s in plan.slots)
        reused = sum(1 for s in plan.slots for d in s.dishes if d.in_last_plan)
        plan.warnings = [w for w in plan.warnings if not w.startswith("Rotation:")]
        plan.warnings.append(f"Rotation: {reused}/{total} dishes reused from last plan (allowance {self.rules.rotation_allowance(total)}).")
        plan.warnings = list(dict.fromkeys(plan.warnings))
        plan.repairs = list(dict.fromkeys(plan.repairs))
        plan.violations = self.validate(plan)
        return plan

    # ------------------------------------------------------------- swap
    def alternatives(self, plan: Plan, day: int, slot: str, dish: str, limit: int = 15) -> list[Candidate]:
        fixed = {(s.day, s.slot): s.names() for s in plan.slots}
        a_old = self.attrs.get(dish)
        others = {n for names in fixed.values() for n in names if n != dish}
        ledger = Ledger(self.inv)
        for (d, s), names in fixed.items():
            for n in names:
                if n != dish and n in self.repo.dishes:
                    ledger.consume(self.repo.dishes[n], self.servings, f"D{d} {s}")
        state = DayState(dishes_in_plan=set(others))
        for n in fixed.get((day, slot), []):
            if n != dish and self.attrs.get(n).gravy:
                state.gravies_in_meal += 1
        comps = {a_old.component} | ({"curry", "dal"} if a_old.component in ("curry", "dal") else set())
        return [c for c in self.candidates(slot, day, ledger, state, {}, 0, comps) if c.dish not in others and c.dish != dish][:limit]

    def swap(self, plan: Plan, day: int, slot: str, old: str, new: str, regenerate_downstream: bool) -> Plan:
        fixed = {(s.day, s.slot): s.names() for s in plan.slots}
        rationale = {(s.day, s.slot): s.rationale for s in plan.slots}
        fixed[(day, slot)] = [new if n == old else n for n in fixed[(day, slot)]]
        rationale[(day, slot)] = f"Operator swapped {old} -> {new}."
        if regenerate_downstream:
            order = [(d, s) for d in range(1, self.days + 1) for s in self.planned_slots]
            idx = order.index((day, slot))
            for key in order[idx + 1:]:
                fixed.pop(key, None)
        newplan = self.assemble_heuristic(fixed=fixed)
        for s in newplan.slots:
            if (s.day, s.slot) in rationale and s.names() == plan.get(s.day, s.slot).names() or (s.day, s.slot) == (day, slot):
                s.rationale = rationale.get((s.day, s.slot), s.rationale)
        newplan.source = plan.source + "+swap"
        newplan.tradeoffs = plan.tradeoffs
        newplan.repairs = plan.repairs + [f"D{day} {slot}: operator swap {old} -> {new}" + (" (downstream regenerated)" if regenerate_downstream else "")]
        return self.repair(newplan) if newplan.violations else newplan

    # --------------------------------------------------------- entry point
    def generate(self, use_llm: bool | None = None) -> Plan:
        use_llm = (not self.llm.mock) if use_llm is None else use_llm
        if use_llm:
            try:
                plan = self.assemble_llm()
                self.progress("Validating and repairing the LLM plan...")
                plan = self.repair(plan)
                self.finalize(plan)
                if plan.violations:
                    plan.warnings.append("LLM plan could not be fully repaired; falling back to heuristic assembly.")
                    hp = self.assemble_heuristic()
                    hp.warnings = plan.warnings + hp.warnings
                    hp.repairs = plan.repairs
                    hp.llm_calls = plan.llm_calls
                    return hp
                return plan
            except Exception as e:  # noqa: BLE001 - surface to the UI, never emit nothing
                self.progress(f"LLM assembly failed ({e}); using heuristic assembler.")
                plan = self.assemble_heuristic()
                plan.warnings.append(f"LLM assembly failed: {e}. Heuristic plan shown instead.")
                return plan
        plan = self.assemble_heuristic()
        return self.repair(plan) if plan.violations else plan
