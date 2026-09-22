"""Inventory: parsed dashboard -> normalised stock with perishable tagging."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

import pandas as pd

from .config import load_config
from .llm import InventoryExtraction, InventoryItem
from .repo import RecipeRepo, UNIT_FIX

PERISHABLE_ORDER = ["Chicken", "Seafood", "Mutton", "Egg", "Dairy", "Bread", "Vegetable", "Fruit", "Vegetable - aromatic", "Staple"]


@dataclass
class StockItem:
    item: str                 # as written on the dashboard
    canonical: str            # repo canonical ingredient name
    category: str
    quantity: float | None
    unit: str
    perishable: bool
    track_only: bool = False  # perishable but excluded from the exhaustion objective (aromatics)
    match: str = "none"       # exact | fuzzy | keyword | none
    notes: str = ""

    @property
    def qty_g(self) -> float | None:
        return self.quantity


@dataclass
class Inventory:
    items: list[StockItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    source: str = ""

    def perishables(self, include_track_only: bool = False) -> list[StockItem]:
        return [i for i in self.items if i.perishable and (include_track_only or not i.track_only)]

    def by_canonical(self) -> dict[str, StockItem]:
        out: dict[str, StockItem] = {}
        for i in self.items:
            if i.canonical in out and out[i.canonical].quantity is not None and i.quantity is not None:
                out[i.canonical].quantity += i.quantity   # merge duplicate lines
            else:
                out.setdefault(i.canonical, i)
        return out

    def on_shelf(self) -> set[str]:
        return {i.canonical for i in self.items if (i.quantity is None or i.quantity > 0)}

    def to_frame(self) -> pd.DataFrame:
        cols = ["item", "canonical", "category", "quantity", "unit", "perishable", "track_only", "match", "notes"]
        if not self.items:
            return pd.DataFrame(columns=cols)
        df = pd.DataFrame([asdict(i) for i in self.items])[cols]
        df["_order"] = df["category"].map(lambda c: PERISHABLE_ORDER.index(c) if c in PERISHABLE_ORDER else 99)
        return df.sort_values(["_order", "item"]).drop(columns="_order").reset_index(drop=True)

    @classmethod
    def from_frame(cls, df: pd.DataFrame, repo: RecipeRepo, source: str = "edited") -> "Inventory":
        items = []
        for _, r in df.iterrows():
            if not str(r.get("item", "")).strip():
                continue
            q = r.get("quantity")
            try:
                q = None if q is None or pd.isna(q) or q == "" else float(q)
            except (TypeError, ValueError):
                q = None
            canon = str(r.get("canonical") or "").strip()
            cat = str(r.get("category") or "").strip()
            if not canon or not cat:
                canon2, cat2, _ = repo.master.canonicalize(str(r["item"]))
                canon, cat = canon or canon2, cat or cat2
            items.append(_make_stock(str(r["item"]), canon, cat, q, str(r.get("unit") or ""), str(r.get("match") or "operator"), str(r.get("notes") or "")))
        return cls(items=items, source=source)


def _make_stock(item: str, canonical: str, category: str, quantity, unit: str, match: str, notes: str) -> StockItem:
    cfg = load_config()["perishables"]
    unit = UNIT_FIX.get(unit.strip().lower(), unit.strip().lower()) if unit else ""
    if unit == "kg" and quantity is not None:
        quantity, unit = quantity * 1000, "g"
    if unit == "l" and quantity is not None:
        quantity, unit = quantity * 1000, "ml"
    per = category in cfg["categories"]
    track = category in cfg.get("track_only_categories", []) or canonical.lower() in {t.lower() for t in cfg.get("track_only_ingredients", [])}
    return StockItem(item=item.strip(), canonical=canonical, category=category, quantity=quantity, unit=unit,
                     perishable=per or track, track_only=track, match=match, notes=notes or "")


def normalise_extraction(ex: InventoryExtraction, repo: RecipeRepo, source: str = "image") -> Inventory:
    """Map the LLM's rows onto the repo's canonical ingredient names + categories."""
    inv = Inventory(warnings=list(ex.warnings), source=source)
    for it in ex.items:
        canon, cat, kind = repo.master.canonicalize(it.canonical or it.item)
        if kind == "none" and it.canonical and it.canonical != it.item:
            canon2, cat2, kind2 = repo.master.canonicalize(it.item)
            if kind2 != "none":
                canon, cat, kind = canon2, cat2, kind2
        # trust the LLM's category for the explicit extras when the master has nothing
        if kind == "none" and it.category in ("Egg", "Bread", "Chicken", "Seafood", "Mutton", "Dairy", "Vegetable", "Fruit", "Vegetable - aromatic"):
            cat = it.category
        if kind == "none" and canon.lower() == it.item.strip().lower() and it.canonical:
            canon = it.canonical.strip().title()
        inv.items.append(_make_stock(it.item, canon, cat, it.quantity, it.unit, kind, it.notes))
    return inv


# ---- cache -----------------------------------------------------------------
def image_hash(blobs: list[bytes]) -> str:
    h = hashlib.sha256()
    for b in blobs:
        h.update(b)
    return h.hexdigest()[:16]


def cache_path(kind: str, key: str) -> Path:
    return load_config().path("cache") / f"{kind}_{key}.json"


def save_inventory_cache(key: str, inv: Inventory) -> None:
    cache_path("inventory", key).write_text(json.dumps({"items": [asdict(i) for i in inv.items], "warnings": inv.warnings, "source": inv.source}, indent=1), encoding="utf-8")


def load_inventory_cache(key: str) -> Inventory | None:
    p = cache_path("inventory", key)
    if not p.exists():
        return None
    d = json.loads(p.read_text(encoding="utf-8"))
    return Inventory(items=[StockItem(**i) for i in d["items"]], warnings=d.get("warnings", []), source=d.get("source", "cache"))
