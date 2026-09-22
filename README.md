# 3-Day Household Meal Plan Generator

Local Streamlit tool that builds a **3-day Breakfast / Lunch / Dinner plan** for a selected household using
**only dishes from `Final_Recipes_Sheet.xlsx`**, honouring the house preference profile, exhausting the
perishables on the shelf, and exporting a **3-sheet Excel** (Meal Plan · Perishable Mapping · Order List).

```
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...        # or run with MEALPLAN_MOCK=1 (no API calls)
streamlit run app.py
```

Then open http://localhost:8501. Quick CLI test without the UI:

```
MEALPLAN_MOCK=1 python -m mealplan.cli --mock --start 2026-09-25
```

## Folder layout

```
app.py                       Streamlit operator UI (select house -> inputs -> generate -> review/swap -> export)
config.yaml                  every tunable (time windows, rotation %, weights, model, Epicure URL, pantry list)
prompts/*.md                 editable LLM prompts (inventory OCR, last-plan OCR, attribute inference, assembly)
mealplan/
  repo.py                    Recipe Master / Per pax quantity / Dish overview loader + alias normaliser
  profiles.py                house profile schema (pydantic) + store
  attributes.py              derived dish attributes: diet (deterministic), slot/component/cuisine/gravy/time (heuristic -> LLM -> operator) + cache
  rules.py                   deterministic hard-rule engine (pre-filter + post-validator)
  inventory.py               dashboard image -> normalised stock, perishable tagging, cache
  history.py                 last-plan image -> matched dishes (rotation) + layout template
  quantities.py              exact per-adult x servings math: perishable ledger, mapping, order list
  engine.py                  planner: score -> assemble (Claude + Epicure MCP or heuristic) -> validate -> repair -> explain -> swap
  export.py                  Excel writer (mirrors the last-plan layout)
  llm.py                     Anthropic API wrapper + mock mode
  cli.py                     command-line runner
data/
  recipe_repo/Final_Recipes_Sheet.xlsx
  house_profiles/<house>.json          one file per household (drop in a new file = new house, no code change)
  samples/                             sample inventory CSV + last plan JSON used in mock mode
  uploads/  cache/                     runtime (git-ignored)
outputs/                     generated {house}_{date}_mealplan.xlsx
tests/                       pytest suite (offline)
```

## Operator workflow

1. **Select house** in the sidebar; the Profile tab shows the rules and lets you edit the JSON.
2. **Inputs tab**: upload the inventory dashboard photo(s) and last week's plan photo(s), click *Parse*
   (Claude vision, cached by image hash), then **correct the editable tables** and click *Apply*.
   You can also upload an inventory CSV or use the sample data.
3. **Dish attributes tab** (optional but recommended once per repo): *Refine attributes with Claude* to replace
   the heuristic slot / component / cuisine / gravy / time guesses. Correct any dish by hand; corrections are locked.
4. **Generate & review**: shows the plan, per-dish *why* (perishables consumed with quantities, slot, diet,
   lead-time flags, rationale), auto-repairs, trade-offs. **Swap a dish** from the legal alternatives and
   optionally regenerate downstream slots.
5. **Export**: writes the Excel to `outputs/` and offers a download.

## How the engine works

1. **Deterministic pre-filter** (`rules.py`): diet, hard exclusions, per-person rules (essential curd -> excluded
   for shared mains; non-essential/accompaniment -> allowed and flagged *portion-only*), slot eligibility, slot
   ingredient bans (no rice/cucumber at dinner), dish-keyword rules (no cucumber in salads, no sandwiches at
   dinner, raita never tomato+curd), no seafood at lunch, soaking only from day 2, time windows, frequency caps
   (chicken 1x/plan, paneer 1x and 0 if in last plan), same-protein-same-day, gravy+gravy, no dish repeats,
   rotation cap.
2. **Scoring** (`engine.py`): perishable exhaustion (share of remaining shelf stock the dish clears, exact
   `Per adult x servings` math) - ordering penalty (essential ingredients not on shelf / not pantry)
   + cuisine-split fit + nutrition direction + liked ingredients - repeats of last plan's hero ingredients.
3. **Assembly**: with an API key, Claude (`claude-opus-5`, configurable) gets the legal candidate lists per slot
   with quantities, inventory, house context and the **Epicure MCP** (`https://epicure-mcp.kaikaku.ai/mcp`)
   pairing tools via the Anthropic MCP connector, and returns a structured plan with per-slot rationale and
   trade-offs. In mock mode / on API failure a greedy heuristic assembler produces the plan.
4. **Post-validation + auto-repair**: every hard rule is re-checked on the whole plan; offending dishes are
   swapped for the next-best legal candidate until the plan validates. An invalid plan is never exported.
5. **Explainability**: each planned dish records perishables consumed (+qty, remaining), derived attributes,
   time estimate + source, portion-only / soak / marinate flags, missing essentials and the assembler's rationale.

## Decisions taken on the Section 12 questions (change in `config.yaml`)

| # | Question | Default implemented |
|---|---|---|
| 1 | Cooking time (no data) | **(b)** LLM-estimated prep+cook minutes per dish, cached, labelled *estimate* everywhere, operator-correctable. Heuristic placeholders until refined. `time_windows.mode`: `estimated` / `operator` / `relaxed` |
| 2 | Time-window scope | **per meal** (`scope: per_meal`); `combined` supported. Aggregation `max_plus_overhead` (longest dish + 15 min per extra dish); `sum`/`max` available. Only the upper bound is enforced (`enforce_lower_bound: false`) |
| 3 | Rotation % | **15%** of the new plan's dishes may come from the last plan (`rotation_reuse_pct`) |
| 4 | Servings | residents count (2); guests added in the sidebar scale all quantity math |
| 5 | Meal composition | per house in the profile: Breakfast = main (+ chutney when it suits), Lunch = curry/dal + dry veg + rice/roti, Dinner = curry/dal + dry veg + roti; a one-pot dish may replace a slot |

## Known gaps (honest list)

- **Times are estimates.** The repo has no time field; nothing here pretends otherwise. Refine with Claude, then correct.
- **Slot / cuisine / gravy are inferred.** Heuristics are decent for common Indian names and wrong for odd ones
  (the UI shows provenance; the LLM refinement fixes most). Slot eligibility is a hard filter, so review it.
- **Diet edge cases**: fish sauce / shrimp paste make a dish Non-Veg (seafood); honey makes a dish Vegetarian
  not Vegan; ghee counts as dairy. Override per dish in the Attributes tab.
- **Eggs & bread** are not in `Per pax quantity`; they are handled as explicit perishable categories with
  fallback per-pax quantities (`fallback_per_pax`). Piece <-> gram reconciliation uses rough piece weights.
- **Image parsing is imperfect** by design; always use the correction step.
- **Epicure MCP** is called server-side by the Anthropic API (beta `mcp-client-2025-11-20`). If the connector is
  rejected the assembler retries without tools, then falls back to the heuristic plan and says so.
- This environment could not reach the Anthropic API, so the live (non-mock) paths are written against the SDK
  docs and degrade gracefully, but have not been exercised end-to-end here.

## Adding a house

Copy `data/house_profiles/gurupriyan_raksha.json`, change `id` / `display_name` and the rules. The schema is in the
Profile tab (download JSON Schema) or `python -c "from mealplan.profiles import schema_json; print(schema_json())"`.

## Tests

```
pytest -q tests      # offline; ~20 s
```
