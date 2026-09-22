"""Command-line runner for quick end-to-end tests without the UI.

    python -m mealplan.cli --house gurupriyan_raksha --mock
    python -m mealplan.cli --house gurupriyan_raksha --inventory data/samples/sample_inventory.csv --start 2026-09-25
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

import pandas as pd

from .attributes import AttributeStore
from .engine import Planner
from .export import export_plan
from .history import build_history
from .inventory import Inventory, normalise_extraction
from .llm import LLM
from .profiles import list_profiles, load_profile
from .repo import get_repo


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--house", default=None, help="house id (default: first profile)")
    ap.add_argument("--mock", action="store_true", help="no API calls; sample inventory/last plan; heuristic assembly")
    ap.add_argument("--inventory", help="CSV with columns item,canonical,category,quantity,unit,notes")
    ap.add_argument("--inventory-image", nargs="*", help="dashboard image(s) to parse with Claude")
    ap.add_argument("--lastplan-image", nargs="*", help="last meal plan image(s) to parse with Claude")
    ap.add_argument("--start", help="plan start date YYYY-MM-DD")
    ap.add_argument("--servings", type=int)
    ap.add_argument("--heuristic", action="store_true", help="skip LLM assembly even if a key is set")
    args = ap.parse_args()

    repo = get_repo()
    store = AttributeStore(repo)
    store.ensure_all()
    house = args.house or next(iter(list_profiles()))
    profile = load_profile(house)
    llm = LLM(mock=args.mock or None)
    if args.inventory:
        inv = Inventory.from_frame(pd.read_csv(args.inventory), repo, source=args.inventory)
    elif args.inventory_image:
        inv = normalise_extraction(llm.extract_inventory([Path(p) for p in args.inventory_image]), repo)
    else:
        inv = normalise_extraction(llm._mock_inventory(), repo, source="sample")
    if args.lastplan_image:
        hist = build_history(llm.extract_last_plan([Path(p) for p in args.lastplan_image]), repo)
    else:
        hist = build_history(llm._mock_last_plan(), repo)
    start = dt.date.fromisoformat(args.start) if args.start else dt.date.today() + dt.timedelta(days=1)
    planner = Planner(repo, store, profile, inv, hist, llm, servings=args.servings, start_date=start, progress=print)
    print("Legal candidates per slot:", json.dumps(planner.candidate_summary()))
    plan = planner.generate(use_llm=False if (args.heuristic or llm.mock) else None)
    for s in plan.slots:
        print(f"D{s.day} {s.date} {s.slot:9} ~{s.est_minutes_total}m: " + " + ".join(s.names()))
    print("violations:", plan.violations or "none")
    print("warnings:", *plan.warnings, sep="\n  ")
    if plan.repairs:
        print("repairs:", *plan.repairs, sep="\n  ")
    out = export_plan(plan, inv, repo, profile, hist.template)
    print("Excel written:", out)


if __name__ == "__main__":
    main()
