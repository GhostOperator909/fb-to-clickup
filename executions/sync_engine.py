"""
Meta Ads → ClickUp Sync Engine  (v2)
DOE Execution Layer

Matching strategy (in priority order):
  1. Ad## field value (exact substring in Meta ad name) — primary
  2. Title fuzzy match (word-overlap score) — fallback / proof-of-concept mode

Status lifecycle:
  launch-ready  ─► (media buyer creates & launches Meta ad)
  running - analytics  ◄─ engine flips when Meta ad goes ACTIVE
  Closed        ◄─ engine flips + sets Ad delivery=inactive when Meta ad goes PAUSED

Self-annealing:
  - Fuzzy-matches ClickUp field names if exact match fails
  - Exponential backoff on rate limits
  - Auto-paginates all lists
  - Writes alias discoveries to /directives/field_aliases.md
"""

import os
import re
import json
import time
import logging
import difflib
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

# ── Setup ────────────────────────────────────────────────────────────────────

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

CLICKUP_KEY       = os.getenv("CLICKUP_API_KEY")
META_TOKEN        = os.getenv("META_ACCESS_TOKEN")
AD_ACCOUNT_ID     = os.getenv("META_AD_ACCOUNT_ID", "").replace("act_", "")
LIST_URL          = os.getenv("CLICKUP_LIST_URL", "")
MATCH_PREFIX      = os.getenv("MATCH_PREFIX", "Ad")
DATE_PRESET       = os.getenv("DATE_PRESET", "yesterday")
LOG_LEVEL         = os.getenv("LOG_LEVEL", "INFO")

# Title-match fallback: minimum word-overlap ratio (0–1) to accept a title match
TITLE_MATCH_THRESHOLD = float(os.getenv("TITLE_MATCH_THRESHOLD", "0.35"))

LIST_ID = LIST_URL.rstrip("/").split("/")[-1] if LIST_URL else ""

CU_HEADERS = {"Authorization": CLICKUP_KEY, "Content-Type": "application/json"}
META_GRAPH = "https://graph.facebook.com/v19.0"

ALIAS_FILE = ROOT / "directives" / "field_aliases.md"
LOG_DIR    = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("sync_engine")

# ── ClickUp status names (from list discovery) ────────────────────────────
CU_STATUS_RUNNING = "running - analytics"
CU_STATUS_CLOSED  = "closed"
CU_STATUS_LAUNCH  = "launch-ready"

# Ad delivery dropdown option IDs (from the ClickUp field definition)
CU_AD_DELIVERY_FIELD_ID = "4cb0a559-b354-4f96-ab60-80435bd13794"
CU_AD_DELIVERY_OPTIONS = {
    "active":         "5d0c1212-49c4-4ade-a794-d99a2e46ca6f",
    "inactive":       "a3ecba10-5e71-494a-9281-0177c49e08b9",
    "not_delivering": "9ad57cba-159d-4ec3-8344-2f01f05f5d27",
}

# The "Ad##" custom field on each ClickUp task
CU_AD_CODE_FIELD_ID = "3bd733ab-7ec7-461e-a957-8d07b09bd67b"

# Meta effective_status values that mean "this ad is running"
META_ACTIVE_STATUSES = {"ACTIVE"}

# Meta effective_status values that mean "ad has been turned off"
META_INACTIVE_STATUSES = {"PAUSED", "ADSET_PAUSED", "CAMPAIGN_PAUSED", "ARCHIVED", "DELETED", "DISAPPROVED"}

# Words to strip when building title fingerprint for fuzzy matching
TITLE_STOP_WORDS = {
    "meta", "video", "static", "gif", "image", "ad", "ads", "copy", "v1", "v2", "v3",
    "sp", "sp1", "sp2", "sp3", "dco", "ftc", "itr", "itr1", "itr2", "itr3",
    "tof", "mof", "bof", "the", "a", "an", "and", "or", "of", "in", "to",
    "for", "with", "on", "at", "by", "from", "is", "it", "as", "be",
}


# ── Canonical field map (directive table) ───────────────────────────────────

CANONICAL_MAP = {
    "Amount spent (USD)":                       lambda r: safe_float(r.get("spend")),
    "Impressions":                              lambda r: safe_int(r.get("impressions")),
    "Frequency":                                lambda r: safe_float(r.get("frequency")),
    "Clicks (all)":                             lambda r: safe_int(r.get("clicks")),
    "Outbound CTR (click-through rate)":        lambda r: safe_float(r.get("ctr")),
    "ThruPlays":                                lambda r: extract_action(r, "video_thruplay_watched_actions"),
    "Purchases":                                lambda r: extract_action(r, "purchase"),
    "Purchases conversion value":               lambda r: extract_action(r, "purchase_value"),
    "Cost per purchase (USD)":                  lambda r: safe_float(r.get("cpp")),
    "Purchase ROAS (return on ad spend)":       lambda r: extract_roas(r),
    "Reporting starts":                         lambda r: date_to_ms(r.get("date_start")),
    "Reporting ends":                           lambda r: date_to_ms(r.get("date_stop")),
}


# ── Type helpers ─────────────────────────────────────────────────────────────

def safe_float(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None

def safe_int(v):
    try:
        return int(float(v)) if v is not None else None
    except (TypeError, ValueError):
        return None

def extract_action(row, action_type):
    for lst_key in ("actions", "video_thruplay_watched_actions"):
        for item in row.get(lst_key, []):
            if item.get("action_type") == action_type:
                return safe_float(item.get("value"))
    return None

def extract_roas(row):
    roas = row.get("purchase_roas")
    if isinstance(roas, list) and roas:
        return safe_float(roas[0].get("value"))
    return None

def date_to_ms(date_str):
    if not date_str:
        return None
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None


# ── API helpers ──────────────────────────────────────────────────────────────

def api_get(url, params=None, headers=None, retries=4, label=""):
    delay = 2
    for attempt in range(retries):
        resp = requests.get(url, params=params, headers=headers, timeout=30)
        if resp.status_code == 429:
            log.warning(f"Rate limited ({label}). Waiting {delay}s…")
            time.sleep(delay); delay *= 2; continue
        if resp.status_code >= 500:
            log.warning(f"Server error {resp.status_code} ({label}). Retrying…")
            time.sleep(delay); delay *= 2; continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"Failed after {retries} retries: {label}")

def api_post(url, payload, headers=None, retries=4, label=""):
    delay = 2
    for attempt in range(retries):
        resp = requests.post(url, json=payload, headers=headers, timeout=30)
        if resp.status_code == 429:
            log.warning(f"Rate limited ({label}). Waiting {delay}s…")
            time.sleep(delay); delay *= 2; continue
        if resp.status_code >= 500:
            log.warning(f"Server error {resp.status_code} ({label}). Retrying…")
            time.sleep(delay); delay *= 2; continue
        if not resp.ok:
            log.error(f"POST failed {resp.status_code} ({label}): {resp.text[:200]}")
            return None
        return resp.json()
    log.error(f"POST failed after {retries} retries: {label}")
    return None

def api_put(url, payload, headers=None, retries=4, label=""):
    delay = 2
    for attempt in range(retries):
        resp = requests.put(url, json=payload, headers=headers, timeout=30)
        if resp.status_code == 429:
            log.warning(f"Rate limited ({label}). Waiting {delay}s…")
            time.sleep(delay); delay *= 2; continue
        if resp.status_code >= 500:
            log.warning(f"Server error {resp.status_code} ({label}). Retrying…")
            time.sleep(delay); delay *= 2; continue
        if not resp.ok:
            log.error(f"PUT failed {resp.status_code} ({label}): {resp.text[:200]}")
            return None
        return resp.json()
    log.error(f"PUT failed after {retries} retries: {label}")
    return None


# ── Self-Annealing: Fuzzy field name matching ─────────────────────────────────

_alias_cache = {}

def load_aliases():
    if _alias_cache:
        return _alias_cache
    if not ALIAS_FILE.exists():
        return {}
    aliases = {}
    in_section = False
    for line in ALIAS_FILE.read_text().splitlines():
        if line.startswith("## Active Aliases"):
            in_section = True; continue
        if in_section and line.startswith("- "):
            parts = line[2:].split("→")
            if len(parts) == 2:
                src = parts[0].strip().strip('"')
                dst = parts[1].strip().strip('"')
                aliases[src] = dst
    _alias_cache.update(aliases)
    return aliases

def save_alias(actual_name, canonical_name):
    _alias_cache[actual_name] = canonical_name
    content = ALIAS_FILE.read_text()
    marker = "## Active Aliases"
    idx = content.find(marker)
    if idx == -1:
        return
    insert_at = content.find("\n", idx) + 1
    content = content.replace("(none yet — populated on first mismatch resolution)\n", "")
    new_line = f'- "{actual_name}" → "{canonical_name}"\n'
    if new_line not in content:
        content = content[:insert_at] + new_line + content[insert_at:]
        ALIAS_FILE.write_text(content)
        log.info(f"[ANNEAL] Saved alias: '{actual_name}' → '{canonical_name}'")

def resolve_field_name(actual_name, canonical_names):
    if actual_name in canonical_names:
        return actual_name
    aliases = load_aliases()
    if actual_name in aliases:
        return aliases[actual_name]
    matches = difflib.get_close_matches(actual_name, canonical_names, n=1, cutoff=0.6)
    if matches:
        resolved = matches[0]
        score = difflib.SequenceMatcher(None, actual_name.lower(), resolved.lower()).ratio()
        if score >= 0.8:
            log.warning(f"[ANNEAL] Fuzzy matched '{actual_name}' → '{resolved}' (score {score:.2f})")
            save_alias(actual_name, resolved)
            return resolved
    return None


# ── Title-based fuzzy matching ────────────────────────────────────────────────

def title_fingerprint(name):
    """Extract meaningful words from an ad/task name for fuzzy matching."""
    # Remove codes like S1012_, V1014_, Ad102431_, 4.3.26|, etc.
    name = re.sub(r'^[A-Z]\d+[_\s]', '', name, flags=re.IGNORECASE)  # S1012_
    name = re.sub(r'\d+\.\d+\.\d+\s*\|[^>]*>\s*', '', name)           # 4.3.26 | SP2 | DCO >
    name = re.sub(r'Ad#?\d+', '', name, flags=re.IGNORECASE)            # Ad102431
    name = re.sub(r'[_\-|>#+\*]', ' ', name)
    words = re.findall(r'[a-zA-Z0-9\+]+', name.lower())
    return set(w for w in words if w not in TITLE_STOP_WORDS and len(w) >= 3)

def title_match_score(cu_task_name, meta_ad_name):
    """0.0–1.0 overlap score between two ad names."""
    cu_fp = title_fingerprint(cu_task_name)
    meta_fp = title_fingerprint(meta_ad_name)
    if not cu_fp or not meta_fp:
        return 0.0
    intersection = cu_fp & meta_fp
    union = cu_fp | meta_fp
    return len(intersection) / len(union)

def find_best_title_match(cu_task_name, meta_ads, threshold=None):
    """
    Return (best_meta_ad, score) or (None, 0.0) if nothing exceeds threshold.
    Uses Jaccard similarity on de-stopped word tokens.
    """
    threshold = threshold if threshold is not None else TITLE_MATCH_THRESHOLD
    best_ad, best_score = None, 0.0
    for ad in meta_ads:
        score = title_match_score(cu_task_name, ad.get("ad_name", ""))
        if score > best_score:
            best_score = score
            best_ad = ad
    if best_score >= threshold:
        return best_ad, best_score
    return None, 0.0


# ── Meta API ──────────────────────────────────────────────────────────────────

def get_meta_ads_with_status():
    """
    Fetch all ads from the account with their effective_status.
    Returns list of {ad_id, ad_name, effective_status}.
    """
    if not META_TOKEN or not AD_ACCOUNT_ID:
        raise ValueError("META_ACCESS_TOKEN and META_AD_ACCOUNT_ID must be set in .env")

    all_ads = []
    url = f"{META_GRAPH}/act_{AD_ACCOUNT_ID}/ads"
    params = {
        "access_token": META_TOKEN,
        "fields": "id,name,effective_status,adset_id",
        "limit": 500,
    }
    while url:
        data = api_get(url, params=params, label="Meta Ads List")
        if "error" in data:
            err = data["error"]
            if err.get("code") in (190, 102):
                log.error("META_ACCESS_TOKEN may be expired. Refresh at Meta Developer portal.")
            raise RuntimeError(f"Meta API error: {err.get('message', data)}")
        batch = data.get("data", [])
        all_ads.extend(batch)
        url = data.get("paging", {}).get("next")
        params = None

    log.info(f"Fetched {len(all_ads)} Meta ads (with delivery status)")
    return all_ads

def get_meta_insights(ad_ids=None):
    """
    Fetch yesterday's (or DATE_PRESET) insights.
    If ad_ids provided, fetch only for those ads (more efficient).
    """
    url = f"{META_GRAPH}/act_{AD_ACCOUNT_ID}/insights"
    params = {
        "access_token": META_TOKEN,
        "date_preset": DATE_PRESET,
        "level": "ad",
        "fields": (
            "ad_id,ad_name,spend,impressions,frequency,clicks,ctr,cpp,"
            "actions,purchase_roas,video_thruplay_watched_actions,date_start,date_stop"
        ),
        "limit": 500,
    }
    if ad_ids:
        # Filter to specific ads only
        params["filtering"] = json.dumps([{"field": "ad.id", "operator": "IN", "value": list(ad_ids)}])

    all_insights = []
    while url:
        data = api_get(url, params=params, label="Meta Insights")
        if "error" in data:
            raise RuntimeError(f"Meta Insights error: {data['error'].get('message', data)}")
        batch = data.get("data", [])
        all_insights.extend(batch)
        url = data.get("paging", {}).get("next")
        params = None

    log.info(f"Fetched {len(all_insights)} Meta insight rows")
    return all_insights


# ── ClickUp API ───────────────────────────────────────────────────────────────

def get_clickup_field_map():
    url = f"https://api.clickup.com/api/v2/list/{LIST_ID}/field"
    data = api_get(url, headers=CU_HEADERS, label="ClickUp Fields")
    fields = data.get("fields", [])
    field_map = {f["name"]: f["id"] for f in fields}
    log.info(f"Discovered {len(field_map)} ClickUp custom fields")
    return field_map

def get_all_clickup_tasks():
    tasks = []
    page = 0
    while True:
        url = f"https://api.clickup.com/api/v2/list/{LIST_ID}/task"
        params = {"page": page, "include_closed": "true"}
        data = api_get(url, params=params, headers=CU_HEADERS, label=f"ClickUp Tasks (page {page})")
        batch = data.get("tasks", [])
        if not batch:
            break
        tasks.extend(batch)
        page += 1
        if len(batch) < 100:
            break
    log.info(f"Fetched {len(tasks)} ClickUp tasks")
    return tasks

def update_clickup_field(task_id, field_id, value, task_name, field_name):
    if value is None:
        log.debug(f"Skipping null for '{field_name}' on '{task_name}'")
        return False
    url = f"https://api.clickup.com/api/v2/task/{task_id}/field/{field_id}"
    result = api_post(url, {"value": value}, headers=CU_HEADERS, label=f"{task_name}/{field_name}")
    return result is not None

def update_clickup_status(task_id, status, task_name):
    """Change a task's status (e.g. 'running - analytics', 'closed')."""
    url = f"https://api.clickup.com/api/v2/task/{task_id}"
    result = api_put(url, {"status": status}, headers=CU_HEADERS, label=f"status→{status} on '{task_name}'")
    if result:
        log.info(f"[STATUS] '{task_name}' → {status}")
    return result is not None

def update_clickup_ad_delivery(task_id, delivery_key, task_name):
    """Set the 'Ad delivery' dropdown field. delivery_key: 'active'|'inactive'|'not_delivering'"""
    option_id = CU_AD_DELIVERY_OPTIONS.get(delivery_key)
    if not option_id:
        log.warning(f"Unknown delivery key '{delivery_key}'")
        return False
    return update_clickup_field(
        task_id, CU_AD_DELIVERY_FIELD_ID, option_id, task_name, "Ad delivery"
    )

def get_task_ad_code(task):
    """Extract the Ad## code from a task: check custom field first, then task name."""
    # 1. Check "Ad##" custom field
    for cf in task.get("custom_fields", []):
        if cf.get("id") == CU_AD_CODE_FIELD_ID:
            val = cf.get("value")
            if val and str(val).strip():
                return str(val).strip()
            break
    # 2. Fall back: extract from task name
    m = re.search(r'(?:Ad#?)?(\d+)', task.get("name", ""), re.IGNORECASE)
    if m:
        return m.group(1)
    return None

def get_task_status(task):
    return task.get("status", {}).get("status", "").lower()


# ── Main Sync ─────────────────────────────────────────────────────────────────

def run_sync(title_match_fallback=False):
    """
    title_match_fallback=True: use fuzzy title matching if Ad## code matching fails.
    Set via env: TITLE_MATCH_FALLBACK=1
    """
    title_match_fallback = title_match_fallback or os.getenv("TITLE_MATCH_FALLBACK", "0") == "1"
    run_start = datetime.utcnow().isoformat()

    stats = {
        "run_at": run_start,
        "date_preset": DATE_PRESET,
        "title_match_fallback": title_match_fallback,
        "synced": [],
        "status_changed": [],
        "skipped_no_code": [],
        "skipped_no_match": [],
        "errors": [],
    }

    # ── Phase 1: Discovery ───────────────────────────────────────────────────
    log.info("=== Phase 1: Discovery ===")
    meta_ads_list  = get_meta_ads_with_status()   # all ads + delivery status
    cu_field_map   = get_clickup_field_map()       # {name: uuid}
    cu_tasks       = get_all_clickup_tasks()

    canonical_names = list(CANONICAL_MAP.keys())

    # Resolve ClickUp field names → canonical → UUID
    resolved_field_map = {}
    for actual_name, uuid in cu_field_map.items():
        canonical = resolve_field_name(actual_name, canonical_names)
        if canonical:
            resolved_field_map[canonical] = uuid

    log.info(f"Resolved {len(resolved_field_map)}/{len(canonical_names)} metric fields")

    # Build Meta ad lookup by code: {code_upper: ad}
    # Code = any Ad\d+ substring found in the ad name
    code_pattern = re.compile(r'(?:Ad#?)?(\d{4,})', re.IGNORECASE)
    meta_by_code = {}
    for ad in meta_ads_list:
        m = code_pattern.search(ad.get("name", ""))
        if m:
            code = m.group(1)
            if code not in meta_by_code:
                meta_by_code[code] = ad

    log.info(f"Indexed {len(meta_by_code)} Meta ads by numeric code")

    # ── Phase 2: Fetch insights only for ads we'll actually need ────────────
    log.info("=== Phase 2: Fetching insights ===")
    # Collect all ad_ids we might need (any task with a code)
    needed_codes = set()
    for task in cu_tasks:
        code = get_task_ad_code(task)
        if code:
            needed_codes.add(code)

    needed_ad_ids = set()
    for code in needed_codes:
        ad = meta_by_code.get(code)
        if ad:
            needed_ad_ids.add(ad["id"])

    insights_rows = get_meta_insights(needed_ad_ids if needed_ad_ids else None)

    # Index insights by ad_id
    insights_by_ad_id = {}
    for row in insights_rows:
        aid = row.get("ad_id")
        if aid:
            insights_by_ad_id[aid] = row

    # Also index Meta ads by ad_id for quick lookup
    meta_by_id = {ad["id"]: ad for ad in meta_ads_list}

    # ── Phase 3: Match & Update ──────────────────────────────────────────────
    log.info("=== Phase 3: Matching & Update ===")

    for task in cu_tasks:
        task_name   = task.get("name", "")
        task_id     = task["id"]
        task_status = get_task_status(task)
        ad_code     = get_task_ad_code(task)

        # ── Find the matching Meta ad ────────────────────────────────────────
        meta_ad      = None
        match_method = None

        if ad_code:
            meta_ad = meta_by_code.get(ad_code)
            if meta_ad:
                match_method = f"code:{ad_code}"
            elif title_match_fallback:
                # Try title match as fallback
                best_ad, score = find_best_title_match(task_name, meta_ads_list)
                if best_ad:
                    meta_ad = best_ad
                    match_method = f"title-fuzzy(score={score:.2f})"
                    log.info(f"[FUZZY] '{task_name}' → '{best_ad['ad_name']}' (score={score:.2f})")
        else:
            # No code at all — only try title match if fallback is on
            if title_match_fallback:
                best_ad, score = find_best_title_match(task_name, meta_ads_list)
                if best_ad:
                    meta_ad = best_ad
                    match_method = f"title-only(score={score:.2f})"
            else:
                stats["skipped_no_code"].append(task_name)
                continue

        if not meta_ad:
            if ad_code:
                log.debug(f"No Meta match for code '{ad_code}' (task: '{task_name}')")
                stats["skipped_no_match"].append({"task": task_name, "code": ad_code})
            continue

        meta_status = meta_ad.get("effective_status", "")
        meta_is_active   = meta_status in META_ACTIVE_STATUSES
        meta_is_inactive = meta_status in META_INACTIVE_STATUSES
        task_errors = []

        # ── Status lifecycle management ──────────────────────────────────────

        # Case A: Meta ACTIVE + task is launch-ready → flip to running
        if meta_is_active and task_status == CU_STATUS_LAUNCH:
            ok = update_clickup_status(task_id, CU_STATUS_RUNNING, task_name)
            if ok:
                update_clickup_ad_delivery(task_id, "active", task_name)
                stats["status_changed"].append({
                    "task": task_name, "from": task_status, "to": CU_STATUS_RUNNING, "reason": "Meta ACTIVE"
                })

        # Case B: Meta ACTIVE + task already running → keep delivery=active, update metrics
        elif meta_is_active and task_status == CU_STATUS_RUNNING:
            update_clickup_ad_delivery(task_id, "active", task_name)

        # Case C: Meta INACTIVE + task is running → flip to Closed, set inactive
        elif meta_is_inactive and task_status == CU_STATUS_RUNNING:
            ok = update_clickup_status(task_id, CU_STATUS_CLOSED, task_name)
            if ok:
                update_clickup_ad_delivery(task_id, "inactive", task_name)
                stats["status_changed"].append({
                    "task": task_name, "from": task_status, "to": CU_STATUS_CLOSED, "reason": f"Meta {meta_status}"
                })

        # Case D: Meta INACTIVE + task already closed → update delivery field only
        elif meta_is_inactive and task_status == CU_STATUS_CLOSED:
            update_clickup_ad_delivery(task_id, "inactive", task_name)

        # Case E: Meta PAUSED + task is launch-ready → set not_delivering, leave status
        elif meta_is_inactive and task_status == CU_STATUS_LAUNCH:
            update_clickup_ad_delivery(task_id, "not_delivering", task_name)

        # ── Performance metrics update ────────────────────────────────────────
        insight_row = insights_by_ad_id.get(meta_ad.get("id"), {})

        if insight_row:
            fields_updated = 0
            for canonical_name, extractor in CANONICAL_MAP.items():
                uuid = resolved_field_map.get(canonical_name)
                if not uuid:
                    continue
                value = extractor(insight_row)
                ok = update_clickup_field(task_id, uuid, value, task_name, canonical_name)
                if ok:
                    fields_updated += 1
                else:
                    task_errors.append(canonical_name)

            entry = {
                "task":          task_name,
                "ad_code":       ad_code,
                "meta_ad":       meta_ad.get("name"),
                "meta_status":   meta_status,
                "match_method":  match_method,
                "fields_updated": fields_updated,
                "spend":         insight_row.get("spend"),
            }
            if task_errors:
                entry["field_errors"] = task_errors
                stats["errors"].append(entry)
            else:
                stats["synced"].append(entry)
                log.info(
                    f"[SYNC] {task_name[:50]} ← {meta_ad['name'][:40]} "
                    f"[{meta_status}] spend={insight_row.get('spend')} ({fields_updated} fields)"
                )
        else:
            # Ad exists but no insights for this date range — still update status/delivery
            log.info(
                f"[STATUS-ONLY] {task_name[:50]} ← {meta_ad['name'][:40]} "
                f"[{meta_status}] (no {DATE_PRESET} insights)"
            )
            stats["synced"].append({
                "task":          task_name,
                "ad_code":       ad_code,
                "meta_ad":       meta_ad.get("name"),
                "meta_status":   meta_status,
                "match_method":  match_method,
                "fields_updated": 0,
                "note":          f"no insights for {DATE_PRESET}",
            })

    # ── Write log ────────────────────────────────────────────────────────────
    log_path = LOG_DIR / f"sync_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
    log_path.write_text(json.dumps(stats, indent=2))

    summary = (
        f"\n{'='*55}\n"
        f"Sync complete | {DATE_PRESET}\n"
        f"  Synced (metrics):      {len(stats['synced'])} tasks\n"
        f"  Status changes:        {len(stats['status_changed'])} tasks\n"
        f"  Skipped (no code):     {len(stats['skipped_no_code'])} tasks\n"
        f"  Skipped (no match):    {len(stats['skipped_no_match'])} tasks\n"
        f"  Errors:                {len(stats['errors'])} tasks\n"
        f"  Log: {log_path}\n"
        f"{'='*55}"
    )
    log.info(summary)
    return stats


if __name__ == "__main__":
    import sys
    try:
        result = run_sync()
        errors = len(result.get("errors", []))
        sys.exit(1 if errors else 0)
    except Exception as e:
        log.exception(f"Fatal error: {e}")
        sys.exit(2)
