"""Last meal-plan history: match extracted dish text to repo dishes; capture layout."""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

import pandas as pd

from .config import load_config
from .llm import LastPlanExtraction, FormatTemplate
from .repo import RecipeRepo


@dataclass
class HistoryEntry:
    day: str
    meal_slot: str
    dish_text: str
    matched_dish: str | None
    score: str = ""


@dataclass
class PlanHistory:
    entries: list[HistoryEntry] = field(default_factory=list)
    template: FormatTemplate = field(default_factory=FormatTemplate)
    warnings: list[str] = field(default_factory=list)

    def recent_dishes(self) -> set[str]:
        return {e.matched_dish for e in self.entries if e.matched_dish}

    def recent_ingredient_hits(self, repo: RecipeRepo) -> set[str]:
        """Canonical ingredients that were Hero in any recently cooked dish (for 'don't repeat drumstick' logic)."""
        out: set[str] = set()
        for e in self.entries:
            d = repo.dishes.get(e.matched_dish or "")
            if d:
                out |= {i.canonical for i in d.ingredients if i.cls == "Hero"}
        return out

    def to_frame(self) -> pd.DataFrame:
        cols = ["day", "meal_slot", "dish_text", "matched_dish", "score"]
        if not self.entries:
            return pd.DataFrame(columns=cols)
        return pd.DataFrame([asdict(e) for e in self.entries])[cols]

    @classmethod
    def from_frame(cls, df: pd.DataFrame, template: FormatTemplate, repo: RecipeRepo) -> "PlanHistory":
        entries = []
        for _, r in df.iterrows():
            txt = str(r.get("dish_text") or "").strip()
            if not txt:
                continue
            m = r.get("matched_dish")
            m = None if m is None or (isinstance(m, float) and pd.isna(m)) or str(m).strip() in ("", "None") else str(m).strip()
            if m and m not in repo.dishes:
                m = repo.match_dish_name(m)
            entries.append(HistoryEntry(day=str(r.get("day") or ""), meal_slot=str(r.get("meal_slot") or ""), dish_text=txt, matched_dish=m, score="operator"))
        return cls(entries=entries, template=template)


def build_history(ex: LastPlanExtraction, repo: RecipeRepo) -> PlanHistory:
    h = PlanHistory(template=ex.format_template, warnings=list(ex.warnings))
    for e in ex.entries:
        m = repo.match_dish_name(e.dish, threshold=80)
        h.entries.append(HistoryEntry(day=e.day, meal_slot=_norm_slot(e.meal_slot), dish_text=e.dish, matched_dish=m,
                                      score="matched" if m else "unmatched"))
    unmatched = [e.dish_text for e in h.entries if not e.matched_dish]
    if unmatched:
        h.warnings.append("Could not match to repo: " + ", ".join(unmatched))
    return h


def _norm_slot(s: str) -> str:
    s = (s or "").strip().lower()
    if s.startswith("b"):
        return "Breakfast"
    if s.startswith("l"):
        return "Lunch"
    if s.startswith("d") or "night" in s or "supper" in s:
        return "Dinner"
    return s.title()


def save_history_cache(key: str, h: PlanHistory) -> None:
    p = load_config().path("cache") / f"lastplan_{key}.json"
    p.write_text(json.dumps({"entries": [asdict(e) for e in h.entries], "template": h.template.model_dump(), "warnings": h.warnings}, indent=1), encoding="utf-8")


def load_history_cache(key: str) -> PlanHistory | None:
    p = load_config().path("cache") / f"lastplan_{key}.json"
    if not p.exists():
        return None
    d = json.loads(p.read_text(encoding="utf-8"))
    return PlanHistory(entries=[HistoryEntry(**e) for e in d["entries"]], template=FormatTemplate.model_validate(d["template"]), warnings=d.get("warnings", []))
