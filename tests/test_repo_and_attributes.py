from mealplan.attributes import classify_diet


def test_repo_shape(repo):
    assert len(repo.dishes) == 1585
    d = repo.get("Palak Paneer")
    assert d is not None and len(d.ingredients) >= 10
    assert all(i.unit in ("g", "ml", "pcs") for i in d.ingredients)


def test_alias_and_keyword_categories(repo):
    m = repo.master
    assert m.canonicalize("Bhindi")[0:2] == ("Okra", "Vegetable")
    assert m.canonicalize("Methi Leaves")[0] == "Fenugreek Leaves"
    assert m.canonicalize("Eggs")[1] == "Egg"
    assert m.canonicalize("Whole Wheat Bread")[1] == "Bread"
    assert m.canonicalize("Chicken Thigh")[1] == "Chicken"
    assert m.canonicalize("Coconut Milk")[1] == "Fruit"          # not dairy
    assert m.canonicalize("Turmeric")[1] == "Staple"


def test_fuzzy_dish_match(repo):
    assert repo.match_dish_name("palak panner") == "Palak Paneer"
    assert repo.match_dish_name("MASALA DOSA") == "Masala Dosa"
    assert repo.match_dish_name("completely unknown dish xyz") is None


def test_diet_classifier(repo):
    assert classify_diet(repo.get("Chicken Curry"))[0] == "Non-Veg"
    assert classify_diet(repo.get("Egg Bhurji"))[0] == "Eggetarian"
    assert classify_diet(repo.get("Palak Paneer"))[0] == "Vegetarian"
    diet, kind, group = classify_diet(repo.get("Prawn Garlic Butter Pasta"))
    assert diet == "Non-Veg" and kind == "shellfish" and group == "seafood"
    diet, kind, _ = classify_diet(repo.get("Fish Curry"))
    assert kind == "fish"


def test_heuristic_attributes(store):
    a = store.get("Masala Dosa")
    assert "Breakfast" in a.slots and a.component == "breakfast_main" and a.cuisine == "South Indian"
    assert store.get("Dal Tadka").gravy is True
    assert store.get("Roti").component == "bread"
    assert store.get("Rice").component == "staple"
    assert store.get("Coconut Chutney").component == "accompaniment"
    assert store.get("Chicken Hakka Noodles").component == "one_pot"
    assert store.get("Palak Paneer").est_minutes_source == "heuristic"


def test_operator_lock(store):
    store.apply_operator("Roti", est_minutes=12, component="bread")
    a = store.get("Roti")
    assert a.est_minutes == 12 and a.est_minutes_source == "operator"
    store.apply_llm([{"dish": "Roti", "est_minutes": 99, "component": "snack", "slots": ["Lunch"], "cuisine": "North Indian", "gravy": False}])
    a = store.get("Roti")
    assert a.est_minutes == 12 and a.component == "bread" and a.slots == ["Lunch"]
