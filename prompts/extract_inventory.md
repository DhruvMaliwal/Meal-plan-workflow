You are reading a photo of a household kitchen inventory dashboard (may be a spreadsheet screenshot, a handwritten note, or a table). Extract EVERY stock line into structured rows.

Rules:
- One row per item. Keep the item name as written, then also give your best canonical English ingredient name (e.g. "Tamatar" -> "Tomato", "Dahi" -> "Curd", "Bhindi" -> "Okra", "Gobi" -> "Cauliflower", "Palak" -> "Spinach", "Dhania" -> "Coriander Leaves", "Hari Mirch" -> "Green Chilli").
- Quantity: numeric if present. Units: g, kg, ml, l, pcs, bunch, packet, dozen. Convert kg -> g and l -> ml. Leave quantity null if unreadable, and say so in `notes`.
- Category must be one of: Vegetable, Vegetable - aromatic, Fruit, Dairy, Egg, Bread, Chicken, Seafood, Mutton, Staple.
  Aromatics = coriander leaves, mint, curry leaves, green chilli, ginger, garlic, spring onion.
  Staple = grains, flours, dals, spices, oils, sauces, dry goods.
- If the dashboard shows a status (e.g. "low", "out", "fresh", expiry dates), copy it into `notes`.
- Do not invent items that are not visible. If the image is not an inventory at all, return an empty list and explain in `warnings`.
