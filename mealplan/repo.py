"""Recipe repo loader.

Reads `Final_Recipes_Sheet.xlsx`:
  * `Recipe Master`      -> one row per (dish, ingredient) with per-adult quantity
  * `Per pax quantity`   -> ingredient master: category, canonical per-pax qty, aliases
  * `Dish overview`      -> dish -> youtube link (fallback video source)

Also provides the ingredient normaliser (alias map + fuzzy match + keyword
categories for egg / bread / meat that the master does not cover).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Iterable

import pandas as pd
from rapidfuzz import fuzz, process

from .config import load_config

UNIT_FIX = {"gm": "g", "gms": "g", "gram": "g", "grams": "g", "pc": "pcs", "piece": "pcs",
            "pieces": "pcs", "nos": "pcs", "no": "pcs", "ml": "ml", "g": "g", "pcs": "pcs",
            "l": "ml", "kg": "g"}

# Categories that exist only implicitly in the repo (not in `Per pax quantity`).
EXTRA_CATEGORIES = ("Egg", "Bread")

# Keyword rules used when an ingredient is not found in the master. Order matters.
KEYWORD_CATEGORY_RULES: list[tuple[str, str]] = [
    (r"\b(egg|eggs|egg whites?|egg yolks?)\b", "Egg"),
    (r"\b(chicken)\b", "Chicken"),
    (r"\b(mutton|lamb|goat|beef|keema)\b", "Mutton"),
    (r"\b(fish|prawns?|shrimps?|salmon|pomfret|tuna|basa|tilapia|mackerel|sardines?|anchov\w*|"
     r"crab|squid|lobster|clams?|mussels?|seer|kingfish|rohu|surmai|bangda|ayala|seafood)\b", "Seafood"),
    (r"\b(bread|pav|bun|buns|sourdough|baguette|pita|loaf|toast)\b", "Bread"),
    (r"\b(milk|curd|dahi|yog(h)?urt|paneer|cheese|cream|butter|ghee|khoya|mawa|buttermilk|"
     r"chaas|chhena|mozzarella|cheddar|feta|parmesan|ricotta|skyr)\b", "Dairy"),
]

SHELLFISH_RE = re.compile(r"\b(prawns?|shrimps?|crab|squid|lobster|clams?|mussels?|oysters?|scallops?)\b", re.I)
PLANT_DAIRY_EXCEPTIONS = re.compile(r"\b(coconut milk|almond milk|oat milk|soy milk|peanut butter|"
                                    r"cocoa butter|dairy-free|vegan)\b", re.I)


def _norm(s: str) -> str:
    s = str(s or "").strip().lower()
    s = re.sub(r"[^a-z0-9\s\-&/]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


@dataclass
class IngredientLine:
    name: str
    canonical: str
    category: str            # Vegetable / Dairy / ... / Egg / Bread / Staple
    per_adult: float | None
    unit: str
    cls: str                 # Base / Hero / Base-Optional / Fat / Garnish / Optional
    soaking: bool
    marination: bool
    resting: bool

    @property
    def essential(self) -> bool:
        return self.cls in {"Base", "Hero", "Fat"}


@dataclass
class Dish:
    name: str
    ingredients: list[IngredientLine]
    youtube: str | None = None

    @property
    def needs_soaking(self) -> bool:
        return any(i.soaking for i in self.ingredients)

    @property
    def needs_marination(self) -> bool:
        return any(i.marination for i in self.ingredients)

    @property
    def needs_resting(self) -> bool:
        return any(i.resting for i in self.ingredients)

    def canonical_ingredients(self) -> set[str]:
        return {i.canonical for i in self.ingredients}

    def categories(self) -> set[str]:
        return {i.category for i in self.ingredients}

    def ingredient_names_lower(self) -> str:
        return " | ".join(i.name.lower() for i in self.ingredients)


@dataclass
class IngredientMaster:
    """Canonical ingredient -> category / per-pax quantity, plus alias lookup."""
    rows: pd.DataFrame
    alias_to_canonical: dict[str, str] = field(default_factory=dict)
    canonical_to_category: dict[str, str] = field(default_factory=dict)
    canonical_per_pax: dict[str, tuple[float, str]] = field(default_factory=dict)
    _choices: list[str] = field(default_factory=list)

    @classmethod
    def from_frame(cls, df: pd.DataFrame) -> "IngredientMaster":
        m = cls(rows=df)
        for _, r in df.iterrows():
            canon = str(r["Ingredient"]).strip()
            cat = str(r["Category"]).strip()
            m.canonical_to_category.setdefault(canon, cat)
            try:
                m.canonical_per_pax.setdefault(canon, (float(r["Per pax qty"]), UNIT_FIX.get(str(r["Unit"]).strip().lower(), str(r["Unit"]).strip())))
            except (TypeError, ValueError):
                pass
            names = [canon] + [a.strip() for a in str(r.get("Names this sheet uses", "") or "").split(",")]
            for a in names:
                if a and a.lower() != "nan":
                    m.alias_to_canonical.setdefault(_norm(a), canon)
        m._choices = list(m.alias_to_canonical.keys())
        return m

    # ---- normalisation -------------------------------------------------
    def canonicalize(self, name: str, fuzzy_threshold: int = 92) -> tuple[str, str, str]:
        """Return (canonical_name, category, match_kind).

        match_kind: exact | fuzzy | keyword | none. Category falls back to
        `Staple` for anything not in the master and not a keyword hit.
        """
        n = _norm(name)
        if not n:
            return name, "Staple", "none"
        if n in self.alias_to_canonical:
            c = self.alias_to_canonical[n]
            return c, self.canonical_to_category.get(c, "Staple"), "exact"
        # keyword categories first for meats/egg/bread (they are more reliable than fuzzy)
        kw = keyword_category(n)
        if kw in ("Egg", "Bread", "Chicken", "Mutton", "Seafood"):
            return name.strip().title(), kw, "keyword"
        if self._choices:
            hit = process.extractOne(n, self._choices, scorer=fuzz.token_sort_ratio, score_cutoff=fuzzy_threshold)
            if hit:
                c = self.alias_to_canonical[hit[0]]
                return c, self.canonical_to_category.get(c, "Staple"), "fuzzy"
        if kw:
            return name.strip().title(), kw, "keyword"
        return name.strip().title(), "Staple", "none"

    def per_pax(self, canonical: str, category: str) -> tuple[float, str] | None:
        if canonical in self.canonical_per_pax:
            return self.canonical_per_pax[canonical]
        fb = load_config()["perishables"].get("fallback_per_pax", {}).get(category)
        if fb:
            return float(fb["qty"]), fb["unit"]
        return None


def keyword_category(norm_name: str) -> str | None:
    if PLANT_DAIRY_EXCEPTIONS.search(norm_name):
        # e.g. coconut milk is a Fruit-category item in the master; not dairy.
        for pat, cat in KEYWORD_CATEGORY_RULES:
            if cat != "Dairy" and re.search(pat, norm_name):
                return cat
        return None
    for pat, cat in KEYWORD_CATEGORY_RULES:
        if re.search(pat, norm_name):
            return cat
    return None


def is_shellfish(name: str) -> bool:
    return bool(SHELLFISH_RE.search(name))


@dataclass
class RecipeRepo:
    dishes: dict[str, Dish]
    master: IngredientMaster
    dish_links: dict[str, str]
    source_path: Path

    # ---- lookups -------------------------------------------------------
    def get(self, name: str) -> Dish | None:
        return self.dishes.get(name) or self.dishes.get(self.match_dish_name(name) or "")

    @property
    def names(self) -> list[str]:
        return list(self.dishes.keys())

    def match_dish_name(self, text: str, threshold: int = 85) -> str | None:
        """Case-insensitive + fuzzy match of free text to a repo dish name."""
        if not text:
            return None
        t = _norm(text)
        lower = {_norm(n): n for n in self.dishes}
        if t in lower:
            return lower[t]
        hit = process.extractOne(t, list(lower.keys()), scorer=fuzz.WRatio, score_cutoff=threshold)
        return lower[hit[0]] if hit else None

    def match_dish_names(self, texts: Iterable[str], threshold: int = 85) -> dict[str, str | None]:
        return {t: self.match_dish_name(t, threshold) for t in texts}

    def ingredient_frame(self) -> pd.DataFrame:
        rows = []
        for d in self.dishes.values():
            for i in d.ingredients:
                rows.append({"Dish": d.name, "Ingredient": i.name, "Canonical": i.canonical,
                             "Category": i.category, "Per adult": i.per_adult, "Unit": i.unit,
                             "Class": i.cls, "Soaking": i.soaking, "Marination": i.marination,
                             "Resting": i.resting})
        return pd.DataFrame(rows)


def _yes(v) -> bool:
    return str(v).strip().lower() in {"yes", "y", "true", "1"}


def load_repo(path: Path | None = None) -> RecipeRepo:
    cfg = load_config()
    path = Path(path) if path else cfg.path("recipe_repo")
    xl = pd.ExcelFile(path)
    rm = xl.parse("Recipe Master")
    pp = xl.parse("Per pax quantity")
    ov = xl.parse("Dish overview")

    master = IngredientMaster.from_frame(pp.dropna(subset=["Ingredient"]))

    links: dict[str, str] = {}
    for _, r in ov.iterrows():
        n, l = r.get("dish name"), r.get("youtube link")
        if isinstance(n, str) and isinstance(l, str) and l.startswith("http"):
            links.setdefault(n.strip(), l.strip())

    rm = rm.dropna(subset=["Dish", "Ingredient"]).copy()
    rm["Dish"] = rm["Dish"].astype(str).str.strip()
    rm["Ingredient"] = rm["Ingredient"].astype(str).str.strip()
    rm["Unit"] = rm["Unit"].fillna("g").astype(str).str.strip().str.lower().map(lambda u: UNIT_FIX.get(u, u if u not in ("nan", "") else "g"))
    rm["Class"] = rm["Class"].fillna("Base").astype(str).str.strip()
    rm["Per adult"] = pd.to_numeric(rm["Per adult"], errors="coerce")

    # canonicalise unique ingredient names once (fast) then map
    uniq = rm["Ingredient"].unique()
    canon_map = {u: master.canonicalize(u) for u in uniq}

    dishes: dict[str, Dish] = {}
    for dish_name, g in rm.groupby("Dish", sort=True):
        yt = None
        for v in g["Youtube video link"].dropna():
            if isinstance(v, str) and v.startswith("http"):
                yt = v.strip()
                break
        lines = []
        for _, r in g.iterrows():
            canon, cat, _kind = canon_map[r["Ingredient"]]
            lines.append(IngredientLine(
                name=r["Ingredient"], canonical=canon, category=cat,
                per_adult=None if pd.isna(r["Per adult"]) else float(r["Per adult"]),
                unit=r["Unit"], cls=r["Class"],
                soaking=_yes(r.get("Soaking")), marination=_yes(r.get("Marination")),
                resting=_yes(r.get("Resting")),
            ))
        dishes[dish_name] = Dish(name=dish_name, ingredients=lines, youtube=yt or links.get(dish_name))
    return RecipeRepo(dishes=dishes, master=master, dish_links=links, source_path=path)


@lru_cache(maxsize=1)
def get_repo() -> RecipeRepo:
    return load_repo()
