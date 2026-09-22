"""3-Day Household Meal Plan Generator - Streamlit operator UI.

Run:  streamlit run app.py          (set ANTHROPIC_API_KEY, or MEALPLAN_MOCK=1 for offline mode)
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import pandas as pd
import streamlit as st

from mealplan.attributes import COMPONENTS, CUISINES, SLOTS, AttributeStore, refine_with_llm
from mealplan.config import load_config, reload_config
from mealplan.engine import Plan, Planner
from mealplan.export import export_plan
from mealplan.history import PlanHistory, build_history, load_history_cache, save_history_cache
from mealplan.inventory import Inventory, image_hash, load_inventory_cache, normalise_extraction, save_inventory_cache
from mealplan.llm import LLM, FormatTemplate
from mealplan.profiles import HouseProfile, list_profiles, load_profile, save_profile, schema_json
from mealplan.repo import get_repo

st.set_page_config(page_title="Meal Plan Generator", page_icon="🍲", layout="wide")
cfg = load_config()


# ---------------------------------------------------------------- resources
@st.cache_resource(show_spinner="Loading recipe repo (1,585 dishes)...")
def _repo():
    return get_repo()


@st.cache_resource(show_spinner="Deriving dish attributes...")
def _store():
    s = AttributeStore(_repo())
    s.ensure_all()
    return s


repo = _repo()
store = _store()
ss = st.session_state
ss.setdefault("inventory", None)
ss.setdefault("history", None)
ss.setdefault("plan", None)
ss.setdefault("export_path", None)
ss.setdefault("llm_log", [])

# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.title("🍲 Meal Plan Generator")
    profiles = list_profiles()
    if not profiles:
        st.error("No house profiles in data/house_profiles/. Add a JSON file matching the schema (see Profile tab).")
        st.stop()
    house_id = st.selectbox("1. Select house", list(profiles.keys()), format_func=lambda k: json.loads(profiles[k].read_text())["display_name"])
    profile = load_profile(house_id)
    mock_default = bool(cfg["llm"]["mock"]) or not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    mock = st.toggle("Mock / offline mode (no API calls)", value=mock_default,
                     help="Uses sample inventory + last plan and the heuristic assembler. Turn off once ANTHROPIC_API_KEY is set.")
    llm = LLM(mock=mock)
    if not mock and not llm.available:
        st.warning("ANTHROPIC_API_KEY is not set - API calls will fail. Export it or use mock mode.")
    st.divider()
    guests = st.number_input("Guests (added to residents)", 0, 10, 0)
    servings = profile.n_residents + int(guests)
    st.caption(f"Servings for quantity math: **{servings}** ({profile.n_residents} residents + {guests} guests)")
    start_date = st.date_input("Plan start date", value=dt.date.today() + dt.timedelta(days=1))
    use_llm = st.toggle("Assemble with Claude + Epicure MCP", value=not mock, disabled=mock,
                        help="Off = deterministic heuristic assembler only (fast, free).")
    st.divider()
    tw = cfg["time_windows"]
    st.caption(f"Time windows: **{tw['mode']}** ({tw['scope']}, {tw['aggregation']}) - "
               f"B/L {tw['windows']['Breakfast']} · D {tw['windows']['Dinner']} min")
    st.caption(f"Rotation reuse cap: **{int(float(cfg['plan']['rotation_reuse_pct'])*100)}%** · max gravy/meal: {cfg['plan']['max_gravy_per_meal']}")
    st.caption(f"Model: `{cfg['llm']['model']}` · Epicure: {'on' if cfg['epicure_mcp']['enabled'] else 'off'}")
    if st.button("Reload config.yaml"):
        reload_config()
        st.rerun()

tab_profile, tab_inputs, tab_attrs, tab_generate, tab_export = st.tabs(
    ["2. Profile", "3. Inputs (inventory + last plan)", "Dish attributes", "4-5. Generate & review", "6. Export"])

# ---------------------------------------------------------------- profile
with tab_profile:
    c1, c2 = st.columns([2, 1])
    with c1:
        st.subheader(profile.display_name)
        st.caption(profile.location)
        st.markdown(f"**Residents:** " + ", ".join(r.name for r in profile.residents) +
                    (f" · **Pets:** {', '.join(p.kind for p in profile.pets)}" if profile.pets else ""))
        st.markdown(f"**Cook:** {profile.cook.name} - {profile.cook.schedule} · off: {', '.join(profile.cook.days_off) or '-'}")
        st.markdown(f"**Diet:** {profile.diet}")
        st.markdown("**Hard exclusions:** " + ", ".join(profile.hard_exclusions_ingredients))
        with st.expander("Per-person rules", expanded=True):
            st.dataframe(pd.DataFrame([pr.model_dump() for pr in profile.person_rules]), use_container_width=True, hide_index=True)
        with st.expander("Slot bans / dish rules / caps"):
            st.json({"slot_ingredient_bans": [b.model_dump() for b in profile.slot_ingredient_bans],
                     "dish_keyword_rules": [r.model_dump() for r in profile.dish_keyword_rules],
                     "frequency_caps": [c.model_dump() for c in profile.frequency_caps]})
        with st.expander("Soft context passed to the LLM"):
            st.text(profile.soft_context_text())
    with c2:
        st.markdown("**Cuisine split target**")
        st.bar_chart(pd.Series(profile.cuisine_split_target))
        st.markdown("**Meal composition**")
        st.json(profile.meal_composition.model_dump())
    with st.expander("Edit rules (raw JSON - saved to data/house_profiles)"):
        raw = st.text_area("profile.json", value=profile.model_dump_json(indent=2), height=400, key="profile_json")
        cc1, cc2 = st.columns(2)
        if cc1.button("Validate & save profile"):
            try:
                newp = HouseProfile.model_validate_json(raw)
                save_profile(newp, profiles[house_id])
                st.success("Saved. Reloading.")
                st.rerun()
            except Exception as e:  # noqa: BLE001
                st.error(f"Invalid profile: {e}")
        if cc2.download_button("Download schema (JSON Schema)", schema_json(), "house_profile.schema.json"):
            pass

# ---------------------------------------------------------------- inputs
with tab_inputs:
    st.info("Upload the inventory dashboard photo(s) and last 1-2 weeks' meal-plan photo(s). Parse, then **correct the tables** before generating.")
    col_inv, col_hist = st.columns(2)
    with col_inv:
        st.subheader("Inventory dashboard")
        inv_files = st.file_uploader("Dashboard image(s)", type=["png", "jpg", "jpeg", "webp"], accept_multiple_files=True, key="inv_up")
        if inv_files:
            st.image([f.getvalue() for f in inv_files], width=220)
        b1, b2, b3 = st.columns(3)
        if b1.button("Parse inventory", disabled=not inv_files and not mock):
            blobs = [f.getvalue() for f in inv_files] if inv_files else []
            key = image_hash(blobs) if blobs else "sample"
            cached = load_inventory_cache(key) if blobs else None
            if cached:
                ss.inventory = cached
                st.success("Loaded parsed inventory from cache (same image hash).")
            else:
                with st.spinner("Reading dashboard with Claude vision..." if not mock else "Loading sample inventory (mock)..."):
                    try:
                        ex = llm.extract_inventory(blobs)
                        ss.inventory = normalise_extraction(ex, repo, source="image" if blobs else "sample")
                        if blobs:
                            for i, f in enumerate(inv_files):
                                (cfg.path("uploads") / f"inventory_{key}_{i}{Path(f.name).suffix}").write_bytes(f.getvalue())
                            save_inventory_cache(key, ss.inventory)
                    except Exception as e:  # noqa: BLE001
                        st.error(f"Inventory extraction failed: {e}")
        csv_up = b2.file_uploader("...or upload CSV", type=["csv"], key="inv_csv", label_visibility="collapsed")
        if csv_up is not None and b2.button("Load CSV"):
            ss.inventory = Inventory.from_frame(pd.read_csv(csv_up), repo, source=csv_up.name)
        if b3.button("Use sample inventory"):
            ss.inventory = normalise_extraction(llm._mock_inventory(), repo, source="sample")
        if ss.inventory is not None:
            for w in ss.inventory.warnings:
                st.warning(w)
            st.caption("Edit freely: item / canonical / category / quantity / unit. `match` shows how the name was mapped to the repo (exact/fuzzy/keyword/none).")
            edited = st.data_editor(ss.inventory.to_frame(), num_rows="dynamic", use_container_width=True, height=420, key="inv_editor",
                                    column_config={"category": st.column_config.SelectboxColumn(options=["Vegetable", "Vegetable - aromatic", "Fruit", "Dairy", "Egg", "Bread", "Chicken", "Seafood", "Mutton", "Staple"]),
                                                   "perishable": st.column_config.CheckboxColumn(disabled=True),
                                                   "track_only": st.column_config.CheckboxColumn(disabled=True)})
            if st.button("Apply inventory corrections"):
                ss.inventory = Inventory.from_frame(edited, repo, source=ss.inventory.source + "+edited")
                st.success(f"Inventory updated: {len(ss.inventory.items)} items, {len(ss.inventory.perishables())} core perishables.")
            per = ss.inventory.perishables()
            st.caption(f"{len(ss.inventory.items)} items · {len(per)} core perishables driving the plan · "
                       f"{len(ss.inventory.perishables(True)) - len(per)} tracked-only (aromatics/long-life)")
    with col_hist:
        st.subheader("Last meal plan(s)")
        hist_files = st.file_uploader("Last 1-2 weeks' plan image(s)", type=["png", "jpg", "jpeg", "webp"], accept_multiple_files=True, key="hist_up")
        if hist_files:
            st.image([f.getvalue() for f in hist_files], width=220)
        h1, h2 = st.columns(2)
        if h1.button("Parse last plan", disabled=not hist_files and not mock):
            blobs = [f.getvalue() for f in hist_files] if hist_files else []
            key = image_hash(blobs) if blobs else "sample"
            cached = load_history_cache(key) if blobs else None
            if cached:
                ss.history = cached
                st.success("Loaded parsed last plan from cache.")
            else:
                with st.spinner("Reading last plan with Claude vision..." if not mock else "Loading sample last plan (mock)..."):
                    try:
                        ex = llm.extract_last_plan(blobs)
                        ss.history = build_history(ex, repo)
                        if blobs:
                            for i, f in enumerate(hist_files):
                                (cfg.path("uploads") / f"lastplan_{key}_{i}{Path(f.name).suffix}").write_bytes(f.getvalue())
                            save_history_cache(key, ss.history)
                    except Exception as e:  # noqa: BLE001
                        st.error(f"Last-plan extraction failed: {e}")
        if h2.button("Use sample last plan"):
            ss.history = build_history(llm._mock_last_plan(), repo)
        if ss.history is not None:
            for w in ss.history.warnings:
                st.warning(w)
            st.caption("Correct `matched_dish` to the exact repo dish name (unmatched rows do not count for rotation).")
            hdf = st.data_editor(ss.history.to_frame(), num_rows="dynamic", use_container_width=True, height=300, key="hist_editor",
                                 column_config={"matched_dish": st.column_config.SelectboxColumn(options=[""] + repo.names),
                                                "meal_slot": st.column_config.SelectboxColumn(options=SLOTS)})
            with st.expander("Format template (mirrored in Sheet 1)", expanded=False):
                t = ss.history.template
                tc1, tc2, tc3 = st.columns(3)
                orientation = tc1.selectbox("Orientation", ["days_as_rows", "days_as_columns"], index=0 if t.orientation != "days_as_columns" else 1)
                day_style = tc2.text_input("Day label style", t.day_label_style)
                sep = tc3.text_input("Dish separator", t.dish_separator)
                links = tc1.checkbox("Include YouTube links", t.includes_links)
                notes = tc2.checkbox("Notes column", t.includes_notes_column)
                headers = tc3.text_input("Slot headers (comma-separated)", ", ".join(t.slot_headers))
                style_notes = st.text_input("Style notes", t.style_notes)
            if st.button("Apply last-plan corrections"):
                tmpl = FormatTemplate(orientation=orientation, day_label_style=day_style, dish_separator=sep, includes_links=links,
                                      includes_notes_column=notes, slot_headers=[h.strip() for h in headers.split(",") if h.strip()], style_notes=style_notes)
                ss.history = PlanHistory.from_frame(hdf, tmpl, repo)
                st.success(f"History updated: {len(ss.history.recent_dishes())} matched dishes for rotation.")

# ---------------------------------------------------------------- attributes
with tab_attrs:
    st.subheader("Derived dish attributes (cached, operator-correctable)")
    st.caption("Diet is deterministic (ingredient categories). Slot / component / cuisine / gravy / time are heuristic until refined with Claude; "
               "your edits are locked and never overwritten. Cache: data/cache/dish_attributes.json")
    df = store.to_frame()
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Dishes", len(df))
    m2.metric("Heuristic only", int((df["source"] == "heuristic").sum()))
    m3.metric("LLM-refined", int((df["source"] == "llm").sum()))
    m4.metric("Operator-corrected", int((df["source"] == "operator").sum()))
    if cfg["time_windows"]["mode"] == "estimated":
        st.warning("Cooking times are ESTIMATES (Section 12 choice b). Every value is labelled with its source; correct any you know.")
    fcol1, fcol2, fcol3 = st.columns([2, 1, 1])
    q = fcol1.text_input("Search dish / ingredient")
    comp_f = fcol2.multiselect("Component", COMPONENTS)
    src_f = fcol3.multiselect("Source", ["heuristic", "llm", "operator"])
    view = df
    if q:
        ql = q.lower()
        hit_names = {n for n, d in repo.dishes.items() if ql in n.lower() or ql in d.ingredient_names_lower()}
        view = view[view["dish"].isin(hit_names)]
    if comp_f:
        view = view[view["component"].isin(comp_f)]
    if src_f:
        view = view[view["source"].isin(src_f)]
    st.dataframe(view[["dish", "diet", "non_veg_kind", "protein_group", "slots", "component", "cuisine", "gravy", "est_minutes", "est_minutes_source", "confidence", "source", "needs_soaking", "needs_marination", "notes"]],
                 use_container_width=True, height=380, hide_index=True)
    st.markdown("**Correct one dish**")
    e1, e2 = st.columns([1, 2])
    dish_sel = e1.selectbox("Dish", sorted(view["dish"].tolist()) if len(view) else repo.names)
    if dish_sel:
        a = store.get(dish_sel)
        d = repo.dishes[dish_sel]
        with e2.expander(f"Ingredients of {dish_sel}", expanded=False):
            st.dataframe(pd.DataFrame([{"ingredient": i.name, "canonical": i.canonical, "category": i.category, "per adult": i.per_adult, "unit": i.unit, "class": i.cls} for i in d.ingredients]), hide_index=True, use_container_width=True)
            if d.youtube:
                st.markdown(f"[Recipe video]({d.youtube})")
        f1, f2, f3, f4, f5 = st.columns(5)
        new_slots = f1.multiselect("Slots", SLOTS, default=[s for s in a.slots if s in SLOTS])
        new_comp = f2.selectbox("Component", COMPONENTS, index=COMPONENTS.index(a.component) if a.component in COMPONENTS else 0)
        new_cuisine = f3.selectbox("Cuisine", CUISINES, index=CUISINES.index(a.cuisine) if a.cuisine in CUISINES else 0)
        new_gravy = f4.checkbox("Gravy / wet", value=a.gravy)
        new_min = f5.number_input("Prep+cook minutes", 0, 600, int(a.est_minutes or 0))
        new_diet = st.selectbox("Diet override (deterministic default shown)", ["Vegan", "Vegetarian", "Eggetarian", "Non-Veg"], index=["Vegan", "Vegetarian", "Eggetarian", "Non-Veg"].index(a.diet))
        if st.button("Save correction"):
            store.apply_operator(dish_sel, slots=new_slots or a.slots, component=new_comp, cuisine=new_cuisine, gravy=new_gravy,
                                 est_minutes=new_min or None, diet=new_diet)
            st.success(f"Saved and locked for {dish_sel}.")
            st.rerun()
    st.divider()
    st.markdown("**Refine with Claude** (batched; only dishes still on heuristics)")
    r1, r2, r3 = st.columns(3)
    n_ref = r1.number_input("Max dishes this run", 10, 1600, 200, step=10)
    scope = r2.selectbox("Scope", ["Legal candidates for this house first", "All pending dishes"])
    if r3.button("Refine attributes with Claude", disabled=mock):
        names = store.pending_llm()
        if scope.startswith("Legal") and ss.inventory is not None:
            planner_tmp = Planner(repo, store, profile, ss.inventory, ss.history, llm, servings=servings, start_date=start_date)
            from mealplan.quantities import Ledger
            legal = set()
            for slot in SLOTS:
                legal |= {c.dish for c in planner_tmp.candidates(slot, 2, Ledger(ss.inventory))}
            names = [n for n in names if n in legal] + [n for n in names if n not in legal]
        names = names[:int(n_ref)]
        bar = st.progress(0.0, text="starting...")
        done, errs = refine_with_llm(store, llm, names, progress=lambda f, t: bar.progress(f, text=t))
        ss.llm_log += llm.calls
        st.success(f"Refined {done} dishes." + (f" Errors: {errs}" if errs else ""))
        st.rerun()

# ---------------------------------------------------------------- generate
with tab_generate:
    ready = ss.inventory is not None
    if not ready:
        st.warning("Parse or load an inventory first (Inputs tab). Last plan is optional but recommended for rotation.")
    planner = Planner(repo, store, profile, ss.inventory or Inventory(), ss.history, llm, servings=servings, start_date=start_date,
                      progress=lambda m: st.toast(m)) if ready else None
    g1, g2 = st.columns([1, 3])
    if g1.button("🚀 Generate 3-day plan", type="primary", disabled=not ready):
        with st.spinner("Pre-filtering, scoring and assembling..." + (" Claude is reasoning with Epicure pairing tools; this can take a couple of minutes." if use_llm else "")):
            try:
                ss.plan = planner.generate(use_llm=use_llm)
                ss.llm_log += llm.calls
                ss.export_path = None
            except Exception as e:  # noqa: BLE001
                st.error(f"Generation failed: {e}")
    if ready and g2.button("Show legal candidate counts per slot"):
        st.json(planner.candidate_summary())
    plan: Plan | None = ss.plan
    if plan is not None and planner is not None:
        if plan.violations:
            st.error("Plan has unresolved violations: " + "; ".join(plan.violations))
        else:
            st.success(f"Valid plan · source: **{plan.source}** · {sum(len(s.dishes) for s in plan.slots)} dishes · {plan.servings} servings")
        for w in plan.warnings:
            st.caption("• " + w)
        # grid
        grid = pd.DataFrame(index=[f"Day {d}" + (f" · {plan.get(d,'Breakfast').date}" if plan.start_date else "") for d in range(1, plan.days + 1)], columns=SLOTS)
        for s in plan.slots:
            grid.loc[f"Day {s.day}" + (f" · {s.date}" if plan.start_date else ""), s.slot] = " + ".join(s.names()) + (f"  (~{s.est_minutes_total} min)" if s.est_minutes_total else "")
        st.table(grid)
        if plan.tradeoffs:
            st.info("**Trade-offs (from the assembler):** " + plan.tradeoffs)
        st.markdown("### Why each dish")
        for s in plan.slots:
            with st.expander(f"Day {s.day} · {s.slot}: {' + '.join(s.names())}" + ("  ⚠️" if s.warnings else ""), expanded=False):
                if s.rationale:
                    st.markdown(f"*{s.rationale}*")
                for w in s.warnings:
                    st.warning(w)
                rows = []
                for d in s.dishes:
                    rows.append({"dish": d.name, "component": d.component, "cuisine": d.cuisine, "diet": d.diet, "gravy": d.gravy,
                                 "protein": d.protein_group, "est min": f"{d.est_minutes} ({d.est_minutes_source})" if d.est_minutes is not None else "n/a",
                                 "perishables consumed": "; ".join(f"{p['ingredient']} {p['need']:g}{p['unit']}" for p in d.perishables if p.get("need")),
                                 "to order": ", ".join(d.missing), "flags": "; ".join(d.flags), "reused from last plan": d.in_last_plan,
                                 "video": d.youtube or ""})
                st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True,
                             column_config={"video": st.column_config.LinkColumn()})
        st.markdown("### Swap a dish")
        s1, s2, s3, s4 = st.columns([1, 1, 2, 2])
        sw_day = s1.selectbox("Day", list(range(1, plan.days + 1)))
        sw_slot = s2.selectbox("Slot", SLOTS)
        cur = plan.get(sw_day, sw_slot).names()
        sw_old = s3.selectbox("Replace", cur) if cur else None
        if sw_old:
            alts = planner.alternatives(plan, sw_day, sw_slot, sw_old)
            labels = {c.dish: f"{c.dish}  [{c.component}, {c.attrs.cuisine}, score {c.score:.1f}]" + (f" clears {', '.join(p['ingredient'] for p in c.perishables[:3])}" if c.perishables else "") for c in alts}
            sw_new = s4.selectbox("With (legal alternatives, best first)", [c.dish for c in alts], format_func=lambda n: labels.get(n, n))
            manual = st.text_input("...or type any repo dish name (validated on swap)")
            regen = st.checkbox("Regenerate downstream slots after the swap", value=False)
            if st.button("Apply swap"):
                target = repo.match_dish_name(manual) if manual.strip() else sw_new
                if not target:
                    st.error("Dish not found in repo.")
                else:
                    ss.plan = planner.swap(plan, sw_day, sw_slot, sw_old, target, regen)
                    ss.export_path = None
                    st.rerun()
        if plan.repairs:
            with st.expander("Auto-repairs applied"):
                for r in plan.repairs:
                    st.write("• " + r)
        if ss.llm_log:
            with st.expander("LLM call log"):
                st.dataframe(pd.DataFrame(ss.llm_log), hide_index=True, use_container_width=True)
        with st.expander("Plan JSON"):
            st.json(plan.to_dict(), expanded=False)

# ---------------------------------------------------------------- export
with tab_export:
    plan = ss.plan
    if plan is None:
        st.info("Generate a plan first.")
    else:
        st.write(f"Writes `{{house}}_{{date}}_mealplan.xlsx` to `{cfg.path('outputs')}` with sheets: **Meal Plan** (mirrors last-plan layout), **Perishable Mapping**, **Order List**, plus a *Why (detail)* sheet.")
        template = ss.history.template if ss.history is not None else FormatTemplate()
        st.caption(f"Layout: {template.orientation} · headers {template.slot_headers} · links {'on' if template.includes_links else 'off'}")
        if st.button("📄 Export Excel", type="primary"):
            try:
                ss.export_path = export_plan(plan, ss.inventory, repo, profile, template)
                st.success(f"Written: {ss.export_path}")
            except Exception as e:  # noqa: BLE001
                st.error(f"Export failed: {e}")
        if ss.export_path and Path(ss.export_path).exists():
            st.download_button("Download .xlsx", Path(ss.export_path).read_bytes(), file_name=Path(ss.export_path).name,
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            from mealplan.quantities import build_order_list, perishable_mapping
            pdishes = [(f"D{s.day} {s.slot}", repo.dishes[d.name]) for s in plan.slots for d in s.dishes if d.name in repo.dishes]
            st.markdown("**Perishable mapping preview**")
            st.dataframe(pd.DataFrame(perishable_mapping(pdishes, ss.inventory, plan.servings)), hide_index=True, use_container_width=True)
            st.markdown("**Order list preview**")
            st.dataframe(pd.DataFrame(build_order_list(pdishes, ss.inventory, plan.servings, {a.lower() for a in profile.always_in_stock})), hide_index=True, use_container_width=True)
