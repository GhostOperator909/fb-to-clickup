"""
GitHub client for Ad Sync by AI Simple.

Handles:
  - OAuth Device Flow (no client secret needed, embeddable in the desktop app)
  - Auto-creating a private repo per customer
  - Committing sync engine files via the Contents API
  - Encrypting and uploading secrets via libsodium (pynacl) + Secrets API
  - Writing a workflow YAML with a cron schedule
  - Listing recent Actions runs
  - Triggering workflow_dispatch runs
"""

from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timedelta
from typing import Any

import requests

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover — Python 3.9+ has zoneinfo
    from backports.zoneinfo import ZoneInfo  # type: ignore

# --------------------------------------------------------------------------- #
# OAuth App client_id — registered once by AI Simple. See HANDOFF_GITHUB_OAUTH.md
# for one-time registration instructions. Device Flow client_ids are PUBLIC,
# not secrets, so embedding in the app is the correct pattern.
# --------------------------------------------------------------------------- #

GITHUB_CLIENT_ID = "Ov23litFOkzj5FblfN6H"

API_ROOT       = "https://api.github.com"
DEVICE_CODE_URL = "https://github.com/login/device/code"
TOKEN_URL       = "https://github.com/login/oauth/access_token"
REPO_NAME       = "adsync-config"
WORKFLOW_PATH   = ".github/workflows/sync.yml"
WORKFLOW_NAME   = "sync.yml"
DEFAULT_SCOPES  = "repo workflow"

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept":        "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

def _ok(data: dict[str, Any], **extra: Any) -> dict[str, Any]:
    out = {"ok": True, **data}
    out.update(extra)
    return out

def _err(msg: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": msg, **extra}

def _repo_full_name(cfg: dict[str, Any]) -> str:
    if cfg.get("github_repo"):
        return cfg["github_repo"]
    login = cfg.get("github_login") or ""
    return f"{login}/{REPO_NAME}" if login else ""

# --------------------------------------------------------------------------- #
# Device Flow OAuth
# --------------------------------------------------------------------------- #

def start_device_flow() -> dict[str, Any]:
    """
    Step 1 of GitHub Device Flow. Returns the user_code the client should type
    into github.com/login/device and the device_code we use to poll for the token.
    """
    if GITHUB_CLIENT_ID == "REPLACE_WITH_REAL_CLIENT_ID":
        return _err(
            "GitHub OAuth App client_id is not configured. See HANDOFF_GITHUB_OAUTH.md "
            "for one-time setup (5 min).",
        )
    try:
        r = requests.post(
            DEVICE_CODE_URL,
            data={"client_id": GITHUB_CLIENT_ID, "scope": DEFAULT_SCOPES},
            headers={"Accept": "application/json"},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            return _err(data.get("error_description") or data["error"])
        return _ok({
            "device_code":      data["device_code"],
            "user_code":        data["user_code"],
            "verification_uri": data["verification_uri"],
            "expires_in":       data.get("expires_in", 900),
            "interval":         data.get("interval", 5),
        })
    except requests.RequestException as e:
        return _err(str(e))

def poll_device_flow(device_code: str, interval: int = 5, max_wait: int = 600) -> dict[str, Any]:
    """
    Step 2 of Device Flow. Polls GitHub until the user authorizes or we time out.
    Returns the access token and the authorized user's login on success.
    """
    deadline = time.time() + max_wait
    wait = max(interval, 5)
    while time.time() < deadline:
        try:
            r = requests.post(
                TOKEN_URL,
                data={
                    "client_id":   GITHUB_CLIENT_ID,
                    "device_code": device_code,
                    "grant_type":  "urn:ietf:params:oauth:grant-type:device_code",
                },
                headers={"Accept": "application/json"},
                timeout=15,
            )
            data = r.json()
            if "access_token" in data:
                token = data["access_token"]
                # Fetch the login for display
                me = requests.get(f"{API_ROOT}/user", headers=_headers(token), timeout=15)
                login = me.json().get("login", "") if me.ok else ""
                return _ok({"token": token, "login": login})
            err = data.get("error")
            if err == "authorization_pending":
                time.sleep(wait)
                continue
            if err == "slow_down":
                wait += 5
                time.sleep(wait)
                continue
            if err == "expired_token":
                return _err("Authorization code expired. Please try again.")
            if err == "access_denied":
                return _err("Authorization was denied.")
            if err:
                return _err(data.get("error_description") or err)
            time.sleep(wait)
        except requests.RequestException as e:
            return _err(str(e))
    return _err("Timed out waiting for GitHub authorization")

# --------------------------------------------------------------------------- #
# Repo management
# --------------------------------------------------------------------------- #

def ensure_repo(token: str, login: str, name: str = REPO_NAME) -> dict[str, Any]:
    """Create the private repo if it doesn't exist. Returns {owner, repo}."""
    check = requests.get(f"{API_ROOT}/repos/{login}/{name}", headers=_headers(token), timeout=15)
    if check.status_code == 200:
        return _ok({"owner": login, "repo": name, "full_name": f"{login}/{name}", "created": False})
    if check.status_code != 404:
        return _err(f"Repo check failed: {check.status_code} {check.text[:200]}")

    r = requests.post(
        f"{API_ROOT}/user/repos",
        headers=_headers(token),
        json={
            "name":        name,
            "description": "Ad Sync by AI Simple — automated Meta→ClickUp sync",
            "private":     True,
            "auto_init":   True,  # creates an initial commit so we have a default branch
        },
        timeout=20,
    )
    if not r.ok:
        return _err(f"Repo create failed: {r.status_code} {r.text[:200]}")
    data = r.json()
    return _ok({
        "owner":     data["owner"]["login"],
        "repo":      data["name"],
        "full_name": data["full_name"],
        "created":   True,
    })

def _get_default_branch(token: str, full_name: str) -> str:
    r = requests.get(f"{API_ROOT}/repos/{full_name}", headers=_headers(token), timeout=15)
    if r.ok:
        return r.json().get("default_branch", "main")
    return "main"

def put_file(token: str, full_name: str, path: str, content: bytes, message: str) -> dict[str, Any]:
    """Create or update a file in the repo via the Contents API."""
    # Look up existing SHA (needed for updates)
    sha = None
    existing = requests.get(
        f"{API_ROOT}/repos/{full_name}/contents/{path}",
        headers=_headers(token),
        timeout=15,
    )
    if existing.status_code == 200:
        sha = existing.json().get("sha")
        # Avoid a no-op update if the content is identical
        try:
            existing_content = base64.b64decode(existing.json().get("content", "") or "")
            if existing_content == content:
                return _ok({"path": path, "updated": False, "unchanged": True})
        except Exception:
            pass

    body: dict[str, Any] = {
        "message": message,
        "content": base64.b64encode(content).decode("ascii"),
    }
    if sha:
        body["sha"] = sha

    r = requests.put(
        f"{API_ROOT}/repos/{full_name}/contents/{path}",
        headers=_headers(token),
        json=body,
        timeout=20,
    )
    if not r.ok:
        return _err(f"PUT {path} failed: {r.status_code} {r.text[:200]}")
    return _ok({"path": path, "updated": bool(sha)})

# --------------------------------------------------------------------------- #
# Secrets (libsodium sealed box via PyNaCl)
# --------------------------------------------------------------------------- #

def _get_public_key(token: str, full_name: str) -> dict[str, Any]:
    r = requests.get(
        f"{API_ROOT}/repos/{full_name}/actions/secrets/public-key",
        headers=_headers(token),
        timeout=15,
    )
    if not r.ok:
        return _err(f"Could not fetch repo public key: {r.status_code} {r.text[:200]}")
    return _ok(r.json())

def _encrypt_secret(public_key_b64: str, value: str) -> str:
    from nacl.public import PublicKey, SealedBox  # type: ignore
    from nacl.encoding import Base64Encoder  # type: ignore
    pub = PublicKey(public_key_b64.encode("ascii"), Base64Encoder())
    sealed = SealedBox(pub)
    ciphertext = sealed.encrypt(value.encode("utf-8"))
    return base64.b64encode(ciphertext).decode("ascii")

def put_secret(token: str, full_name: str, public_key: dict[str, Any], name: str, value: str) -> dict[str, Any]:
    encrypted = _encrypt_secret(public_key["key"], value)
    r = requests.put(
        f"{API_ROOT}/repos/{full_name}/actions/secrets/{name}",
        headers=_headers(token),
        json={"encrypted_value": encrypted, "key_id": public_key["key_id"]},
        timeout=15,
    )
    if not r.ok:
        return _err(f"Secret {name} failed: {r.status_code} {r.text[:200]}")
    return _ok({"name": name})

# --------------------------------------------------------------------------- #
# Cron expression builder (converts local schedule → UTC cron)
# --------------------------------------------------------------------------- #

def build_cron(frequency: str, hhmm: str, tz: str, weekday: int) -> str:
    """
    Return a GitHub Actions cron expression (UTC).

    weekday: 1=Mon … 7=Sun (cron uses 0=Sun..6=Sat)
    Note: GitHub's cron is UTC. We snapshot the current UTC offset of the
    caller's timezone to compute the correct UTC hour. During DST transitions
    the scheduled run will drift by 1 hour until the schedule is re-saved —
    documented in the Schedule tab UI.
    """
    if frequency == "hourly":
        return "0 * * * *"
    if frequency == "six_hourly":
        return "0 */6 * * *"

    try:
        local_hour, local_minute = (int(x) for x in (hhmm or "09:00").split(":"))
    except ValueError:
        local_hour, local_minute = 9, 0

    try:
        zone = ZoneInfo(tz or "America/New_York")
    except Exception:
        zone = ZoneInfo("UTC")

    # Take today's date + the target local time, then convert to UTC
    now = datetime.now(zone)
    local_dt = now.replace(hour=local_hour, minute=local_minute, second=0, microsecond=0)
    utc_dt = local_dt.astimezone(ZoneInfo("UTC"))

    if frequency == "weekly":
        # Convert 1..7 (Mon..Sun) to 0..6 (Sun..Sat) for cron
        # Cron: Sun=0, Mon=1, … Sat=6
        # Our:  Mon=1, Tue=2, … Sun=7
        cron_weekday = (weekday % 7)  # Sun=0, Mon=1, … Sat=6
        return f"{utc_dt.minute} {utc_dt.hour} * * {cron_weekday}"

    # daily (default)
    return f"{utc_dt.minute} {utc_dt.hour} * * *"

# --------------------------------------------------------------------------- #
# Workflow YAML — multi-connection
# --------------------------------------------------------------------------- #

import re

def _secret_suffix(connection_id: str) -> str:
    """
    Sanitize a connection id into a GitHub Actions secret name suffix.
    Secret names must match [A-Z0-9_], so we uppercase and replace anything
    else with underscores.
    """
    return re.sub(r"[^A-Za-z0-9]", "_", connection_id or "default").upper()

def _step_for_connection(conn: dict[str, Any]) -> str:
    """Render a single 'sync N' step in the workflow YAML."""
    suffix = _secret_suffix(conn["id"])
    name = conn.get("name") or conn["id"]
    safe_name = name.replace('"', '\\"')
    return f"""
      - name: "Sync — {safe_name}"
        env:
          META_ACCESS_TOKEN:  ${{{{ secrets.META_ACCESS_TOKEN_{suffix} }}}}
          META_AD_ACCOUNT_ID: ${{{{ secrets.META_AD_ACCOUNT_ID_{suffix} }}}}
          CLICKUP_API_KEY:    ${{{{ secrets.CLICKUP_API_KEY_{suffix} }}}}
          CLICKUP_LIST_URL:   ${{{{ secrets.CLICKUP_LIST_URL_{suffix} }}}}
          DATE_PRESET:        ${{{{ secrets.DATE_PRESET_{suffix} }}}}
          MATCH_PREFIX:       ${{{{ secrets.MATCH_PREFIX_{suffix} }}}}
          LOG_LEVEL:          INFO
          DRY_RUN:            ${{{{ github.event.inputs.dry_run }}}}
        continue-on-error: true
        run: |
          if [ "$DRY_RUN" = "true" ]; then
            echo "=== DRY RUN — no writes will be sent to ClickUp ==="
            python -c "
          import os, sys
          os.environ['TITLE_MATCH_FALLBACK'] = '0'
          sys.path.insert(0, '.')
          from executions.sync_engine import *
          log.info('DRY RUN — discovery only, no writes')
          ads = get_meta_ads_with_status()
          fields = get_clickup_field_map()
          tasks = get_all_clickup_tasks()
          insights = get_meta_insights()
          log.info(f'Found {{len(ads)}} Meta ads, {{len(tasks)}} ClickUp tasks, {{len(insights)}} insight rows')
          log.info(f'Resolved {{len(fields)}} custom fields')
          meta_codes = {{}}
          for ad in ads:
              c = get_meta_ad_code(ad.get('name',''))
              if c: meta_codes[c] = ad
          matched = 0
          for t in tasks:
              if get_task_status(t) not in CU_SYNCABLE_STATUSES: continue
              if not task_is_meta(t): continue
              code = get_task_ad_code(t)
              if code and code in meta_codes: matched += 1
          log.info(f'Would sync {{matched}} matched tasks (no writes performed)')
          "
          else
            python executions/sync_engine.py
          fi
"""

def render_workflow_yaml(cron: str, connections: list[dict[str, Any]]) -> bytes:
    """
    Build the workflow YAML for an arbitrary number of scheduled connections.
    Each connection becomes its own step with namespaced env vars and
    continue-on-error so one failure doesn't block the rest.
    """
    if not connections:
        # Empty workflow — nothing to sync. Still install it so the user can
        # opt connections in later without having to re-provision the file.
        steps = "\n      - name: No connections scheduled\n        run: echo 'No connections opted into the schedule. Add some in the desktop app.'\n"
    else:
        steps = "".join(_step_for_connection(c) for c in connections)

    body = f"""\
# Generated by Ad Sync by AI Simple
# Edit the schedule from the desktop app — not here.
name: Ad Sync

on:
  schedule:
    - cron: '{cron}'
  workflow_dispatch:
    inputs:
      dry_run:
        description: 'Test run (no writes to ClickUp)'
        required: false
        default: 'false'
        type: choice
        options:
          - 'false'
          - 'true'

concurrency:
  group: adsync
  cancel-in-progress: false

jobs:
  sync:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'
          cache: 'pip'

      - name: Install dependencies
        run: pip install -r requirements.txt
{steps}
      - name: Upload sync logs
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: sync-logs-${{{{ github.run_id }}}}
          path: logs/
          if-no-files-found: ignore
          retention-days: 30
"""
    return body.encode("utf-8")

# --------------------------------------------------------------------------- #
# High-level orchestration called from main.py
# --------------------------------------------------------------------------- #

def provision_schedule(
    cfg: dict[str, Any],
    sync_engine_source: dict[str, bytes],
    scheduled_connections: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    End-to-end provisioning for a multi-account schedule.

    Args:
        cfg: full app config (used for github_token/login + cron settings)
        sync_engine_source: {rel_path: bytes} of sync engine files to commit
        scheduled_connections: list of dicts, each one:
            {
                "id":   "brello",
                "name": "Brello",
                "effective": {
                    "meta_access_token": "...",
                    "meta_ad_account_id": "...",
                    "clickup_api_key": "...",
                    "clickup_list_url": "...",
                    "date_preset": "maximum",
                    "match_prefix": "Ad",
                },
            }

    For each connection, secrets are pushed under names suffixed by the
    sanitized connection id (e.g. META_ACCESS_TOKEN_BRELLO). The workflow
    YAML loops over them as one step each, with continue-on-error so a
    single failing connection doesn't block the rest.
    Idempotent — safe to call repeatedly to refresh secrets/workflow.
    """
    token = cfg.get("github_token", "")
    login = cfg.get("github_login", "")
    if not token or not login:
        return _err("GitHub is not connected")

    # 1. Ensure the private repo exists
    repo = ensure_repo(token, login)
    if not repo.get("ok"):
        return repo
    full_name = repo["full_name"]

    # 2. Commit the sync engine source files
    for rel_path, content in sync_engine_source.items():
        result = put_file(token, full_name, rel_path, content, f"Update {rel_path} from Ad Sync app")
        if not result.get("ok"):
            return result

    # 3. Fetch the repo's encryption public key
    pk = _get_public_key(token, full_name)
    if not pk.get("ok"):
        return pk

    # 4. Upload one set of namespaced secrets per scheduled connection
    pushed_secrets = []
    for sc in scheduled_connections:
        suffix = _secret_suffix(sc["id"])
        eff = sc.get("effective", {})
        secrets_to_push = {
            f"META_ACCESS_TOKEN_{suffix}":  eff.get("meta_access_token", ""),
            f"META_AD_ACCOUNT_ID_{suffix}": eff.get("meta_ad_account_id", ""),
            f"CLICKUP_API_KEY_{suffix}":    eff.get("clickup_api_key", ""),
            f"CLICKUP_LIST_URL_{suffix}":   eff.get("clickup_list_url", ""),
            f"DATE_PRESET_{suffix}":        eff.get("date_preset", "maximum") or "maximum",
            f"MATCH_PREFIX_{suffix}":       eff.get("match_prefix", "Ad") or "Ad",
        }
        for name, value in secrets_to_push.items():
            if value in (None, ""):
                continue
            result = put_secret(token, full_name, pk, name, str(value))
            if not result.get("ok"):
                return result
            pushed_secrets.append(name)

    # 5. Write the workflow YAML — one step per scheduled connection
    cron = build_cron(
        cfg.get("schedule_frequency", "daily"),
        cfg.get("schedule_time", "09:00"),
        cfg.get("timezone", "America/New_York"),
        int(cfg.get("schedule_weekday", 1)),
    )
    wf_result = put_file(
        token, full_name, WORKFLOW_PATH,
        render_workflow_yaml(cron, scheduled_connections),
        f"Schedule: {cfg.get('schedule_frequency')} @ {cfg.get('schedule_time')} {cfg.get('timezone')} ({len(scheduled_connections)} connections)",
    )
    if not wf_result.get("ok"):
        return wf_result

    return _ok({
        "repo":          full_name,
        "cron":          cron,
        "url":           f"https://github.com/{full_name}",
        "connections":   len(scheduled_connections),
        "secrets_pushed": len(pushed_secrets),
    })

def disable_schedule(cfg: dict[str, Any]) -> dict[str, Any]:
    """Remove the workflow file so GitHub Actions stops running on schedule."""
    token = cfg.get("github_token", "")
    full_name = _repo_full_name(cfg)
    if not token or not full_name:
        return _ok({"noop": True})

    existing = requests.get(
        f"{API_ROOT}/repos/{full_name}/contents/{WORKFLOW_PATH}",
        headers=_headers(token),
        timeout=15,
    )
    if existing.status_code == 404:
        return _ok({"noop": True})
    if existing.status_code != 200:
        return _err(f"Could not look up workflow: {existing.status_code}")

    sha = existing.json().get("sha")
    r = requests.delete(
        f"{API_ROOT}/repos/{full_name}/contents/{WORKFLOW_PATH}",
        headers=_headers(token),
        json={"message": "Disable schedule from Ad Sync app", "sha": sha},
        timeout=15,
    )
    if not r.ok:
        return _err(f"Could not disable schedule: {r.status_code} {r.text[:200]}")
    return _ok({"disabled": True})

def list_workflow_runs(cfg: dict[str, Any], limit: int = 20) -> dict[str, Any]:
    token = cfg.get("github_token", "")
    full_name = _repo_full_name(cfg)
    if not token or not full_name:
        return _err("GitHub is not connected")
    r = requests.get(
        f"{API_ROOT}/repos/{full_name}/actions/workflows/{WORKFLOW_NAME}/runs",
        headers=_headers(token),
        params={"per_page": limit},
        timeout=15,
    )
    if r.status_code == 404:
        return _ok({"runs": [], "note": "Workflow not found yet — schedule not provisioned"})
    if not r.ok:
        return _err(f"Runs fetch failed: {r.status_code} {r.text[:200]}")
    data = r.json()
    runs = []
    for run in data.get("workflow_runs", [])[:limit]:
        runs.append({
            "id":           run["id"],
            "status":       run["status"],      # queued | in_progress | completed
            "conclusion":   run["conclusion"],  # success | failure | cancelled | None
            "created_at":   run["created_at"],
            "run_number":   run["run_number"],
            "html_url":     run["html_url"],
            "event":        run["event"],
            "duration_ms":  None,
        })
        # Compute duration if completed
        if run.get("run_started_at") and run.get("updated_at") and run["status"] == "completed":
            try:
                start = datetime.fromisoformat(run["run_started_at"].replace("Z", "+00:00"))
                end   = datetime.fromisoformat(run["updated_at"].replace("Z", "+00:00"))
                runs[-1]["duration_ms"] = int((end - start).total_seconds() * 1000)
            except Exception:
                pass
    return _ok({"runs": runs, "total": data.get("total_count", len(runs))})

def trigger_workflow(cfg: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
    token = cfg.get("github_token", "")
    full_name = _repo_full_name(cfg)
    if not token or not full_name:
        return _err("GitHub is not connected")

    default_branch = _get_default_branch(token, full_name)
    payload = {"ref": default_branch}
    if dry_run:
        payload["inputs"] = {"dry_run": "true"}
    r = requests.post(
        f"{API_ROOT}/repos/{full_name}/actions/workflows/{WORKFLOW_NAME}/dispatches",
        headers=_headers(token),
        json=payload,
        timeout=15,
    )
    if r.status_code == 204:
        return _ok({"dispatched": True, "dry_run": dry_run})
    return _err(f"Dispatch failed: {r.status_code} {r.text[:200]}")
