"""Derived dish attributes (the repo has no dish-level metadata).

* diet          -> deterministic rules over ingredient categories/names (no LLM)
* slots         -> Breakfast / Lunch / Dinner eligibility (heuristic, LLM-refined, operator-correctable)
* component     -> role in a meal: breakfast_main, curry, dal, dry_veg, staple, bread, one_pot,
                   accompaniment, salad, soup, snack, beverage, dessert
* cuisine       -> best-effort (heuristic / LLM)
* gravy         -> bool (rules note: no gravy + gravy)
* est_minutes   -> prep+cook estimate (LLM or heuristic) - ALWAYS labelled with its source
* protein_group -> chicken / seafood / mutton / egg / paneer / dal / soya / none

All derived values are cached per dish in data/cache/dish_attributes.json with a
`source` per field (heuristic | llm | operator) so operator corrections persist.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterable

from .config import load_config
from .repo import Dish, RecipeRepo, is_shellfish

SLOTS = ("Breakfast", "Lunch", "Dinner")
COMPONENTS = ("breakfast_main", "curry", "dal", "dry_veg", "staple", "bread", "one_pot",
              "accompaniment", "salad", "soup", "snack", "beverage", "dessert", "protein_main")
CUISINES = ("South Indian", "North Indian", "Indo-Chinese", "Asian", "Continental", "Mediterranean",
            "Middle Eastern", "Mexican", "Other Indian", "Fusion/Other")

DAIRY_RE = re.compile(r"\b(milk|curd|dahi|yog(h)?urt|paneer|cheese|cream|butter|ghee|khoya|mawa|"
                      r"buttermilk|chaas|chhena|mozzarella|cheddar|feta|parmesan|ricotta|skyr|malai)\b", re.I)
NON_DAIRY_RE = re.compile(r"\b(coconut milk|almond milk|oat milk|soy milk|peanut butter|cocoa butter|"
                          r"dairy-free|vegan|coconut cream|cashew cream)\b", re.I)
EGG_RE = re.compile(r"\b(egg|eggs|egg whites?|egg yolks?|omelette|omelet)\b", re.I)
HONEY_RE = re.compile(r"\bhoney\b", re.I)
FISH_SAUCE_RE = re.compile(r"\b(fish sauce|oyster sauce|shrimp paste|anchov)", re.I)


@dataclass
class DishAttributes:
    dish: str
    # deterministic
    diet: str = "Vegetarian"                 # Vegan | Vegetarian | Eggetarian | Non-Veg
    non_veg_kind: str = ""                   # chicken | fish | shellfish | mutton | mixed | ""
    protein_group: str = "none"
    needs_soaking: bool = False
    needs_marination: bool = False
    needs_resting: bool = False
    # inferred (heuristic -> llm -> operator)
    slots: list[str] = field(default_factory=lambda: ["Lunch", "Dinner"])
    component: str = "dry_veg"
    cuisine: str = "Other Indian"
    gravy: bool = False
    est_minutes: int | None = None
    est_minutes_source: str = "none"         # none | heuristic | llm | operator
    confidence: float = 0.4
    source: str = "heuristic"                # provenance of slots/component/cuisine/gravy
    operator_locked: list[str] = field(default_factory=list)   # fields the operator corrected
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "DishAttributes":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


# ---------------------------------------------------------------------------
# Deterministic diet classifier
# ---------------------------------------------------------------------------
def classify_diet(dish: Dish) -> tuple[str, str, str]:
    """Return (diet, non_veg_kind, protein_group)."""
    cats = dish.categories()
    names = " | ".join(i.name for i in dish.ingredients) + " | " + dish.name
    kinds = set()
    if "Chicken" in cats:
        kinds.add("chicken")
    if "Mutton" in cats:
        kinds.add("mutton")
    if "Seafood" in cats or FISH_SAUCE_RE.search(names):
        sea = [i.name for i in dish.ingredients if i.category == "Seafood"]
        if any(is_shellfish(n) for n in sea) or re.search(r"shrimp paste|oyster sauce", names, re.I):
            kinds.add("shellfish")
        if any(not is_shellfish(n) for n in sea) or re.search(r"fish sauce|anchov", names, re.I):
            kinds.add("fish")
    if kinds:
        nk = next(iter(kinds)) if len(kinds) == 1 else "mixed"
        pg = "seafood" if kinds <= {"fish", "shellfish"} else ("chicken" if kinds == {"chicken"} else ("mutton" if kinds == {"mutton"} else "mixed"))
        return "Non-Veg", nk, pg
    if "Egg" in cats or EGG_RE.search(dish.name):
        return "Eggetarian", "", "egg"
    has_dairy = "Dairy" in cats or any(DAIRY_RE.search(i.name) and not NON_DAIRY_RE.search(i.name) for i in dish.ingredients)
    if any(re.search(r"\bpaneer\b", i.name, re.I) for i in dish.ingredients):
        pg = "paneer"
    elif any(i.cls == "Hero" and re.search(r"\b(dal|lentil|chana|rajma|chickpea|lobia|kidney bean|moong|masoor|toor|urad|sprouts?|beans)\b", i.name, re.I) for i in dish.ingredients) \
            or re.search(r"\b(dal|daal|sambar|rajma|chole|chana masala|pappu|kadala)\b", dish.name, re.I):
        pg = "dal"
    elif any(re.search(r"\b(soya|soy chunks?|tofu|tempeh)\b", i.name, re.I) for i in dish.ingredients):
        pg = "soya"
    else:
        pg = "none"
    if has_dairy or any(HONEY_RE.search(i.name) for i in dish.ingredients):
        return "Vegetarian", "", pg
    return "Vegan", "", pg


# ---------------------------------------------------------------------------
# Heuristic slot / component / cuisine / gravy inference (mock mode + pre-seed)
# ---------------------------------------------------------------------------
_BF = r"(dosa|idli|upma|poha|pongal|uttapam|appam|puttu|pathiri|pathri|chilla|cheela|paratha|parantha|thepla|" \
      r"oats|oatmeal|porridge|muesli|granola|smoothie|shake|juice|tea|coffee|pancake|waffle|toast|sandwich|" \
      r"omelette|omelet|bhurji|scrambled|boiled egg|boil eggs|fried egg|poached|idiyappam|sevai|vermicelli|" \
      r"kanda|aval|dalia|daliya|ragi|besan|sattu|khakhra|akki roti|adai|pesarattu|neer|appe|paniyaram|" \
      r"avocado toast|overnight|kichadi|khichdi|dhokla|handvo|thalipeeth|thaalipeeth|sabudana|" \
      r"medu vada|vada|bonda|misal|usal|sundal|puri|poori|bhatura|chole bhature|croissant|bagel|"\
      r"french toast)"
_ONE_POT = r"(biryani|pulao|pilaf|fried rice|noodles?|pasta|spaghetti|risotto|khichdi|pongal|bisi bele|" \
           r"burrito|bowl|wrap|roll|tacos?|pizza|lasagn|congee|ramen|pad thai|puliyogare|pulihora|lemon rice|" \
           r"tomato rice|coconut rice|curd rice|ghee rice|jeera rice|matar rice|spinach rice|herb rice|" \
           r"egg rice|paneer rice|chicken rice|tikka rice|rice bowl|sandwich|shakshuka|frittata|quesadilla|dosa|idli|upma|poha|uttapam|appam|paratha|thepla|chilla|omelette|toast)"
_GRAVY = r"(curry|gravy|masala|makhani|makhanwala|butter chicken|korma|kurma|kadhai|kadai|kadhi|dal\b|daal|sambar|" \
         r"rasam|kootu|stew|mappas|molee|moilee|ishtu|rogan|tikka masala|do pyaza|dopyaza|shorba|soup|" \
         r"chettinad (chicken|prawn|fish|mutton)( curry)?$|vindaloo|xacuti|salan|qorma|pappu|rajma|chole|chana masala|" \
         r"paneer butter|palak paneer|matar paneer|shahi|malai kofta|kofta|bhuna|handi|nihari|dalcha|" \
         r"aviyal|avial|pulusu|theeyal|erissery|olan|thai (green|red|yellow) curry|laksa|khichdi|manchurian gravy)"
_DRY = r"(sabzi|sabji|subzi|subji|bhaji|poriyal|thoran|palya|upkari|foogath|fry|roast|stir fry|stir-fry|" \
       r"sukka|chukka|varuval|pepper fry|bhurji|do pyaza|jeera aloo|bharta|bhartha|kurkuri|tawa|grilled|" \
       r"tikka|kebab|kabab|air fried|air-fried|baked|saut|tandoori|65|manchurian|chilli|schezwan|pan fried|steak|cutlet|"\
       r"mezhukkupuratti|sundal|usili|podimas|kachumber)"
_STAPLE = r"^(rice|steamed .*rice|plain rice|brown rice|matta rice|jasmine rice|jeera rice|ghee rice|sona masoori|" \
          r"basmati|quinoa|millet|couscous|steamed)$"
_BREAD = r"^(roti|chapati|chapathi|phulka|paratha|plain paratha|lachha paratha|naan|kulcha|bhakri|jowar roti|" \
         r"bajra roti|ragi roti|millet roti|makki ki roti|akki roti|khapli wheat roti|besan roti|methi roti|beetroot roti|masala akki roti|garlic naan|pita|tortilla|wraps?)$"
_ACC = r"(chutney|raita|pickle|achar|dip|podi|thogayal|thuvaiyal|pachadi|kachumber|papad|salsa|hummus|tzatziki|sauce|vinaigrette|dressing|mayo|pesto|gojju|thokku|pachdi|kosambari)"
_SALAD = r"salad|coleslaw|slaw|kosambari"
_SOUP = r"soup|shorba|broth|rasam"
_BEV = r"(tea|coffee|juice|smoothie|shake|lassi|chaas|buttermilk|panna|sharbat|kanji|kombucha|latte|milk$|water$|kadha|kashayam|cooler|mocktail|lemonade|drink|soda|infused water|detox)"
_DESSERT = r"(kheer|payasam|halwa|ladoo|laddu|barfi|burfi|cake|brownie|cookie|pudding|mousse|ice cream|kulfi|sheera|kesari|mysore pak|jamun|rasgulla|sandesh|phirni|modak|tart|muffin|dessert)"
_SNACK = r"(pakora|pakoda|bhajiya|vada|bonda|cutlet|tikki|samosa|kachori|chaat|bhel|sev|chivda|namkeen|momo|dumpling|spring roll|nuggets|fries|popcorn|nachos|patties|kebab|kabab|65|lollipop|garlic bread|bruschetta|crostini|hummus toast)"

_CUISINE_RULES: list[tuple[str, str]] = [
    ("Indo-Chinese", r"(manchurian|schezwan|szechuan|hakka|chilli (chicken|paneer|garlic|gobi|mushroom|fish|prawn)|fried rice|noodles?|" 
                     r"indo.?chinese|momos?|dragon|hot and sour|manchow|sweet corn soup|chow mein|american chop suey|65\b)"),
    ("Asian", r"(thai|korean|japanese|chinese|teriyaki|ramen|sushi|pad thai|soba|udon|kimchi|gochujang|miso|" 
              r"stir.?fry|tom kha|tom yum|laksa|bok choy|edamame|sesame|soy|bao|dumpling|congee|satay|rendang|black pepper chicken|honey garlic|sweet and sour|bang bang|cashew chicken|basil chicken|shirataki|vietnamese|pho|bibimbap|katsu)"),
    ("Mediterranean", r"(greek|mediterranean|feta|hummus|falafel|pita|tzatziki|shakshuka|tabbouleh|couscous|olive|za.?atar|lebanese|turkish|moroccan|harissa|baba ghanoush|fattoush|shawarma|kebab|kabab)"),
    ("Continental", r"(pasta|spaghetti|penne|lasagn|risotto|pizza|sandwich|toast|burger|steak|salad|soup|caesar|pesto|arrabiata|alfredo|aglio|garlic bread|baked|casserole|roast(ed)? (veg|potato|chicken|cauliflower|broccoli)|omelette|omelet|scrambled|frittata|grilled|mac|cheese|wrap|bowl|oats|oatmeal|smoothie|pancake|waffle|muesli|granola|french toast|avocado|bruschetta|quinoa|honey|lemon (chicken|fish|garlic)|herb|creamy|butter garlic|cajun|bbq|barbecue|coleslaw|quiche|croissant|bagel|sourdough)"),
    ("Mexican", r"(burrito|tacos?|quesadilla|salsa|guacamole|nachos|enchilada|fajita|mexican|tortilla|chipotle|black bean|jalape)"),
    ("South Indian", r"(dosa|idli|sambar|sambhar|rasam|poriyal|thoran|kootu|upma|pongal|uttapam|appam|puttu|pathiri|"
                     r"chettinad|kerala|malabar|thalassery|andhra|hyderabadi|mangalore|mangalorean|udupi|karnataka|tamil|telugu|coorg|konkani|malvani|goan|"
                     r"avial|aviyal|olan|erissery|theeyal|molee|moilee|mappas|ishtu|stew|payasam|kesari|puliyogare|pulihora|pulusu|gongura|"
                     r"bisi bele|akki roti|neer|adai|pesarattu|paniyaram|appe|kuzhambu|kulambu|varuval|sukka|chukka|pepper fry|donne|"
                     r"coconut|curry leaves|curd rice|lemon rice|tomato rice|ghee roast|kori|gassi|kozhi|meen|chemmeen|kadala|ulli|podi|thogayal|"
                     r"kosambari|palya|upkari|foogath|mezhukkupuratti|usili|podimas|sundal|vada|bonda|medu|sevai|idiyappam|kanji|ragi|millet|"
                     r"drumstick|raw banana|yam|ash gourd|snake gourd|ridge gourd|colocasia|arbi|tamarind|rava|semiya|kichadi|ven pongal|tomato chutney|peanut chutney|coconut chutney)"),
    ("North Indian", r"(paneer|paratha|parantha|dal makhani|makhani|rajma|chole|chana masala|kadhai|kadai|butter chicken|tikka|tandoori|"
                     r"punjabi|mughlai|awadhi|lucknowi|kashmiri|rogan|dum|biryani|pulao|korma|kofta|bharta|jeera aloo|aloo (gobi|matar|methi|palak|baingan|shimla)|"
                     r"gobi|matar|methi|palak|saag|sarson|bhindi|baingan|lauki|tinda|karela|kaddu|shimla|dahi|raita|kadhi|lassi|"
                     r"roti|chapati|phulka|naan|kulcha|bhatura|poha|kanda|sabzi|sabji|subzi|bhaji|bhurji|dal fry|dal tadka|tadka|"
                     r"moong|masoor|arhar|toor|urad|chana dal|khichdi|halwa|kheer|thepla|dhokla|handvo|undhiyu|gujarati|rajasthani|marathi|maharashtrian|"
                     r"misal|usal|thalipeeth|sabudana|shorba|nihari|keema|do pyaza|dopyaza|bhuna|handi|amritsari|dhaba|achari|malai|shahi|hariyali|lehsun|pyaaz|pyaz|aloo|"
                     r"sattu|bajra|jowar|makki|besan|chilla|cheela|kachori|samosa|chaat|bhel|pav|vada pav|pav bhaji|sev|kachumber|jain|hing|kasundi|bengali|sindhi|bihari|litti)"),
]


def _rx(p: str, s: str) -> bool:
    """Word-bounded, case-insensitive search (so 'neer' never matches 'paneer')."""
    return re.search(r"\b(?:" + p + r")\b", s, re.I) is not None


def heuristic_component_and_slots(dish: Dish, diet: str) -> tuple[str, list[str], bool]:
    n = dish.name.lower().strip()
    ings = dish.ingredient_names_lower()
    is_bev = _rx(_BEV, n) and not _rx(r"(chicken|paneer|fish|rice|curry|sabzi)", n)
    if is_bev:
        return "beverage", ["Breakfast"], False
    if _rx(_SOUP, n):
        return "soup", ["Lunch", "Dinner"], True
    if _rx(_DESSERT, n) and not _rx(r"(sabzi|curry|rice|soup)", n):
        return "dessert", ["Lunch", "Dinner"], False
    if re.fullmatch(_BREAD, n):
        return "bread", ["Breakfast", "Lunch", "Dinner"] if "paratha" in n or "akki" in n else ["Lunch", "Dinner"], False
    if re.fullmatch(_STAPLE, n) or _rx(r"^(steamed|plain|cooked|boiled)? ?(sona masoori|ponni|basmati|brown|matta|red|jasmine|white)? ?rice$", n):
        return "staple", ["Lunch"], False
    if _rx(_SALAD, n):
        return "salad", ["Lunch", "Dinner"], False
    if _rx(_ACC, n) and not _rx(r"(sabzi|curry|chicken|paneer|rice|biryani|masala$)", n):
        slots = ["Breakfast", "Lunch", "Dinner"] if _rx(r"chutney|podi|thogayal|pickle|achar", n) else ["Lunch", "Dinner"]
        return "accompaniment", slots, False
    if _rx(_SNACK, n) and not _rx(r"(curry|sabzi|masala|biryani|rice)", n):
        return "snack", ["Breakfast"], False
    gravy = _rx(_GRAVY, n) and not _rx(r"(dry|sukka|fry|roast|65|tikka$|kebab|bhurji|masala omelette|masala dosa|masala poha|masala upma|masala idli|masala sandwich|masala toast|masala paratha|masala rice|masala oats|masala corn|masala peanut|masala chai|masala egg)", n)
    if not gravy and not _rx(r"(dry|fry|roast|tikka|kebab|bhurji|stir)", n):
        # ingredient-based gravy hint: tomato/curd/coconut milk/cream base with onion and >= 9 ingredients
        wet = _rx(r"(tomato pur|tomato pulp|coconut milk|fresh cream|curd|yogurt|cashew|water|stock|broth|tamarind)", ings)
        if wet and _rx(r"onion", ings) and len(dish.ingredients) >= 9 and _rx(r"(chicken|paneer|dal|egg|fish|kofta|masala|curry)", n) and not _rx(_BF, n):
            gravy = True
    is_bf = _rx(_BF, n)
    is_one_pot = _rx(_ONE_POT, n)
    if is_bf and not _rx(r"(curry|sabzi|gravy|biryani|pulao|masala$)", n):
        # breakfast-register dishes; many are also fine for dinner (dosa/idli) but lunch rarely.
        slots = ["Breakfast"]
        cereal = _rx(r"(oats?|oatmeal|porridge|granola|muesli|smoothie|shake|overnight|pancake|waffle|french toast|juice|tea|coffee)", n)
        if not cereal and _rx(r"(dosa|idli|uttapam|appam|paratha|thepla|chilla|khichdi|sandwich|toast|omelette|bhurji|pongal|puttu|pathiri|idiyappam|sevai|wrap|roll)", n):
            slots.append("Dinner")
        if not cereal and _rx(r"(paratha|thepla|khichdi|wrap|roll|sandwich|pongal|bisi bele)", n):
            slots.append("Lunch")
        comp = "one_pot" if _rx(r"(khichdi|pongal|wrap|roll|sandwich|biryani|pulao)", n) else "breakfast_main"
        return comp, sorted(set(slots), key=SLOTS.index), gravy
    if is_one_pot:
        return "one_pot", ["Lunch", "Dinner"], gravy
    if _rx(r"\b(dal|daal|sambar|rasam|kootu|pappu|kadhi|rajma|chole|chana masala|lobia|kadala|usal|dalcha)\b", n) and not _rx(r"(dosa|chilla|paratha|idli|vada|soup|salad|fry$|sabzi)", n):
        return "dal", ["Lunch", "Dinner"], True
    if diet == "Non-Veg" or _rx(r"(paneer|egg|tofu|soya|tempeh)", n):
        if gravy:
            return "curry", ["Lunch", "Dinner"], True
        return "protein_main", ["Lunch", "Dinner"], False
    if gravy:
        return "curry", ["Lunch", "Dinner"], True
    return "dry_veg", ["Lunch", "Dinner"], False


def heuristic_cuisine(dish: Dish) -> str:
    n = dish.name.lower()
    for cuisine, pat in _CUISINE_RULES:
        if _rx(pat, n):
            return cuisine
    ings = dish.ingredient_names_lower()
    if re.search(r"(soy sauce|sesame oil|rice vinegar|fish sauce|gochujang|miso)", ings):
        return "Asian"
    if re.search(r"(curry leaves|coconut|mustard seeds|urad dal|tamarind|sambar|rasam)", ings):
        return "South Indian"
    if re.search(r"(garam masala|kasuri methi|paneer|cumin seeds|coriander powder)", ings):
        return "North Indian"
    if re.search(r"(olive oil|oregano|basil|parmesan|cheese|butter|pasta|parsley|thyme)", ings):
        return "Continental"
    return "Other Indian"


def heuristic_minutes(dish: Dish, component: str, gravy: bool) -> int:
    """Very rough prep+cook estimate. Labelled 'heuristic' - never presented as fact."""
    n = len(dish.ingredients)
    base = {"beverage": 5, "accompaniment": 10, "salad": 15, "snack": 25, "soup": 25, "dessert": 35,
            "bread": 20, "staple": 20, "breakfast_main": 30, "dry_veg": 25, "dal": 30, "curry": 40,
            "protein_main": 35, "one_pot": 45}.get(component, 30)
    base += max(0, n - 10) * 2
    if gravy:
        base += 5
    if dish.needs_marination:
        base += 15
    if dish.needs_resting:
        base += 10
    if dish.needs_soaking:
        base += 5  # active time only; soaking itself is overnight
    if re.search(r"\b(biryani|dum|slow|mutton|rogan|nihari|baked|roast)\b", dish.name, re.I):
        base += 15
    return int(base)


def heuristic_attributes(dish: Dish) -> DishAttributes:
    diet, nk, pg = classify_diet(dish)
    comp, slots, gravy = heuristic_component_and_slots(dish, diet)
    cuisine = heuristic_cuisine(dish)
    a = DishAttributes(dish=dish.name, diet=diet, non_veg_kind=nk, protein_group=pg,
                       needs_soaking=dish.needs_soaking, needs_marination=dish.needs_marination,
                       needs_resting=dish.needs_resting, slots=slots, component=comp, cuisine=cuisine,
                       gravy=gravy, confidence=0.45, source="heuristic")
    a.est_minutes = heuristic_minutes(dish, comp, gravy)
    a.est_minutes_source = "heuristic"
    return a


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
class AttributeStore:
    """Per-dish attribute cache keyed by dish name; operator edits win over LLM/heuristic."""

    def __init__(self, repo: RecipeRepo, path: Path | None = None):
        self.repo = repo
        self.path = path or load_config().path("cache") / "dish_attributes.json"
        self._data: dict[str, DishAttributes] = {}
        self.load()

    def load(self) -> None:
        if self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._data = {k: DishAttributes.from_dict(v) for k, v in raw.items()}
        else:
            self._data = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({k: v.to_dict() for k, v in self._data.items()}, indent=1, ensure_ascii=False), encoding="utf-8")

    def ensure_all(self) -> int:
        """Make sure every repo dish has (at least heuristic) attributes. Returns #added."""
        added = 0
        for name, dish in self.repo.dishes.items():
            if name not in self._data:
                self._data[name] = heuristic_attributes(dish)
                added += 1
            else:
                # deterministic fields are always refreshed from the repo
                a = self._data[name]
                diet, nk, pg = classify_diet(dish)
                if "diet" not in a.operator_locked:
                    a.diet, a.non_veg_kind = diet, nk
                if "protein_group" not in a.operator_locked:
                    a.protein_group = pg
                a.needs_soaking, a.needs_marination, a.needs_resting = dish.needs_soaking, dish.needs_marination, dish.needs_resting
        if added:
            self.save()
        return added

    def get(self, dish_name: str) -> DishAttributes:
        if dish_name not in self._data:
            d = self.repo.dishes.get(dish_name)
            if d is None:
                raise KeyError(dish_name)
            self._data[dish_name] = heuristic_attributes(d)
        return self._data[dish_name]

    def all(self) -> dict[str, DishAttributes]:
        return self._data

    def pending_llm(self) -> list[str]:
        return [n for n, a in self._data.items() if a.source == "heuristic"]

    def apply_llm(self, updates: Iterable[dict]) -> int:
        """Apply LLM-inferred fields, respecting operator locks."""
        n = 0
        for u in updates:
            name = u.get("dish")
            if not name or name not in self._data:
                # try fuzzy match to repo dish name
                m = self.repo.match_dish_name(name or "", threshold=95)
                if not m:
                    continue
                name = m
            a = self._data[name]
            for f in ("slots", "component", "cuisine", "gravy"):
                if f in u and u[f] is not None and f not in a.operator_locked:
                    val = u[f]
                    if f == "slots":
                        val = [s for s in val if s in SLOTS] or a.slots
                    if f == "component" and val not in COMPONENTS:
                        continue
                    setattr(a, f, val)
            if u.get("est_minutes") is not None and "est_minutes" not in a.operator_locked:
                a.est_minutes = int(u["est_minutes"])
                a.est_minutes_source = "llm"
            if u.get("confidence") is not None:
                a.confidence = float(u["confidence"])
            if u.get("notes"):
                a.notes = str(u["notes"])[:300]
            a.source = "llm" if not a.operator_locked else "operator"
            n += 1
        if n:
            self.save()
        return n

    def apply_operator(self, dish_name: str, **fields) -> None:
        a = self.get(dish_name)
        for k, v in fields.items():
            if v is None:
                continue
            if k == "est_minutes":
                a.est_minutes = int(v)
                a.est_minutes_source = "operator"
            else:
                setattr(a, k, v)
            if k not in a.operator_locked:
                a.operator_locked.append(k)
        a.source = "operator"
        self.save()

    def to_frame(self):
        import pandas as pd
        rows = []
        for a in self._data.values():
            d = a.to_dict()
            d["slots"] = ", ".join(a.slots)
            d["operator_locked"] = ", ".join(a.operator_locked)
            rows.append(d)
        return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# LLM refinement (batched, cached)
# ---------------------------------------------------------------------------
def refine_with_llm(store: AttributeStore, llm, dish_names: list[str] | None = None, batch_size: int | None = None,
                    progress=None, only_pending: bool = True) -> tuple[int, list[str]]:
    """Ask the LLM for slot/component/cuisine/gravy/time for dishes; cache results.

    Returns (n_updated, errors). Operator-locked fields are never overwritten.
    """
    cfg = load_config()
    batch_size = batch_size or int(cfg["llm"].get("attribute_batch_size", 40))
    names = dish_names or (store.pending_llm() if only_pending else list(store.all().keys()))
    if only_pending and dish_names:
        names = [n for n in names if store.get(n).source == "heuristic"]
    errors: list[str] = []
    done = 0
    for i in range(0, len(names), batch_size):
        chunk = names[i:i + batch_size]
        payload = []
        for n in chunk:
            d = store.repo.dishes.get(n)
            if not d:
                continue
            payload.append({"dish": n, "ingredients": [{"name": x.name, "class": x.cls or "Base"} for x in d.ingredients],
                            "flags": {"soaking": d.needs_soaking, "marination": d.needs_marination, "resting": d.needs_resting}})
        if not payload:
            continue
        try:
            batch = llm.infer_attributes(payload)
            done += store.apply_llm([g.model_dump() for g in batch.dishes])
        except Exception as e:  # noqa: BLE001
            errors.append(f"batch {i // batch_size + 1}: {e}")
        if progress:
            progress(min(1.0, (i + len(chunk)) / max(1, len(names))), f"{done} dishes refined")
    return done, errors
