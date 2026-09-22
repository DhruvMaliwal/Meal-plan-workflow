"""House preference profiles: schema + store.

One JSON file per household in data/house_profiles/. Adding a house = dropping
in a file that validates against `HouseProfile`. Hard rules are enforced
deterministically by the engine; soft rules are passed to the LLM as context.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .config import load_config

Slot = Literal["Breakfast", "Lunch", "Dinner"]


class Resident(BaseModel):
    name: str
    notes: str = ""


class Pet(BaseModel):
    name: str = ""
    kind: str = ""
    portioning_note: str = ""


class Cook(BaseModel):
    name: str = ""
    schedule: str = ""
    days_off: list[str] = Field(default_factory=list, description="Weekday names, e.g. ['Sunday']")
    language: str = ""
    notes: list[str] = Field(default_factory=list)


class PersonRule(BaseModel):
    """Per-person like/dislike/conditional rule."""
    person: str
    kind: Literal["dislike", "like", "hard_exclusion", "conditional"]
    ingredients: list[str] = Field(default_factory=list, description="Ingredient keywords (matched on canonical + raw names)")
    dishes: list[str] = Field(default_factory=list, description="Dish name keywords")
    note: str = ""
    # For 'conditional' rules only: how the engine treats them.
    # 'portion_only' -> allowed but flagged (serve to this person only / omit for this person)
    # 'exclude'      -> treated as a hard exclusion for shared meals
    # 'soft'         -> passed to the LLM only
    enforcement: Literal["portion_only", "exclude", "soft"] = "soft"


class SlotIngredientBan(BaseModel):
    slot: Slot
    ingredients: list[str]
    note: str = ""
    # Only ban when the ingredient plays one of these roles (empty = any role).
    classes: list[str] = Field(default_factory=lambda: ["Base", "Hero"])


class DishKeywordRule(BaseModel):
    """Bans dish-name keyword + ingredient co-occurrence, e.g. salad + cucumber."""
    dish_keywords: list[str]
    ingredients: list[str] = Field(default_factory=list)
    slots: list[Slot] = Field(default_factory=list, description="Empty = all slots")
    note: str = ""


class WeekdayRule(BaseModel):
    """Bans that apply only on a given weekday (e.g. Tuesday veg day, Monday fast, no eggs Tue/Thu)."""
    weekday: Literal["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    slots: list[Slot] = Field(default_factory=list, description="Empty = all slots")
    ban_ingredients: list[str] = Field(default_factory=list)
    ban_dish_keywords: list[str] = Field(default_factory=list)
    note: str = ""


class FrequencyCap(BaseModel):
    name: str
    match_ingredients: list[str] = Field(default_factory=list)
    match_dish_keywords: list[str] = Field(default_factory=list)
    max_per_plan: int = 1
    # If the ingredient appeared in the last plan, allow zero this plan (e.g. paneer <= 1 / 2 weeks).
    block_if_in_last_plan: bool = False
    note: str = ""


class BrandRule(BaseModel):
    brand: str
    action: Literal["never_buy", "prefer"] = "never_buy"
    note: str = ""


class IngredientGradeRule(BaseModel):
    ingredient: str
    rule: str


class MealComposition(BaseModel):
    """Default number/type of dishes per slot. Components map to derived `component` attribute."""
    Breakfast: list[str] = Field(default_factory=lambda: ["breakfast_main", "accompaniment?"])
    Lunch: list[str] = Field(default_factory=lambda: ["curry|dal", "dry_veg", "staple"])
    Dinner: list[str] = Field(default_factory=lambda: ["curry|dal", "dry_veg", "bread"])
    # A one-pot dish (biryani, pasta, noodles, fried rice, bowl) may replace the whole slot.
    allow_one_pot: dict[str, bool] = Field(default_factory=lambda: {"Breakfast": True, "Lunch": True, "Dinner": True})


class HouseProfile(BaseModel):
    id: str
    display_name: str
    location: str = ""
    residents: list[Resident]
    pets: list[Pet] = Field(default_factory=list)
    cook: Cook = Field(default_factory=Cook)
    diet: Literal["Vegetarian", "Eggetarian", "Non-Veg", "Vegan"] = "Non-Veg"

    # --- hard rules (deterministic) ---
    hard_exclusions_ingredients: list[str] = Field(default_factory=list, description="Never cook; ingredient keywords")
    hard_exclusions_dish_keywords: list[str] = Field(default_factory=list, description="Never cook; dish-name keywords")
    person_rules: list[PersonRule] = Field(default_factory=list)
    slot_ingredient_bans: list[SlotIngredientBan] = Field(default_factory=list)
    dish_keyword_rules: list[DishKeywordRule] = Field(default_factory=list)
    frequency_caps: list[FrequencyCap] = Field(default_factory=list)
    weekday_rules: list[WeekdayRule] = Field(default_factory=list)
    allow_same_protein_same_day: bool = False
    allow_lunch_to_dinner_carryover: bool = False
    disallow_marination: bool = False
    # Which slots the cook actually serves; unplanned slots are left empty ("self-managed").
    planned_slots: list[Slot] = Field(default_factory=lambda: ["Breakfast", "Lunch", "Dinner"])
    # Servings for quantity math when it is not simply the resident count (e.g. cook serves 3).
    default_servings: int | None = None
    # Per-house override of config plan.rotation_reuse_pct (0 = never repeat last plan's dishes).
    rotation_reuse_pct: float | None = None

    # --- ingredient / brand rules ---
    brand_rules: list[BrandRule] = Field(default_factory=list)
    ingredient_grade_rules: list[IngredientGradeRule] = Field(default_factory=list)

    # --- soft context (LLM) ---
    cuisine_split_target: dict[str, float] = Field(default_factory=dict, description="e.g. {'South Indian': .4, 'North Indian': .3, 'Experimental/Asian': .3}")
    liked_ingredients: list[str] = Field(default_factory=list)
    disliked_ingredients_soft: list[str] = Field(default_factory=list)
    breakfast_style: str = ""
    cooking_technique: dict[str, str] = Field(default_factory=dict)
    nutrition_direction: list[str] = Field(default_factory=list)
    weekend_pattern: str = ""
    standing_instructions: list[str] = Field(default_factory=list)
    non_negotiables: list[str] = Field(default_factory=list)
    always_in_stock: list[str] = Field(default_factory=list, description="Never on the order list unless inventory says zero")
    meal_composition: MealComposition = Field(default_factory=MealComposition)
    notes: list[str] = Field(default_factory=list)

    # --- helpers ---
    @property
    def n_residents(self) -> int:
        return len(self.residents)

    @property
    def servings(self) -> int:
        return int(self.default_servings or len(self.residents))

    def hard_exclusion_ingredients_all(self) -> list[str]:
        out = list(self.hard_exclusions_ingredients)
        for pr in self.person_rules:
            if pr.kind == "hard_exclusion" or (pr.kind == "conditional" and pr.enforcement == "exclude"):
                out.extend(pr.ingredients)
        return out

    def portion_only_rules(self) -> list[PersonRule]:
        return [pr for pr in self.person_rules if pr.kind == "conditional" and pr.enforcement == "portion_only"]

    def soft_context_text(self) -> str:
        """Compact, LLM-facing summary of the softer preferences."""
        parts = [f"Household: {self.display_name} ({self.location}). Residents: "
                 + ", ".join(f"{r.name}{' - ' + r.notes if r.notes else ''}" for r in self.residents) + "."]
        if self.cook.name:
            parts.append(f"Cook: {self.cook.name}, {self.cook.schedule}; days off: {', '.join(self.cook.days_off) or 'none'}.")
        if len(self.planned_slots) < 3:
            parts.append("Slots planned by the cook: " + ", ".join(self.planned_slots) + " (other meals are self-managed by residents).")
        if self.weekday_rules:
            parts.append("Weekday rules: " + "; ".join(f"{w.weekday}{' ' + '/'.join(w.slots) if w.slots else ''}: no {', '.join(w.ban_ingredients + w.ban_dish_keywords)}" for w in self.weekday_rules) + ".")
        if self.cuisine_split_target:
            parts.append("Cuisine split target: " + ", ".join(f"{k} ~{int(v*100)}%" for k, v in self.cuisine_split_target.items()) + ".")
        if self.breakfast_style:
            parts.append("Breakfast style: " + self.breakfast_style)
        if self.liked_ingredients:
            parts.append("Loved ingredients (lean in): " + ", ".join(self.liked_ingredients) + ".")
        if self.disliked_ingredients_soft:
            parts.append("Disliked (avoid where possible): " + ", ".join(self.disliked_ingredients_soft) + ".")
        if self.cooking_technique:
            parts.append("Cooking technique: " + "; ".join(f"{k}: {v}" for k, v in self.cooking_technique.items()) + ".")
        if self.nutrition_direction:
            parts.append("Nutrition direction: " + "; ".join(self.nutrition_direction) + ".")
        if self.weekend_pattern:
            parts.append("Weekend pattern: " + self.weekend_pattern)
        for pr in self.person_rules:
            if pr.kind in ("like", "dislike") or (pr.kind == "conditional" and pr.enforcement in ("soft", "portion_only")):
                parts.append(f"{pr.person} {pr.kind}: {', '.join(pr.ingredients + pr.dishes)}. {pr.note}".strip())
        if self.standing_instructions:
            parts.append("Standing instructions:\n- " + "\n- ".join(self.standing_instructions))
        if self.non_negotiables:
            parts.append("NON-NEGOTIABLES:\n- " + "\n- ".join(self.non_negotiables))
        if self.notes:
            parts.append("Notes:\n- " + "\n- ".join(self.notes))
        return "\n".join(parts)


def profiles_dir() -> Path:
    return load_config().path("house_profiles")


def list_profiles() -> dict[str, Path]:
    out = {}
    for p in sorted(profiles_dir().glob("*.json")):
        if p.name.startswith("_"):
            continue
        try:
            out[json.loads(p.read_text(encoding="utf-8"))["id"]] = p
        except Exception:
            continue
    return out


def load_profile(house_id: str) -> HouseProfile:
    path = list_profiles()[house_id]
    return HouseProfile.model_validate_json(path.read_text(encoding="utf-8"))


def save_profile(profile: HouseProfile, path: Path | None = None) -> Path:
    path = path or profiles_dir() / f"{profile.id}.json"
    path.write_text(profile.model_dump_json(indent=2), encoding="utf-8")
    return path


def schema_json() -> str:
    return json.dumps(HouseProfile.model_json_schema(), indent=2)
