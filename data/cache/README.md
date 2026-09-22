Runtime cache (git-ignored):
- `dish_attributes.json` - derived dish attributes (diet/slot/component/cuisine/gravy/time) with provenance; operator edits are locked here.
- `inventory_<hash>.json` / `lastplan_<hash>.json` - parsed dashboard / last-plan images keyed by image hash (re-uploads are free).
Delete a file to force re-derivation.
