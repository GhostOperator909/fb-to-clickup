"""
Dry run — shows what WOULD be updated without writing anything to ClickUp.
Usage: python executions/dry_run.py
"""
import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent.parent))

# Monkey-patch the update function before importing sync engine
import executions.sync_engine as engine

_real_update = engine.update_clickup_field
_dry_run_log = []

def _fake_update(task_id, field_id, value, task_name, field_name):
    _dry_run_log.append(f"  [{field_name}] = {value}")
    return True

engine.update_clickup_field = _fake_update

import json

def main():
    print("=== DRY RUN — no data will be written to ClickUp ===\n")

    meta_ads     = engine.get_meta_insights()
    cu_field_map = engine.get_clickup_field_map()
    cu_tasks     = engine.get_all_clickup_tasks()
    canonical_names = list(engine.CANONICAL_MAP.keys())

    # Resolve fields
    resolved_field_map = {}
    print("=== Field Resolution ===")
    for actual_name, uuid in cu_field_map.items():
        canonical = engine.resolve_field_name(actual_name, canonical_names)
        if canonical:
            resolved_field_map[canonical] = uuid
            print(f"  [OK] '{actual_name}' → '{canonical}'")
        else:
            print(f"  [--] '{actual_name}' (not in directive — skipped)")

    # Build meta lookup
    pattern = engine.re.compile(rf"{engine.re.escape(engine.MATCH_PREFIX)}\d+", engine.re.IGNORECASE)
    meta_lookup = {}
    for ad in meta_ads:
        ad_name = ad.get("ad_name", "")
        m = pattern.search(ad_name)
        if m:
            code = m.group(0).upper()
            if code not in meta_lookup:
                meta_lookup[code] = ad

    print(f"\n=== Meta Ad Codes Found ({len(meta_lookup)}) ===")
    if meta_lookup:
        for code in sorted(meta_lookup.keys()):
            print(f"  {code} → {meta_lookup[code].get('ad_name')}")
    else:
        print("  NONE — Meta ads do not contain 'Ad##' codes in their names.")
        print("  Add 'Ad01', 'Ad02' etc to your Meta ad names, OR")
        print("  populate the 'Ad##' ClickUp custom field on each card to match by that.")

    AD_CODE_FIELD_ID = "3bd733ab-7ec7-461e-a957-8d07b09bd67b"
    print(f"\n=== ClickUp Task Matching ===")
    matched = 0
    for task in cu_tasks:
        task_name = task.get("name", "")

        # Check Ad## custom field
        ad_code = None
        for cf in task.get("custom_fields", []):
            if cf.get("id") == AD_CODE_FIELD_ID:
                val = cf.get("value")
                if val:
                    m = pattern.search(str(val))
                    if m:
                        ad_code = m.group(0).upper()
                break

        if not ad_code:
            m = pattern.search(task_name)
            if m:
                ad_code = m.group(0).upper()

        if not ad_code:
            continue

        meta_row = meta_lookup.get(ad_code)
        if meta_row:
            print(f"  [MATCH] '{task_name}' ← code '{ad_code}' ← Meta ad '{meta_row.get('ad_name')}'")
            matched += 1
        else:
            print(f"  [MISS ] '{task_name}' has code '{ad_code}' but no Meta ad found")

    print(f"\nSummary: {matched} tasks would be updated")
    print("\nRun 'python executions/sync_engine.py' to execute the real sync.")

if __name__ == "__main__":
    main()
