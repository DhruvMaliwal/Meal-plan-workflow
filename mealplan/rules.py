"""Deterministic hard-rule engine.

Every hard constraint from Section 6 lives here so it can be applied twice:
as a pre-filter (candidate legality) and as a post-validator (whole-plan
legality). Soft preferences are NOT enforced here; they go to the scorer/LLM.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from .attributes import DishAttributes
from .config import load_config
from .history import PlanHistory
from .profiles import HouseProfile
from .repo import Dish, IngredientLine

DIET_RANK = {"Vegan": 0, "Vegetarian": 1, "Eggetarian": 2, "Non-Veg": 3}
# ingredient names containing these are NOT the base ingredient (rice vinegar != rice)
DERIVED_QUALIFIERS = re.compile(r"\b(vinegar|flour|paper|syrup|powder|oil|sauce|noodles?|bran|water|stock|broth|"
                                r"extract|essence|flakes|puffed|crispies|milk|cream|seasoning|masala|paste|"
                                r"seeds?|leaves|leaf|vermicelli|sevai|semiya|starch|flake|cereal)\b")
HERO_PROTEINS = {"chicken", "seafood", "mutton", "egg", "paneer", "mixed"}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", (s or "").lower())).strip()


_KW_CACHE: dict[str, tuple] = {}
_NORM_CACHE: dict[str, str] = {}


def _norm_cached(s: str) -> str:
    v = _NORM_CACHE.get(s)
    if v is None:
        v = _NORM_CACHE[s] = _norm(s)
    return v


def _kw(keyword: str):
    v = _KW_CACHE.get(keyword)
    if v is None:
        kw = _norm(keyword)
        v = _KW_CACHE[keyword] = (kw, re.compile(rf"\b{re.escape(kw)}\b") if kw else None, DERIVED_QUALIFIERS.search(kw) is not None)
    return v


def ingredient_matches(keyword: str, line: IngredientLine) -> bool:
    """True if `keyword` refers to this ingredient (not a derived product of it)."""
    kw, pat, kw_has_q = _kw(keyword)
    if not kw:
        return False
    name, canon = _norm_cached(line.name), _norm_cached(line.canonical)
    if kw == canon or kw == name:
        return True
    for target in (name, canon):
        if pat.search(target):
            # 'rice' should not match 'rice flour' unless the keyword itself says so
            if not kw_has_q and DERIVED_QUALIFIERS.search(target.replace(kw, " ")):
                continue
            return True
    return False


def dish_name_matches(keywords: list[str], dish_name: str) -> str | None:
    n = _norm_cached(dish_name)
    for k in keywords:
        kk, pat, _ = _kw(k)
        if kk and pat.search(n):
            return k
    return None


@dataclass
class Verdict:
    ok: bool
    reasons: list[str] = field(default_factory=list)   # why excluded
    flags: list[str] = field(default_factory=list)     # allowed-with-note


@dataclass
class DayState:
    """What has already been placed on a given day / in the plan (for stateful rules)."""
    proteins_today: set[str] = field(default_factory=set)
    gravies_in_meal: int = 0
    minutes_in_meal: list[int] = field(default_factory=list)
    dishes_in_plan: set[str] = field(default_factory=set)
    cap_counts: dict[str, int] = field(default_factory=dict)
    reused_from_last: int = 0


class RuleEngine:
    def __init__(self, profile: HouseProfile, history: PlanHistory | None = None):
        self.p = profile
        self.cfg = load_config()
        self.history = history or PlanHistory()
        self.last_dishes = self.history.recent_dishes()
        self.last_hero_ingredients = set()
        self.exclusions = profile.hard_exclusion_ingredients_all()
        self.portion_rules = profile.portion_only_rules()
        self.time_cfg = self.cfg["time_windows"]
        self.plan_cfg = self.cfg["plan"]
        self._static_cache: dict[tuple, Verdict] = {}
        self._cap_cache: dict[str, list[str]] = {}

    def set_history(self, history: PlanHistory, repo_dishes: dict[str, Dish]) -> None:
        self.history = history
        self.last_dishes = history.recent_dishes()
        heroes = set()
        for name in self.last_dishes:
            d = repo_dishes.get(name)
            if d:
                heroes |= {i.canonical.lower() for i in d.ingredients if i.cls == "Hero"}
        self.last_hero_ingredients = heroes
        self._static_cache.clear()

    # ------------------------------------------------------------------ caps
    def cap_hits(self, dish: Dish) -> list[str]:
        c = self._cap_cache.get(dish.name)
        if c is not None:
            return c
        hits = []
        for cap in self.p.frequency_caps:
            if any(ingredient_matches(k, i) and i.cls in ("Hero", "Base") for k in cap.match_ingredients for i in dish.ingredients) \
                    or dish_name_matches(cap.match_dish_keywords, dish.name):
                hits.append(cap.name)
        self._cap_cache[dish.name] = hits
        return hits

    # --------------------------------------------------------- per-dish check
    def check_dish(self, dish: Dish, a: DishAttributes, slot: str, day_idx: int, state: DayState | None = None) -> Verdict:
        """Static (memoised) rules + stateful rules for the current plan position."""
        soak_from = int(self.cfg["lead_time"].get("soaking_allowed_from_day", 2))
        key = (dish.name, slot, day_idx + 1 >= soak_from, a.source, a.est_minutes, tuple(a.slots), a.component, a.gravy)
        base = self._static_cache.get(key)
        if base is None:
            base = self._static_check(dish, a, slot, day_idx)
            self._static_cache[key] = base
        v = Verdict(ok=base.ok, reasons=list(base.reasons), flags=list(base.flags))
        if state is not None:
            v.reasons.extend(self._state_check(dish, a, slot, state))
            v.ok = not v.reasons
        return v

    def _state_check(self, dish: Dish, a: DishAttributes, slot: str, state: DayState) -> list[str]:
        reasons = []
        if dish.name in state.dishes_in_plan and not self.plan_cfg.get("allow_dish_repeat_within_plan", False):
            reasons.append("already in this plan")
        if a.protein_group in HERO_PROTEINS and a.protein_group in state.proteins_today and not self.p.allow_same_protein_same_day:
            reasons.append(f"same protein ({a.protein_group}) already today")
        if a.gravy and state.gravies_in_meal >= int(self.plan_cfg.get("max_gravy_per_meal", 1)):
            reasons.append("gravy + gravy in the same meal")
        if state.cap_counts:
            for cap_name in self.cap_hits(dish):
                cap = next(c for c in self.p.frequency_caps if c.name == cap_name)
                if state.cap_counts.get(cap_name, 0) >= cap.max_per_plan:
                    reasons.append(f"cap '{cap_name}' reached ({cap.max_per_plan}/plan)")
        if a.est_minutes is not None and state.minutes_in_meal and self.time_cfg.get("mode") != "relaxed" and self._time_source_ok(a):
            agg = self.aggregate_minutes(state.minutes_in_meal + [a.est_minutes])
            hi = self.upper_bound(slot)
            if hi and agg > hi:
                reasons.append(f"meal time {agg} min would exceed {slot} window {hi} min")
        return reasons

    def _static_check(self, dish: Dish, a: DishAttributes, slot: str, day_idx: int) -> Verdict:
        v = Verdict(ok=True)
        p = self.p
        # diet
        if DIET_RANK.get(a.diet, 3) > DIET_RANK.get(p.diet, 3):
            v.reasons.append(f"diet: dish is {a.diet}, house is {p.diet}")
        # slot eligibility (derived attribute)
        if slot not in a.slots:
            v.reasons.append(f"slot: not eligible for {slot} (eligible: {', '.join(a.slots)})")
        # hard exclusions
        for k in self.exclusions:
            hit = [i.name for i in dish.ingredients if ingredient_matches(k, i)]
            if hit:
                v.reasons.append(f"hard exclusion '{k}' via ingredient {hit[0]}")
                break
        kw = dish_name_matches(p.hard_exclusions_dish_keywords, dish.name)
        if kw:
            v.reasons.append(f"hard exclusion dish keyword '{kw}'")
        # portion-only rules (e.g. curd -> Raksha only)
        for pr in self.portion_rules:
            hits = [i for i in dish.ingredients if any(ingredient_matches(k, i) for k in pr.ingredients)] \
                   + ([i for i in dish.ingredients] if dish_name_matches(pr.dishes, dish.name) and pr.dishes else [])
            if not hits:
                continue
            essential = any(i.cls in ("Hero", "Base") for i in hits)
            if a.component == "accompaniment" or not essential:
                v.flags.append(f"{pr.person}: {hits[0].name} -> portion-only ({pr.note.split('.')[0]})")
            else:
                v.reasons.append(f"{hits[0].name} is essential and {pr.person} does not eat it (shared meal)")
        # slot ingredient bans (no rice/cucumber at dinner)
        for ban in p.slot_ingredient_bans:
            if ban.slot != slot:
                continue
            for i in dish.ingredients:
                if (not ban.classes or i.cls in ban.classes) and any(ingredient_matches(k, i) for k in ban.ingredients):
                    v.reasons.append(f"{slot} ban: {i.name} ({ban.note.split('.')[0]})")
                    break
        # dish keyword rules (no cucumber in salads; no sandwiches at dinner)
        for r in p.dish_keyword_rules:
            if r.slots and slot not in r.slots:
                continue
            kw = dish_name_matches(r.dish_keywords, dish.name)
            if not kw:
                continue
            if not r.ingredients:
                v.reasons.append(f"rule: '{kw}' dishes not allowed at {slot} ({r.note.split('.')[0]})")
            else:
                hit = [i.name for i in dish.ingredients if any(ingredient_matches(k, i) for k in r.ingredients)]
                if hit:
                    v.reasons.append(f"rule: '{kw}' + {hit[0]} ({r.note.split('.')[0]})")
        # global: no seafood at lunch
        if slot in self.plan_cfg.get("no_seafood_slots", []) and a.non_veg_kind in ("fish", "shellfish", "mixed") and a.protein_group in ("seafood", "mixed"):
            v.reasons.append(f"no seafood at {slot}")
        # lead time
        soak_from = int(self.cfg["lead_time"].get("soaking_allowed_from_day", 2))
        if a.needs_soaking:
            if day_idx + 1 < soak_from:
                v.reasons.append(f"needs soaking; only feasible from day {soak_from} (plan a day ahead)")
            else:
                v.flags.append("soak/batter the night before")
        if a.needs_marination:
            v.flags.append("marinate in the morning")
        # time window
        tw = self.time_window_check(slot, [a] , single=True)
        if tw:
            v.reasons.append(tw)
        if a.est_minutes is not None and a.est_minutes_source in ("llm", "heuristic") and self.time_cfg.get("mode") == "estimated":
            v.flags.append(f"~{a.est_minutes} min ({a.est_minutes_source} estimate)")
        # frequency caps with last-plan lookback
        for cap in self.p.frequency_caps:
            if cap.block_if_in_last_plan and cap.name in self.cap_hits(dish):
                if any(k.lower() in self.last_hero_ingredients for k in cap.match_ingredients) or \
                        any(dish_name_matches(cap.match_dish_keywords, d) for d in self.last_dishes if cap.match_dish_keywords):
                    v.reasons.append(f"cap '{cap.name}': already served in the last plan")
        v.ok = not v.reasons
        return v

    # --------------------------------------------------------------- time
    def _time_source_ok(self, a: DishAttributes) -> bool:
        mode = self.time_cfg.get("mode", "estimated")
        if mode == "relaxed" or a.est_minutes is None:
            return False
        if mode == "operator":
            return a.est_minutes_source == "operator"
        return a.est_minutes_source in ("operator", "llm", "heuristic")

    def upper_bound(self, slot: str) -> int | None:
        w = self.time_cfg.get("windows", {}).get(slot)
        if not w:
            return None
        if self.time_cfg.get("scope") == "combined" and slot in ("Breakfast", "Lunch"):
            return int(self.time_cfg["windows"]["Breakfast"][1]) + int(self.time_cfg["windows"]["Lunch"][1])
        return int(w[1])

    def lower_bound(self, slot: str) -> int | None:
        w = self.time_cfg.get("windows", {}).get(slot)
        return int(w[0]) if w and self.time_cfg.get("enforce_lower_bound") else None

    def aggregate_minutes(self, mins: list[int]) -> int:
        mins = [m for m in mins if m is not None]
        if not mins:
            return 0
        agg = self.time_cfg.get("aggregation", "max_plus_overhead")
        if agg == "sum":
            return int(sum(mins))
        if agg == "max":
            return int(max(mins))
        return int(max(mins) + int(self.time_cfg.get("overhead_min_per_extra_dish", 15)) * (len(mins) - 1))

    def time_window_check(self, slot: str, attrs: list[DishAttributes], single: bool = False) -> str | None:
        if self.time_cfg.get("mode") == "relaxed":
            return None
        usable = [a.est_minutes for a in attrs if self._time_source_ok(a)]
        if not usable:
            return None
        agg = self.aggregate_minutes(usable)
        hi = self.upper_bound(slot)
        if hi and agg > hi:
            return f"{'dish' if single else 'meal'} time ~{agg} min exceeds {slot} window ({hi} min)"
        lo = self.lower_bound(slot)
        if not single and lo and agg < lo:
            return f"meal time ~{agg} min is under the {slot} lower bound ({lo} min)"
        return None

    # ---------------------------------------------------------- rotation
    def rotation_allowance(self, total_dishes: int) -> int:
        pct = float(self.plan_cfg.get("rotation_reuse_pct", 0.15))
        return int(math.floor(total_dishes * pct + 1e-9))
