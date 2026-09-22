"""Excel export: Sheet 1 'Meal Plan' (mirrors the last-plan layout), Sheet 2 'Perishable Mapping', Sheet 3 'Order List'."""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .config import load_config
from .engine import Plan
from .inventory import Inventory
from .llm import FormatTemplate
from .profiles import HouseProfile
from .quantities import build_order_list, perishable_mapping
from .repo import RecipeRepo

HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
HEADER_FONT = Font(bold=True, color="FFFFFF")
DAY_FILL = PatternFill("solid", fgColor="DDEBF7")
WARN_FILL = PatternFill("solid", fgColor="FFF2CC")
BAD_FILL = PatternFill("solid", fgColor="F8CBAD")
GOOD_FILL = PatternFill("solid", fgColor="E2EFDA")
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
WRAP = Alignment(wrap_text=True, vertical="top")


def _style_header(ws, row: int, ncols: int) -> None:
    for c in range(1, ncols + 1):
        cell = ws.cell(row=row, column=c)
        cell.fill, cell.font, cell.border = HEADER_FILL, HEADER_FONT, BORDER
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)


def _autosize(ws, widths: dict[int, int] | None = None, default: int = 18, maxw: int = 60) -> None:
    widths = widths or {}
    for col in range(1, ws.max_column + 1):
        if col in widths:
            ws.column_dimensions[get_column_letter(col)].width = widths[col]
            continue
        longest = default
        for row in ws.iter_rows(min_col=col, max_col=col):
            v = row[0].value
            if v is not None:
                longest = max(longest, min(maxw, max(len(x) for x in str(v).split("\n")) + 2))
        ws.column_dimensions[get_column_letter(col)].width = longest


def _day_label(plan: Plan, day: int, style: str) -> str:
    d = dt.date.fromisoformat(plan.start_date) + dt.timedelta(days=day - 1) if plan.start_date else None
    st = (style or "").lower()
    if d and "date" in st:
        return d.strftime("%d-%b (%a)")
    if d and ("weekday" in st or "day name" in st):
        return d.strftime("%A")
    if d:
        return f"Day {day} - {d.strftime('%a %d %b')}"
    return f"Day {day}"


def _cell_text(ms, template: FormatTemplate, repo: RecipeRepo) -> str:
    sep = template.dish_separator or " + "
    if "\\n" in sep or sep.strip() == "" and "\n" in sep:
        sep = "\n"
    names = [d.name for d in ms.dishes]
    text = sep.join(names) if names else "-"
    flags = []
    for d in ms.dishes:
        for f in d.flags:
            if "portion-only" in f or "soak" in f or "marinate" in f:
                flags.append(f"{d.name}: {f.split(' (')[0]}")
    if flags:
        text += "\n" + "\n".join("* " + f for f in flags)
    if template.includes_links:
        links = [d.youtube for d in ms.dishes if d.youtube]
        if links:
            text += "\n" + "\n".join(links)
    return text


def write_plan_sheet(ws, plan: Plan, template: FormatTemplate, repo: RecipeRepo, profile: HouseProfile) -> None:
    slots = [s for s in (template.slot_headers or ["Breakfast", "Lunch", "Dinner"])]
    canon = {"Breakfast": "Breakfast", "Lunch": "Lunch", "Dinner": "Dinner"}
    # map arbitrary header text to canonical slot
    def slot_for(header: str) -> str | None:
        h = header.lower()
        for k in canon:
            if k.lower()[:3] in h:
                return k
        return None
    headers = [h for h in slots if slot_for(h)] or ["Breakfast", "Lunch", "Dinner"]
    ws.title = "Meal Plan"
    title = f"{plan.house_name} - {plan.days}-day meal plan ({plan.servings} servings)"
    if plan.start_date:
        title += f" from {plan.start_date}"
    ws.cell(row=1, column=1, value=title).font = Font(bold=True, size=13)
    ws.cell(row=2, column=1, value=f"Generated {dt.datetime.now():%Y-%m-%d %H:%M} | source: {plan.source} | layout mirrored from last plan ({template.orientation})").font = Font(italic=True, color="666666")
    r0 = 4
    notes_col = bool(template.includes_notes_column)
    if template.orientation == "days_as_columns":
        ws.cell(row=r0, column=1, value="Meal")
        for j in range(plan.days):
            ws.cell(row=r0, column=2 + j, value=_day_label(plan, j + 1, template.day_label_style))
        _style_header(ws, r0, 1 + plan.days)
        for i, h in enumerate(headers):
            r = r0 + 1 + i
            c = ws.cell(row=r, column=1, value=h)
            c.font, c.fill, c.border, c.alignment = Font(bold=True), DAY_FILL, BORDER, WRAP
            for j in range(plan.days):
                ms = plan.get(j + 1, slot_for(h))
                cell = ws.cell(row=r, column=2 + j, value=_cell_text(ms, template, repo))
                cell.alignment, cell.border = WRAP, BORDER
                if ms.warnings:
                    cell.fill = WARN_FILL
            ws.row_dimensions[r].height = 110
        _autosize(ws, {1: 14}, default=38, maxw=60)
        last_row = r0 + len(headers)
    else:
        cols = ["Day"] + headers + (["Notes"] if notes_col else []) + list(template.extra_columns or [])
        for j, h in enumerate(cols):
            ws.cell(row=r0, column=1 + j, value=h)
        _style_header(ws, r0, len(cols))
        for i in range(plan.days):
            r = r0 + 1 + i
            c = ws.cell(row=r, column=1, value=_day_label(plan, i + 1, template.day_label_style))
            c.font, c.fill, c.border, c.alignment = Font(bold=True), DAY_FILL, BORDER, WRAP
            warns = []
            for j, h in enumerate(headers):
                ms = plan.get(i + 1, slot_for(h))
                cell = ws.cell(row=r, column=2 + j, value=_cell_text(ms, template, repo))
                cell.alignment, cell.border = WRAP, BORDER
                if ms.warnings:
                    cell.fill = WARN_FILL
                    warns += ms.warnings
            if notes_col:
                cell = ws.cell(row=r, column=2 + len(headers), value="\n".join(dict.fromkeys(warns)))
                cell.alignment, cell.border = WRAP, BORDER
            ws.row_dimensions[r].height = 120
        _autosize(ws, {1: 16}, default=40, maxw=60)
        last_row = r0 + plan.days
    ws.freeze_panes = ws.cell(row=r0 + 1, column=2)
    # standing notes below the grid
    r = last_row + 2
    ws.cell(row=r, column=1, value="Standing instructions").font = Font(bold=True)
    for k, line in enumerate(profile.non_negotiables[:8]):
        ws.cell(row=r + 1 + k, column=1, value="- " + line)
    r = r + 2 + len(profile.non_negotiables[:8])
    if plan.tradeoffs:
        ws.cell(row=r, column=1, value="Trade-offs").font = Font(bold=True)
        c = ws.cell(row=r + 1, column=1, value=plan.tradeoffs)
        c.alignment = WRAP
        ws.merge_cells(start_row=r + 1, start_column=1, end_row=r + 1, end_column=max(3, ws.max_column))
        ws.row_dimensions[r + 1].height = 70
        r += 3
    if plan.warnings:
        ws.cell(row=r, column=1, value="Notes").font = Font(bold=True)
        for k, w in enumerate(plan.warnings[:10]):
            ws.cell(row=r + 1 + k, column=1, value="- " + w)


def write_detail_sheet(ws, plan: Plan) -> None:
    """Dish-by-dish explainability (why each dish was chosen)."""
    ws.title = "Why (detail)"
    cols = ["Day", "Slot", "Dish", "Component", "Cuisine", "Diet", "Gravy", "Protein", "Est. min (source)",
            "Perishables consumed", "To order (essentials)", "Flags", "YouTube", "Slot rationale"]
    for j, h in enumerate(cols):
        ws.cell(row=1, column=1 + j, value=h)
    _style_header(ws, 1, len(cols))
    r = 2
    for ms in plan.slots:
        for d in ms.dishes:
            per = "; ".join(f"{p['ingredient']} {p['need']:g}{p['unit']}" for p in d.perishables if p.get("need"))
            vals = [ms.day, ms.slot, d.name, d.component, d.cuisine, d.diet, "yes" if d.gravy else "", d.protein_group,
                    f"{d.est_minutes} ({d.est_minutes_source})" if d.est_minutes is not None else "n/a",
                    per, ", ".join(d.missing), "; ".join(d.flags), d.youtube or "", ms.rationale]
            for j, v in enumerate(vals):
                c = ws.cell(row=r, column=1 + j, value=v)
                c.alignment, c.border = WRAP, BORDER
            r += 1
    ws.freeze_panes = "D2"
    _autosize(ws, {3: 30, 10: 45, 11: 30, 12: 40, 13: 40, 14: 60}, default=12)


def write_mapping_sheet(ws, plan: Plan, inventory: Inventory, repo: RecipeRepo) -> None:
    ws.title = "Perishable Mapping"
    plan_dishes = [(f"D{s.day} {s.slot}", repo.dishes[d.name]) for s in plan.slots for d in s.dishes if d.name in repo.dishes]
    rows = perishable_mapping(plan_dishes, inventory, plan.servings)
    cols = ["Perishable (as on dashboard)", "Canonical", "Category", "On shelf", "Unit", "Used by plan", "Left unused", "Coverage", "Consumed by (dish [day slot] qty)", "Tracked only"]
    for j, h in enumerate(cols):
        ws.cell(row=1, column=1 + j, value=h)
    _style_header(ws, 1, len(cols))
    for i, row in enumerate(rows):
        r = 2 + i
        vals = [row["perishable"], row["canonical"], row["category"], row["on_shelf"], row["unit"], row["used"], row["left_unused"],
                row["coverage"], row["consumed_by"], "yes" if row["track_only"] else ""]
        for j, v in enumerate(vals):
            c = ws.cell(row=r, column=1 + j, value=v)
            c.alignment, c.border = WRAP, BORDER
        cov = row["coverage"]
        fill = None
        if not row["track_only"]:
            if cov is None:
                fill = WARN_FILL
            elif cov >= 0.8:
                fill = GOOD_FILL
            elif cov <= 0.2:
                fill = BAD_FILL
            else:
                fill = WARN_FILL
        if fill:
            ws.cell(row=r, column=8).fill = fill
        if cov is not None:
            ws.cell(row=r, column=8).number_format = "0%"
    ws.freeze_panes = "B2"
    _autosize(ws, {9: 70}, default=14)
    # summary
    r = len(rows) + 3
    core = [x for x in rows if not x["track_only"] and x["coverage"] is not None]
    if core:
        avg = sum(x["coverage"] for x in core) / len(core)
        ws.cell(row=r, column=1, value=f"Average coverage of {len(core)} core perishables: {avg:.0%}").font = Font(bold=True)
        unused = [x["perishable"] for x in core if x["coverage"] == 0]
        if unused:
            ws.cell(row=r + 1, column=1, value="Not used by this plan: " + ", ".join(unused)).fill = BAD_FILL


def write_order_sheet(ws, plan: Plan, inventory: Inventory, repo: RecipeRepo, profile: HouseProfile) -> None:
    ws.title = "Order List"
    plan_dishes = [(f"D{s.day} {s.slot}", repo.dishes[d.name]) for s in plan.slots for d in s.dishes if d.name in repo.dishes]
    rows = build_order_list(plan_dishes, inventory, plan.servings, {a.lower() for a in profile.always_in_stock})
    # perishables / essentials first
    rows.sort(key=lambda r: (not r["essential"], r["category"] == "Staple", r["category"], r["item"]))
    cols = ["Item", "Category", "Required by plan", "On shelf", "To order", "Unit", "Essential?", "Used in", "Status / notes"]
    for j, h in enumerate(cols):
        ws.cell(row=1, column=1 + j, value=h)
    _style_header(ws, 1, len(cols))
    for i, row in enumerate(rows):
        r = 2 + i
        vals = [row["item"], row["category"], row["required"], row["on_shelf"], row["to_order"], row["unit"],
                "yes" if row["essential"] else "optional", row["used_in"], row["status"]]
        for j, v in enumerate(vals):
            c = ws.cell(row=r, column=1 + j, value=v)
            c.alignment, c.border = WRAP, BORDER
        if row["to_order"] == "check" or "check" in str(row["status"]):
            ws.cell(row=r, column=5).fill = WARN_FILL
        if not row["essential"]:
            for j in range(len(cols)):
                ws.cell(row=r, column=1 + j).font = Font(color="808080")
    ws.freeze_panes = "B2"
    _autosize(ws, {8: 55, 9: 40}, default=14)
    r = len(rows) + 3
    brands = [b for b in profile.brand_rules if b.action == "never_buy"]
    if brands:
        ws.cell(row=r, column=1, value="Brand rules: " + "; ".join(f"never buy '{b.brand}'" for b in brands)).font = Font(bold=True, color="C00000")
        r += 1
    for g in profile.ingredient_grade_rules:
        ws.cell(row=r, column=1, value=f"{g.ingredient}: {g.rule}")
        r += 1
    ws.cell(row=r + 1, column=1, value="Quantities = Recipe Master 'Per adult' x servings - on-shelf stock. 'check' = unit or quantity could not be resolved.").font = Font(italic=True, color="666666")


def export_plan(plan: Plan, inventory: Inventory, repo: RecipeRepo, profile: HouseProfile, template: FormatTemplate | None = None,
                out_dir: Path | None = None) -> Path:
    template = template or FormatTemplate()
    out_dir = out_dir or load_config().path("outputs")
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", profile.display_name).strip("_")
    date = plan.start_date or dt.date.today().isoformat()
    path = out_dir / f"{safe}_{date}_mealplan.xlsx"
    wb = Workbook()
    write_plan_sheet(wb.active, plan, template, repo, profile)
    write_mapping_sheet(wb.create_sheet(), plan, inventory, repo)
    write_order_sheet(wb.create_sheet(), plan, inventory, repo, profile)
    write_detail_sheet(wb.create_sheet(), plan)
    wb.save(path)
    return path
