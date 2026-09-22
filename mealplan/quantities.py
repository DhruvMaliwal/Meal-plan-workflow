"""Exact quantity math: dish ingredients x servings vs on-shelf stock.

Drives (a) the perishable-exhaustion objective, (b) Sheet 2 'Perishable Mapping',
(c) Sheet 3 'Order List'.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .config import load_config
from .inventory import Inventory
from .repo import Dish, IngredientLine, RecipeRepo


@dataclass
class Need:
    dish: str
    ingredient: str
    canonical: str
    category: str
    qty: float | None       # scaled to servings
    unit: str
    cls: str
    essential: bool


def dish_needs(dish: Dish, servings: int) -> list[Need]:
    out = []
    for i in dish.ingredients:
        q = None if i.per_adult is None else round(i.per_adult * servings, 1)
        out.append(Need(dish.name, i.name, i.canonical, i.category, q, i.unit, i.cls, i.essential))
    return out


# Rough average piece weights (g) used only to reconcile 'pcs' on the dashboard with 'g' in recipes.
PIECE_WEIGHTS_G = {"lemon": 40, "lime": 30, "egg": 50, "onion": 80, "tomato": 80, "potato": 100, "coconut": 400,
                   "banana": 100, "apple": 150, "orange": 150, "bread": 30, "pav": 40, "cucumber": 150, "carrot": 70,
                   "capsicum": 120, "bell pepper": 120, "drumstick": 75, "brinjal": 150, "eggplant": 150, "beetroot": 120,
                   "green chilli": 3, "garlic": 40, "ginger": 30, "mango": 250, "kiwi": 80, "pomegranate": 250,
                   "bottle gourd": 500, "cauliflower": 500, "broccoli": 300, "radish": 150, "sweet potato": 150,
                   "raw banana": 120, "corn": 200, "sweet corn": 200, "avocado": 150, "papaya": 800, "pineapple": 900}


def _unit_compatible(a: str, b: str) -> bool:
    a, b = (a or "").lower(), (b or "").lower()
    return a == b or {a, b} <= {"g", "ml"} or a == "" or b == ""


def to_shelf_units(canonical: str, qty: float | None, unit: str, shelf_unit: str) -> float | None:
    """Convert a recipe need into the unit the dashboard uses. None if not resolvable."""
    if qty is None:
        return None
    u, su = str(unit or "").lower(), str(shelf_unit or "").lower()
    if u in ("nan", "none"):
        u = ""
    if su in ("nan", "none"):
        su = ""
    if _unit_compatible(u, su):
        return qty
    w = PIECE_WEIGHTS_G.get(canonical.lower())
    if w is None:
        for k, v in PIECE_WEIGHTS_G.items():
            if k in canonical.lower():
                w = v
                break
    if w is None:
        return None
    if su == "pcs" and u in ("g", "ml"):
        return qty / w
    if su in ("g", "ml") and u == "pcs":
        return qty * w
    return None


@dataclass
class Ledger:
    """Tracks remaining on-shelf quantity as dishes are added to the plan."""
    inventory: Inventory
    remaining: dict[str, float | None] = field(default_factory=dict)
    units: dict[str, str] = field(default_factory=dict)
    used: dict[str, float] = field(default_factory=dict)          # canonical -> total used
    used_by: dict[str, list[tuple[str, str, float]]] = field(default_factory=dict)  # canonical -> [(dish, day/slot, qty)]

    shelf: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.shelf:
            self.shelf = self.inventory.by_canonical()
        if not self.remaining:
            for c, s in self.shelf.items():
                self.remaining[c] = s.quantity
                self.units[c] = s.unit

    def copy(self) -> "Ledger":
        l = Ledger(self.inventory, shelf=self.shelf)
        l.remaining = dict(self.remaining)
        l.units = dict(self.units)
        l.used = dict(self.used)
        l.used_by = {k: list(v) for k, v in self.used_by.items()}
        return l

    def on_shelf(self, canonical: str) -> bool:
        return canonical in self.remaining and (self.remaining[canonical] is None or self.remaining[canonical] > 0)

    def consume(self, dish: Dish, servings: int, where: str) -> list[tuple[str, float, float | None]]:
        """Deduct the dish's needs. Returns [(canonical, qty_used, remaining_after)] for perishables."""
        hits = []
        for n in dish_needs(dish, servings):
            if n.canonical not in self.remaining:
                continue
            q = to_shelf_units(n.canonical, n.qty, n.unit, self.units.get(n.canonical, n.unit))
            q = 0.0 if q is None else q
            rem = self.remaining[n.canonical]
            if rem is not None:
                self.remaining[n.canonical] = max(0.0, rem - q)
            self.used[n.canonical] = self.used.get(n.canonical, 0.0) + q
            self.used_by.setdefault(n.canonical, []).append((dish.name, where, q))
            hits.append((n.canonical, q, self.remaining[n.canonical]))
        return hits


def perishable_use_score(dish: Dish, ledger: Ledger, servings: int, perishable_categories: set[str]) -> tuple[float, list[dict]]:
    """How much of the *remaining* perishable stock this dish would clear (0..n).

    Each perishable contributes min(need, remaining)/on_shelf, weighted 2x if the
    dish would finish the item. Items with unknown quantity count 0.5.
    """
    score = 0.0
    detail = []
    shelf = ledger.shelf
    for n in dish_needs(dish, servings):
        if n.category not in perishable_categories or n.canonical not in ledger.remaining:
            continue
        rem = ledger.remaining[n.canonical]
        total = shelf[n.canonical].quantity
        if rem is None or total in (None, 0):
            contrib = 0.5
            frac = None
        elif rem <= 0:
            contrib = 0.0
            frac = 0.0
        else:
            need = to_shelf_units(n.canonical, n.qty, n.unit, ledger.units.get(n.canonical, n.unit)) or 0.0
            used = min(need, rem)
            frac = used / total if total else 0
            contrib = frac * (2.0 if need >= rem * 0.8 else 1.0)
        if contrib:
            score += contrib
            detail.append({"ingredient": n.canonical, "need": n.qty, "unit": n.unit, "remaining": rem,
                           "shelf_unit": ledger.units.get(n.canonical, n.unit), "share": frac})
    return score, detail


def missing_essentials(dish: Dish, ledger: Ledger, servings: int, assumed_pantry: set[str], always_in_stock: set[str]) -> list[Need]:
    """Essential ingredients not on the shelf and not assumed pantry."""
    out = []
    for n in dish_needs(dish, servings):
        if not n.essential:
            continue
        low = n.canonical.lower()
        if low in assumed_pantry or low in always_in_stock or n.ingredient.lower() in assumed_pantry:
            continue
        if n.category == "Staple" and any(k in low for k in ("salt", "water", "oil", "powder", "seeds", "masala", "paste")):
            continue
        if ledger.on_shelf(n.canonical):
            continue
        out.append(n)
    return out


def build_order_list(plan_dishes: list[tuple[str, Dish]], inventory: Inventory, servings: int, always_in_stock: set[str]) -> list[dict]:
    """Aggregate required quantities across the plan minus on-shelf stock.

    plan_dishes: [(day/slot label, Dish)].
    Returns rows: item, category, required, on_shelf, to_order, unit, used_in, status.
    """
    cfg = load_config()["perishables"]
    assumed = {a.lower() for a in cfg.get("assumed_pantry", [])}
    shelf = inventory.by_canonical()
    req: dict[str, dict] = {}
    for where, dish in plan_dishes:
        for n in dish_needs(dish, servings):
            r = req.setdefault(n.canonical, {"item": n.canonical, "category": n.category, "required": 0.0, "unit": n.unit,
                                             "used_in": [], "essential": False, "unknown_qty": False})
            if n.qty is None:
                r["unknown_qty"] = True
            else:
                r["required"] += n.qty
            r["used_in"].append(f"{dish.name} ({where})")
            r["essential"] = r["essential"] or n.essential
            if not _unit_compatible(r["unit"], n.unit):
                r["unknown_qty"] = True
    rows = []
    for canon, r in sorted(req.items(), key=lambda kv: (kv[1]["category"] != "Staple", kv[1]["category"], kv[0])):
        on = shelf.get(canon)
        on_q = on.quantity if on else None
        low = canon.lower()
        status = ""
        if low in always_in_stock:
            if on is None or on_q is None or on_q > 0:
                continue  # house keeps it stocked; only order if inventory says zero
            status = "always-in-stock item at zero - order"
        if on is not None and on_q is None:
            to_order = 0.0
            status = status or "on shelf (qty unknown) - check"
        elif on is not None:
            req_shelf = to_shelf_units(canon, r["required"], r["unit"], on.unit)
            if req_shelf is None:
                r["unknown_qty"] = True
                to_order = 0.0
                status = status or f"unit mismatch (need {r['unit']}, shelf {on.unit}) - check"
            else:
                to_order = max(0.0, req_shelf - on_q)
                if to_order == 0:
                    continue
                # report in shelf units when converted
                if on.unit and on.unit != r["unit"]:
                    r["unit_note"] = f"{r['required']:g}{r['unit']} ~ {req_shelf:.1f} {on.unit}"
                    r["required"], r["unit"] = round(req_shelf, 1), on.unit
                status = status or ("partial on shelf" if on_q > 0 else "on shelf at zero")
        else:
            if low in assumed:
                continue  # pantry staple not on dashboard: assume present
            to_order = r["required"]
            status = status or ("not on dashboard" + ("" if r["essential"] else " (optional/garnish)"))
        if r["unknown_qty"]:
            status += "; qty unresolved - check"
        rows.append({"item": canon, "category": r["category"], "required": round(r["required"], 1),
                     "on_shelf": on_q if on else 0, "to_order": round(to_order, 1) if not r["unknown_qty"] else "check",
                     "unit": r["unit"], "essential": r["essential"], "used_in": "; ".join(dict.fromkeys(r["used_in"])),
                     "status": (status + ("; " + r["unit_note"] if r.get("unit_note") else "")).strip("; ")})
    return rows


def perishable_mapping(plan_dishes: list[tuple[str, Dish]], inventory: Inventory, servings: int) -> list[dict]:
    """Every on-shelf perishable -> dishes consuming it, qty used vs on shelf, leftover."""
    ledger = Ledger(inventory)
    for where, dish in plan_dishes:
        ledger.consume(dish, servings, where)
    rows = []
    for s in inventory.perishables(include_track_only=True):
        canon = s.canonical
        uses = ledger.used_by.get(canon, [])
        used = ledger.used.get(canon, 0.0)
        left = None if s.quantity is None else max(0.0, s.quantity - used)
        cov = None if s.quantity in (None, 0) else min(1.0, used / s.quantity)
        rows.append({"perishable": s.item, "canonical": canon, "category": s.category, "on_shelf": s.quantity, "unit": s.unit,
                     "used": round(used, 1), "left_unused": None if left is None else round(left, 1),
                     "coverage": None if cov is None else round(cov, 2),
                     "consumed_by": "; ".join(f"{d} [{w}] {q:g}{s.unit}" for d, w, q in uses) or "-",
                     "track_only": s.track_only})
    return rows
