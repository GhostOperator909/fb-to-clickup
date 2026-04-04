"""
Discovery helper — run this once to verify what ClickUp custom fields
exist on your list and what Meta ads are visible in your account.

Usage:
    python executions/discover_fields.py

Outputs two JSON files to /tmp/:
  - tmp/clickup_fields.json  — all custom fields with their UUIDs
  - tmp/meta_ads.json        — all ads visible to your account
"""

import os
import json
from pathlib import Path
from dotenv import load_dotenv
import requests

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

CLICKUP_KEY    = os.getenv("CLICKUP_API_KEY")
META_TOKEN     = os.getenv("META_ACCESS_TOKEN")
AD_ACCOUNT_ID  = os.getenv("META_AD_ACCOUNT_ID", "").replace("act_", "")
LIST_URL       = os.getenv("CLICKUP_LIST_URL", "")
LIST_ID        = LIST_URL.rstrip("/").split("/")[-1] if LIST_URL else ""

TMP = ROOT / "tmp"
TMP.mkdir(exist_ok=True)


def discover_clickup_fields():
    url = f"https://api.clickup.com/api/v2/list/{LIST_ID}/field"
    resp = requests.get(url, headers={"Authorization": CLICKUP_KEY}, timeout=15)
    resp.raise_for_status()
    fields = resp.json().get("fields", [])
    out = [{"name": f["name"], "id": f["id"], "type": f.get("type")} for f in fields]
    path = TMP / "clickup_fields.json"
    path.write_text(json.dumps(out, indent=2))
    print(f"\n=== ClickUp Custom Fields ({len(out)} found) ===")
    for f in out:
        print(f"  [{f['type']:20}] {f['name']}")
        print(f"               ID: {f['id']}")
    print(f"\nSaved to {path}")
    return out


def discover_meta_ads():
    url = f"https://graph.facebook.com/v19.0/act_{AD_ACCOUNT_ID}/ads"
    params = {
        "access_token": META_TOKEN,
        "fields": "id,name,status,adset_id",
        "limit": 200,
    }
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        print(f"Meta error: {data['error']}")
        return []
    ads = data.get("data", [])
    path = TMP / "meta_ads.json"
    path.write_text(json.dumps(ads, indent=2))
    print(f"\n=== Meta Ads ({len(ads)} found) ===")
    for ad in ads[:30]:  # Show first 30
        print(f"  [{ad.get('status', '?'):8}] {ad['name']}")
    if len(ads) > 30:
        print(f"  ... and {len(ads) - 30} more — see {path}")
    print(f"\nSaved to {path}")
    return ads


if __name__ == "__main__":
    print("Running discovery…\n")
    discover_clickup_fields()
    discover_meta_ads()
    print("\nDone. Use these to verify the field names match the directive mapping table.")
