You are reading photo(s) of a household's previous meal plan (usually a table shared on WhatsApp/Excel: days vs Breakfast/Lunch/Dinner).

Extract two things:

1. `entries`: one row per (day, meal_slot, dish). Split combined cells like "Roti + Aloo Gobi + Dal" into separate dish rows in order. Keep the dish text as written. `day` is the label as shown (e.g. "Mon", "Day 1", "12 Sep"). `meal_slot` must be one of Breakfast, Lunch, Dinner (map Snacks/Evening to the nearest of these or drop them and mention in `warnings`).

2. `format_template`: describe the layout so it can be mirrored exactly in a spreadsheet:
   - `orientation`: "days_as_rows" (each row = a day, columns = meal slots) or "days_as_columns" (each column = a day, rows = meal slots).
   - `day_label_style`: how days are labelled, e.g. "Weekday name", "Date dd-Mon", "Day N".
   - `slot_headers`: the exact header text used for the meal columns/rows, in order.
   - `dish_separator`: how multiple dishes in one cell were separated (e.g. " + ", newline, ", ").
   - `includes_links`: true if YouTube/recipe links were present.
   - `includes_notes_column`: true if there was a notes/remarks column.
   - `extra_columns`: any other columns present (e.g. "Serves", "Prep note").
   - `style_notes`: anything else about the look (header colour, bold, merged cells).

Do not invent dishes that are not visible. If nothing is legible, return empty entries and explain in `warnings`.
