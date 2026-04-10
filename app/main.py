"""
Ad Sync by AI Simple — desktop app entry point.

Architecture:
  - Manual syncs run locally in a background thread inside this app.
  - Scheduled syncs run in the cloud via GitHub Actions (one private repo
    per customer, created via Device Flow OAuth).

Config is persisted to ~/Library/Application Support/AdSync/config.json.
Logs are persisted to ~/Library/Application Support/AdSync/logs/.
"""

from __future__ import annotations

import io
import json
import logging
import os
import platform
import re
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

APP_NAME        = "Ad Sync by AI Simple"
APP_LABEL       = "com.aisimple.adsync"
APP_VERSION     = "1.0.0"
SUPPORT_EMAIL   = "andrew@aisimple.co"
SUPPORT_PHONE   = "(315) 335-8779"

# --------------------------------------------------------------------------- #
# Path helpers
# --------------------------------------------------------------------------- #

def user_data_dir() -> Path:
    """All writable state (config, logs, aliases) lives here."""
    home = Path.home()
    if platform.system() == "Darwin":
        p = home / "Library" / "Application Support" / "AdSync"
    else:
        p = home / ".config" / "adsync"
    p.mkdir(parents=True, exist_ok=True)
    return p

CONFIG_PATH  = user_data_dir() / "config.json"
LOG_DIR      = user_data_dir() / "logs"
ALIAS_FILE   = user_data_dir() / "field_aliases.md"
LOG_DIR.mkdir(parents=True, exist_ok=True)

def app_root() -> Path:
    """
    Return the directory containing the fb-to-clickup repo files.
    - In dev:   the repo root (parent of app/)
    - In bundle: the Resources dir inside the .app (where PyInstaller puts data)
    """
    if getattr(sys, "frozen", False):
        # PyInstaller: data files are next to the executable under _MEIPASS
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

import uuid

# Top-level config schema (v2 — multi-account).
#   defaults     : ClickUp settings + sync prefs that new connections inherit
#   connections  : list of {id, name, meta_*, optional overrides, last_run_*}
#   github_*     : OAuth + repo state (shared by all connections)
#   schedule_*   : schedule shared by all opted-in connections
DEFAULT_CONFIG: dict[str, Any] = {
    "defaults": {
        "clickup_api_key":  "",
        "clickup_list_url": "",
        "date_preset":      "maximum",
        "match_prefix":     "Ad",
        "log_level":        "INFO",
    },
    "connections": [],   # list of connection dicts
    # GitHub-backed scheduled sync state
    "github_token":        "",
    "github_login":        "",
    "github_repo":         "",
    "schedule_enabled":    False,
    "schedule_frequency":  "daily",
    "schedule_time":       "09:00",
    "schedule_weekday":    1,
    "timezone":            "America/New_York",
    "setup_complete":      False,
}

DEFAULT_CONNECTION: dict[str, Any] = {
    "id":                  "",
    "name":                "",
    "meta_access_token":   "",
    "meta_ad_account_id":  "",
    # Per-connection overrides (empty string = inherit from defaults)
    "clickup_api_key":     "",
    "clickup_list_url":    "",
    "date_preset":         "",
    "match_prefix":        "",
    "log_level":           "",
    # Scheduling opt-in
    "scheduled":           False,
    # Last run state, populated by the runner
    "last_run_at":         "",
    "last_run_status":     "",     # complete | failed | cancelled
    "last_run_summary":    None,   # {synced, status_changed, skipped, errors}
}

def _migrate_v1_to_v2(data: dict[str, Any]) -> dict[str, Any]:
    """
    Migrate the original flat config (single Meta token, single ClickUp list)
    into the new {defaults, connections} schema. Idempotent — safe to call
    repeatedly. Existing v2 configs pass through untouched.
    """
    if "connections" in data and isinstance(data["connections"], list):
        # Already v2, just fill in any missing defaults
        out = {**DEFAULT_CONFIG, **data}
        out["defaults"] = {**DEFAULT_CONFIG["defaults"], **out.get("defaults", {})}
        # Make sure each connection has every field
        out["connections"] = [
            {**DEFAULT_CONNECTION, **c} for c in out["connections"]
        ]
        return out

    # v1 → v2 migration
    out = {**DEFAULT_CONFIG}
    out["defaults"] = {
        "clickup_api_key":  data.get("clickup_api_key", "") or "",
        "clickup_list_url": data.get("clickup_list_url", "") or "",
        "date_preset":      data.get("date_preset", "maximum") or "maximum",
        "match_prefix":     data.get("match_prefix", "Ad") or "Ad",
        "log_level":        data.get("log_level", "INFO") or "INFO",
    }
    out["connections"] = []
    if data.get("meta_access_token") and data.get("meta_ad_account_id"):
        out["connections"].append({
            **DEFAULT_CONNECTION,
            "id":   "default",
            "name": "Default",
            "meta_access_token":  data.get("meta_access_token", ""),
            "meta_ad_account_id": data.get("meta_ad_account_id", ""),
            "scheduled": True,
        })
    # Carry over GitHub + schedule state
    for k in ("github_token", "github_login", "github_repo", "schedule_enabled",
              "schedule_frequency", "schedule_time", "schedule_weekday",
              "timezone", "setup_complete"):
        if k in data:
            out[k] = data[k]
    return out

def load_config() -> dict[str, Any]:
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text())
            return _migrate_v1_to_v2(data)
        except (json.JSONDecodeError, OSError):
            pass
    return {**DEFAULT_CONFIG, "defaults": dict(DEFAULT_CONFIG["defaults"])}

def save_config(cfg: dict[str, Any]) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass

# --------------------------------------------------------------------------- #
# Connection helpers
# --------------------------------------------------------------------------- #

def get_effective_config(cfg: dict[str, Any], connection_id: str) -> dict[str, Any]:
    """
    Build the flat config dict the sync engine expects, by merging the global
    defaults with a specific connection's overrides. Returns None if not found.
    """
    conn = next((c for c in cfg.get("connections", []) if c.get("id") == connection_id), None)
    if not conn:
        return None
    defaults = cfg.get("defaults", {})
    def pick(key: str) -> str:
        v = conn.get(key) or ""
        return v if v else defaults.get(key, "")
    return {
        "meta_access_token":  conn.get("meta_access_token", ""),
        "meta_ad_account_id": conn.get("meta_ad_account_id", ""),
        "clickup_api_key":    pick("clickup_api_key"),
        "clickup_list_url":   pick("clickup_list_url"),
        "date_preset":        pick("date_preset") or "maximum",
        "match_prefix":       pick("match_prefix") or "Ad",
        "log_level":          pick("log_level") or "INFO",
    }

def list_connections(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a UI-safe summary of every connection (no secrets)."""
    out = []
    for c in cfg.get("connections", []):
        out.append({
            "id":               c.get("id", ""),
            "name":             c.get("name", ""),
            "meta_ad_account_id": c.get("meta_ad_account_id", ""),
            "meta_access_token_set": bool(c.get("meta_access_token")),
            "clickup_list_url": c.get("clickup_list_url", "") or cfg.get("defaults", {}).get("clickup_list_url", ""),
            "uses_default_clickup": not bool(c.get("clickup_list_url")),
            "scheduled":        bool(c.get("scheduled", False)),
            "last_run_at":      c.get("last_run_at", ""),
            "last_run_status":  c.get("last_run_status", ""),
            "last_run_summary": c.get("last_run_summary"),
        })
    return out

def find_connection(cfg: dict[str, Any], connection_id: str) -> dict[str, Any] | None:
    return next((c for c in cfg.get("connections", []) if c.get("id") == connection_id), None)

def upsert_connection(cfg: dict[str, Any], payload: dict[str, Any]) -> str:
    """Create or update a connection. Returns the connection id."""
    cid = (payload.get("id") or "").strip() or uuid.uuid4().hex[:8]
    existing = find_connection(cfg, cid)
    if existing:
        # Don't blank out a saved Meta token if the UI sent an empty string
        for key in ("meta_access_token",):
            if not payload.get(key):
                payload[key] = existing.get(key, "")
        existing.update({k: v for k, v in payload.items() if k in DEFAULT_CONNECTION})
        existing["id"] = cid
    else:
        new_conn = {**DEFAULT_CONNECTION}
        new_conn.update({k: v for k, v in payload.items() if k in DEFAULT_CONNECTION})
        new_conn["id"] = cid
        cfg.setdefault("connections", []).append(new_conn)
    return cid

def delete_connection_by_id(cfg: dict[str, Any], connection_id: str) -> bool:
    before = len(cfg.get("connections", []))
    cfg["connections"] = [c for c in cfg.get("connections", []) if c.get("id") != connection_id]
    return len(cfg["connections"]) < before

def update_connection_run_state(cfg: dict[str, Any], connection_id: str,
                                 status: str, summary: dict | None) -> None:
    conn = find_connection(cfg, connection_id)
    if not conn:
        return
    conn["last_run_at"]      = datetime.utcnow().isoformat() + "Z"
    conn["last_run_status"]  = status
    conn["last_run_summary"] = summary

def apply_config_to_env(cfg: dict[str, Any]) -> None:
    """Push effective config values into os.environ for the sync engine."""
    os.environ["META_ACCESS_TOKEN"]   = cfg.get("meta_access_token", "") or ""
    os.environ["META_AD_ACCOUNT_ID"]  = cfg.get("meta_ad_account_id", "") or ""
    os.environ["CLICKUP_API_KEY"]     = cfg.get("clickup_api_key", "") or ""
    os.environ["CLICKUP_LIST_URL"]    = cfg.get("clickup_list_url", "") or ""
    os.environ["DATE_PRESET"]         = cfg.get("date_preset", "maximum") or "maximum"
    os.environ["MATCH_PREFIX"]        = cfg.get("match_prefix", "Ad") or "Ad"
    os.environ["LOG_LEVEL"]           = cfg.get("log_level", "INFO") or "INFO"
    os.environ["ADSYNC_LOG_DIR"]      = str(LOG_DIR)
    os.environ["ADSYNC_ALIAS_FILE"]   = str(ALIAS_FILE)

# --------------------------------------------------------------------------- #
# Sync runner (threaded, with log streaming)
# --------------------------------------------------------------------------- #

class _ListHandler(logging.Handler):
    """A logging handler that appends records to a list (with a lock)."""
    def __init__(self, sink: list[str], lock: threading.Lock):
        super().__init__()
        self.sink = sink
        self.lock = lock

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            msg = record.getMessage()
        with self.lock:
            self.sink.append(msg)

class SyncRunner:
    def __init__(self) -> None:
        # RLock (reentrant) instead of Lock so the same thread can re-acquire
        # the lock without deadlocking. This is essential because the engine's
        # logger calls _ListHandler.emit() from inside log.info(), and emit()
        # tries to acquire the same lock that the worker thread may already
        # hold via _append() in the same call chain.
        self._lock = threading.RLock()
        self._log_lines: list[str] = []
        self._cursor = 0
        self._running = False
        self._status = "idle"    # idle | running | complete | failed | cancelled
        self._started_at: float | None = None
        self._finished_at: float | None = None
        self._last_result: dict | None = None
        self._last_error: str | None = None
        self._thread: threading.Thread | None = None
        self._cancel_event = threading.Event()
        # Which connection is currently running. Set in start(), cleared on idle.
        self._connection_id: str = ""
        self._connection_name: str = ""

    # -- public (called from API class) ------------------------------------- #

    def state(self) -> dict[str, Any]:
        with self._lock:
            elapsed = None
            if self._started_at is not None:
                end = self._finished_at or time.time()
                elapsed = int(end - self._started_at)
            return {
                "status":      self._status,
                "running":     self._running,
                "elapsed":     elapsed,
                "last_result": self._last_result,
                "last_error":  self._last_error,
                "log_count":   len(self._log_lines),
                "cancel_requested": self._cancel_event.is_set(),
                "connection_id":   self._connection_id,
                "connection_name": self._connection_name,
            }

    def drain_logs(self) -> list[str]:
        # Snapshot under the lock, but return a copy so the JS bridge never
        # holds onto a reference into our internal list.
        with self._lock:
            new_lines = list(self._log_lines[self._cursor:])
            self._cursor = len(self._log_lines)
        return new_lines

    def cancel(self) -> dict[str, Any]:
        """Set the cancel flag. The runner checks it between phases."""
        if not self._running:
            return {"ok": False, "error": "Nothing is running"}
        self._cancel_event.set()
        self._append("⚠ Cancel requested — stopping after current operation…")
        return {"ok": True}

    def clear_logs(self) -> None:
        with self._lock:
            self._log_lines.clear()
            self._cursor = 0
            self._last_result = None
            self._last_error = None

    def start(self, cfg: dict[str, Any], dry_run: bool = False,
              connection_id: str = "", connection_name: str = "") -> dict[str, Any]:
        with self._lock:
            if self._running:
                return {"ok": False, "error": "A sync is already running"}
            self._running = True
            self._status = "running"
            self._started_at = time.time()
            self._finished_at = None
            self._log_lines.clear()
            self._cursor = 0
            self._last_result = None
            self._last_error = None
            self._cancel_event.clear()
            self._connection_id = connection_id
            self._connection_name = connection_name
        # Give the UI an immediate heartbeat so the log pane never looks frozen.
        label = f" [{connection_name}]" if connection_name else ""
        self._append(
            f"TEST RUN{label} — no writes will be sent to ClickUp"
            if dry_run else f"Sync requested{label} — starting worker thread"
        )
        self._thread = threading.Thread(target=self._run, args=(cfg, dry_run), daemon=True)
        self._thread.start()
        return {"ok": True}

    # -- internal ------------------------------------------------------------ #

    def _append(self, line: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            self._log_lines.append(f"{stamp}  {line}")

    def _run(self, cfg: dict[str, Any], dry_run: bool = False) -> None:
        try:
            self._append("Applying config → environment")
            apply_config_to_env(cfg)

            if self._cancel_event.is_set():
                self._append("Cancelled before engine load.")
                with self._lock:
                    self._status = "cancelled"
                return

            # Make sure the repo root is on sys.path so `executions.sync_engine` imports.
            root = app_root()
            if str(root) not in sys.path:
                sys.path.insert(0, str(root))

            # Import (or reload) the engine so it re-captures the env we just set.
            self._append("Loading sync engine module")
            import importlib
            if "executions.sync_engine" in sys.modules:
                engine = importlib.reload(sys.modules["executions.sync_engine"])
            else:
                engine = importlib.import_module("executions.sync_engine")

            # Attach our log handler to the engine's logger.
            handler = _ListHandler(self._log_lines, self._lock)
            handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
            engine.log.addHandler(handler)
            engine.log.setLevel(getattr(logging, cfg.get("log_level", "INFO"), logging.INFO))
            # Also capture root logger so 3rd-party noise (requests etc) shows up
            logging.getLogger().addHandler(handler)
            # Don't double-emit: stop the engine's named logger from
            # propagating to the root logger, or every record fires the
            # handler twice (once on the named logger, once on root).
            engine.log.propagate = False

            # ── Cancellation: wrap engine writes so they abort fast ─────────
            # Both real and dry-run paths share this wrapper, which raises a
            # sentinel exception that bubbles up out of run_sync() so the
            # matching loop can exit between tasks.
            cancel_event = self._cancel_event

            class _Cancelled(Exception):
                pass

            _real_field_update  = engine.update_clickup_field
            _real_status_update = engine.update_clickup_status
            _real_delivery_update = engine.update_clickup_ad_delivery

            def _check_cancel():
                if cancel_event.is_set():
                    raise _Cancelled()

            if dry_run:
                def _fake_field_update(task_id, field_id, value, task_name, field_name, value_options=None):
                    _check_cancel()
                    engine.log.info(
                        f"[DRY-RUN] would write '{field_name}' = {value} "
                        f"→ task '{task_name}' ({task_id})"
                    )
                    return True
                def _fake_status_update(task_id, status, task_name):
                    _check_cancel()
                    engine.log.info(
                        f"[DRY-RUN] would change status → '{status}' on '{task_name}' ({task_id})"
                    )
                    return True
                def _fake_delivery_update(task_id, delivery_key, task_name):
                    _check_cancel()
                    engine.log.info(
                        f"[DRY-RUN] would set Ad delivery → '{delivery_key}' on '{task_name}' ({task_id})"
                    )
                    return True
                engine.update_clickup_field       = _fake_field_update
                engine.update_clickup_status      = _fake_status_update
                engine.update_clickup_ad_delivery = _fake_delivery_update
            else:
                # Real run: still wrap to insert cancel checks before each write.
                def _wrapped_field_update(task_id, field_id, value, task_name, field_name, value_options=None):
                    _check_cancel()
                    return _real_field_update(task_id, field_id, value, task_name, field_name, value_options=value_options)
                def _wrapped_status_update(task_id, status, task_name):
                    _check_cancel()
                    return _real_status_update(task_id, status, task_name)
                def _wrapped_delivery_update(task_id, delivery_key, task_name):
                    _check_cancel()
                    return _real_delivery_update(task_id, delivery_key, task_name)
                engine.update_clickup_field       = _wrapped_field_update
                engine.update_clickup_status      = _wrapped_status_update
                engine.update_clickup_ad_delivery = _wrapped_delivery_update

            self._append(
                f"{'Test run' if dry_run else 'Starting sync'} — date preset: {cfg.get('date_preset')}"
            )

            cancelled = False
            try:
                result = engine.run_sync()
            except _Cancelled:
                cancelled = True
                result = {}
                self._append("✖ Sync cancelled by user.")
            finally:
                engine.update_clickup_field       = _real_field_update
                engine.update_clickup_status      = _real_status_update
                engine.update_clickup_ad_delivery = _real_delivery_update

            if cancelled:
                with self._lock:
                    self._status = "cancelled"
                    self._last_result = None
                if not dry_run and self._connection_id:
                    self._persist_run("cancelled", None)
                return
            self._append(
                f"Sync finished — synced={len(result.get('synced',[]))} "
                f"status_changes={len(result.get('status_changed',[]))} "
                f"skipped={len(result.get('skipped_no_match',[])) + len(result.get('skipped_no_code',[]))} "
                f"errors={len(result.get('errors',[]))}"
            )
            with self._lock:
                self._last_result = {
                    "synced":         len(result.get("synced", [])),
                    "status_changed": len(result.get("status_changed", [])),
                    "skipped":        len(result.get("skipped_no_match", [])) + len(result.get("skipped_no_code", [])),
                    "errors":         len(result.get("errors", [])),
                }
                self._status = "failed" if self._last_result["errors"] else "complete"
            # Persist last-run state to the connection (real runs only — dry runs
            # don't touch ClickUp so they shouldn't overwrite the real history).
            if not dry_run and self._connection_id:
                self._persist_run(self._status, self._last_result)
        except Exception as e:
            tb = traceback.format_exc()
            self._append(f"FATAL: {e}")
            for line in tb.splitlines():
                self._append(line)
            with self._lock:
                self._status = "failed"
                self._last_error = str(e)
            if not dry_run and self._connection_id:
                self._persist_run("failed", None)
        finally:
            with self._lock:
                self._running = False
                self._finished_at = time.time()

    def _persist_run(self, status: str, summary: dict | None) -> None:
        """Update the connection's last_run_* fields on disk."""
        try:
            cfg = load_config()
            update_connection_run_state(cfg, self._connection_id, status, summary)
            save_config(cfg)
        except Exception as e:
            # Don't crash the runner if persistence fails — just log it.
            self._append(f"⚠ Could not persist run state: {e}")

RUNNER = SyncRunner()

# --------------------------------------------------------------------------- #
# Source files that get uploaded to the customer's GitHub repo
# --------------------------------------------------------------------------- #

_SYNC_REPO_FILES = [
    "executions/sync_engine.py",
    "directives/meta_sync.md",
    "directives/field_aliases.md",
    "requirements.txt",
]

def _sync_engine_files() -> dict[str, bytes]:
    """
    Read the files we need to ship to the customer's private GitHub repo.
    In dev, reads from the repo root. In a bundle, reads from the bundled
    Resources dir that PyInstaller creates.
    """
    root = app_root()
    files: dict[str, bytes] = {}
    for rel in _SYNC_REPO_FILES:
        p = root / rel
        if p.exists():
            files[rel] = p.read_bytes()
    return files

# --------------------------------------------------------------------------- #
# Credential test helpers (used by the wizard "Test" buttons)
# --------------------------------------------------------------------------- #

def test_meta(token: str, account_id: str) -> dict[str, Any]:
    import urllib.request, urllib.error
    account_id = (account_id or "").replace("act_", "").strip()
    if not token or not account_id:
        return {"ok": False, "error": "Missing token or ad account ID"}
    url = (
        f"https://graph.facebook.com/v21.0/act_{account_id}"
        f"?fields=name,account_status,currency&access_token={token}"
    )
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            data = json.loads(r.read())
        return {
            "ok": True,
            "account_name": data.get("name"),
            "status":       data.get("account_status"),
            "currency":     data.get("currency"),
        }
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode())
            msg = body.get("error", {}).get("message", str(e))
        except Exception:
            msg = str(e)
        return {"ok": False, "error": msg}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def verify_setup(cfg: dict[str, Any]) -> dict[str, Any]:
    """
    Live end-to-end readiness check. Validates every step of the sync pipeline
    and returns a structured report the UI can render step-by-step.

    Steps:
      1. Meta token works + ad account accessible
      2. ClickUp token works + list accessible
      3. ClickUp custom fields map to every canonical metric the sync writes
      4. At least one Meta ad found with spend in the selected date preset
      5. At least one matching ClickUp task found for a Meta ad
      6. Live write-test: write the ad's spend to the matched task and read it back
    """
    report: dict[str, Any] = {"ok": True, "steps": []}

    def step(name: str, ok: bool, detail: str = "", data: dict | None = None):
        entry = {"name": name, "ok": ok, "detail": detail}
        if data:
            entry["data"] = data
        report["steps"].append(entry)
        if not ok:
            report["ok"] = False
        return ok

    # Step 1 — Meta
    meta = test_meta(cfg.get("meta_access_token", ""), cfg.get("meta_ad_account_id", ""))
    if not step(
        "Meta credentials",
        meta.get("ok", False),
        f"{meta.get('account_name')} ({meta.get('currency')})" if meta.get("ok") else meta.get("error", "failed"),
    ):
        return report

    # Step 2 — ClickUp
    cu = test_clickup(cfg.get("clickup_api_key", ""), cfg.get("clickup_list_url", ""))
    if not step(
        "ClickUp credentials",
        cu.get("ok", False),
        f"{cu.get('list_name')} — {cu.get('space')}" if cu.get("ok") else cu.get("error", "failed"),
    ):
        return report

    # Need the sync engine on sys.path for the next steps
    try:
        apply_config_to_env(cfg)
        root = app_root()
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        import importlib
        if "executions.sync_engine" in sys.modules:
            engine = importlib.reload(sys.modules["executions.sync_engine"])
        else:
            engine = importlib.import_module("executions.sync_engine")
    except Exception as e:
        step("Load sync engine", False, str(e), {"traceback": traceback.format_exc()})
        return report

    # Step 3 — Field mapping coverage
    try:
        cu_field_map = engine.get_clickup_field_map()
        canonical_names = list(engine.CANONICAL_MAP.keys())
        resolved = {}
        missing = []
        for name in canonical_names:
            target = engine.resolve_field_name(name, list(cu_field_map.keys())) or (
                name if name in cu_field_map else None
            )
            if target and target in cu_field_map:
                resolved[name] = {"clickup_name": target, "field_id": cu_field_map[target]}
            elif name in cu_field_map:
                resolved[name] = {"clickup_name": name, "field_id": cu_field_map[name]}
            else:
                missing.append(name)
        covered_pct = int(100 * len(resolved) / max(1, len(canonical_names)))
        step(
            "ClickUp field mapping",
            len(resolved) > 0,
            f"{len(resolved)}/{len(canonical_names)} metric fields mapped ({covered_pct}%)"
            + (f" — missing: {', '.join(missing)}" if missing else ""),
            {"resolved": resolved, "missing": missing},
        )
    except Exception as e:
        step("ClickUp field mapping", False, str(e), {"traceback": traceback.format_exc()})
        return report

    # Step 4 — Meta ads with insights
    try:
        meta_ads = engine.get_meta_ads_with_status()
        insights = engine.get_meta_insights()
        if not meta_ads:
            step("Meta ads", False, "No ads returned from Meta for this account")
            return report
        if not insights:
            step(
                "Meta insights",
                False,
                f"Meta returned {len(meta_ads)} ads but no insights for date preset '{cfg.get('date_preset')}'",
            )
            return report
        step(
            "Meta ads & insights",
            True,
            f"{len(meta_ads)} ads visible, {len(insights)} insight rows for '{cfg.get('date_preset')}'",
        )
    except Exception as e:
        step("Meta ads & insights", False, str(e), {"traceback": traceback.format_exc()})
        return report

    # Step 5 — Find a matching task
    try:
        cu_tasks = engine.get_all_clickup_tasks()
        # Use the engine's full-code matcher (preserves prefix letters so
        # P1002 ↔ TV1002 don't collide on '1002')
        meta_by_code = {}
        for ad in meta_ads:
            code = engine.get_meta_ad_code(ad.get("name", ""))
            if code and code not in meta_by_code:
                meta_by_code[code] = ad

        match = None
        for task in cu_tasks:
            # Skip non-Meta tasks (Vibe / TikTok / Pintrest) so we never
            # write Meta data to the wrong platform's card.
            if not engine.task_is_meta(task):
                continue
            code = engine.get_task_ad_code(task)
            if code and code in meta_by_code:
                insight = next((row for row in insights if row.get("ad_id") == meta_by_code[code]["id"]), None)
                if insight:
                    match = {"task": task, "meta_ad": meta_by_code[code], "insight": insight, "code": code}
                    break

        if not match:
            task_codes = []
            task_name_samples = []
            for task in cu_tasks[:200]:
                c = engine.get_task_ad_code(task)
                if c:
                    task_codes.append(c)
                if len(task_name_samples) < 5:
                    task_name_samples.append(task.get("name", "")[:80])
            step(
                "Task ↔ Meta ad matching",
                False,
                f"Scanned {len(cu_tasks)} ClickUp tasks and {len(meta_by_code)} Meta ad codes — no matches with insights yet",
                {
                    "tasks_scanned":      len(cu_tasks),
                    "meta_codes_indexed": len(meta_by_code),
                    "sample_task_codes":  sorted(set(task_codes))[:15],
                    "sample_meta_codes":  sorted(meta_by_code.keys())[:15],
                    "sample_task_names":  task_name_samples,
                    "sample_meta_ad_names": [ad.get("name", "")[:80] for ad in meta_ads[:5]],
                },
            )
            return report

        step(
            "Task ↔ Meta ad matching",
            True,
            f"Matched task '{match['task']['name'][:50]}' ↔ Meta ad '{match['meta_ad']['name'][:50]}' (code {match['code']})",
            {
                "task_name":   match["task"]["name"],
                "task_id":     match["task"]["id"],
                "meta_ad":     match["meta_ad"]["name"],
                "ad_code":     match["code"],
                "sample_spend": match["insight"].get("spend"),
            },
        )
    except Exception as e:
        step("Task ↔ Meta ad matching", False, str(e), {"traceback": traceback.format_exc()})
        return report

    # Step 6 — Live write test: write Amount spent (USD) to the matched task and read it back
    try:
        amount_field_id = None
        for canonical_name, meta_data in resolved.items():
            if canonical_name == "Amount spent (USD)":
                amount_field_id = meta_data["field_id"]
                break
        if not amount_field_id:
            step(
                "Live write-test",
                False,
                "'Amount spent (USD)' field not present on the ClickUp list — can't run write test",
            )
            return report

        spend_value = float(match["insight"].get("spend") or 0)
        ok = engine.update_clickup_field(
            match["task"]["id"],
            amount_field_id,
            spend_value,
            match["task"]["name"],
            "Amount spent (USD)",
        )
        if not ok:
            step("Live write-test", False, "ClickUp rejected the write to 'Amount spent (USD)'")
            return report

        # Read it back to confirm
        import urllib.request
        req = urllib.request.Request(
            f"https://api.clickup.com/api/v2/task/{match['task']['id']}",
            headers={"Authorization": cfg.get("clickup_api_key", "")},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            task_after = json.loads(r.read())
        wrote = None
        for cf in task_after.get("custom_fields", []):
            if cf.get("id") == amount_field_id:
                wrote = cf.get("value")
                break
        step(
            "Live write-test",
            wrote is not None,
            f"Wrote spend={spend_value} to '{match['task']['name'][:50]}' — read back: {wrote}",
            {"wrote": spend_value, "read_back": wrote, "task_id": match["task"]["id"]},
        )
    except Exception as e:
        step("Live write-test", False, str(e), {"traceback": traceback.format_exc()})
        return report

    return report


def list_meta_ad_accounts(token: str) -> dict[str, Any]:
    """
    Return every ad account the given Meta access token can see.
    Uses /me/adaccounts which works for both user tokens and system user tokens.
    """
    import urllib.request, urllib.error
    if not token:
        return {"ok": False, "error": "Missing Meta access token"}
    accounts = []
    url = (
        "https://graph.facebook.com/v21.0/me/adaccounts"
        f"?fields=account_id,name,currency,account_status&limit=200&access_token={token}"
    )
    try:
        while url:
            with urllib.request.urlopen(url, timeout=20) as r:
                data = json.loads(r.read())
            for a in data.get("data", []):
                accounts.append({
                    "id":       a.get("account_id", ""),
                    "name":     a.get("name", ""),
                    "currency": a.get("currency", ""),
                    "status":   a.get("account_status", 0),
                })
            url = data.get("paging", {}).get("next")
        # Sort by name for nicer display
        accounts.sort(key=lambda x: (x.get("name") or "").lower())
        return {"ok": True, "accounts": accounts}
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode())
            msg = body.get("error", {}).get("message", str(e))
        except Exception:
            msg = str(e)
        return {"ok": False, "error": msg}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def list_clickup_lists(api_key: str) -> dict[str, Any]:
    """
    Walk every workspace → space → folder → list the given ClickUp API key
    can see, plus folderless lists. Returns a flat array sorted by full path
    so the UI can render a single dropdown like
    'Brello — E-commerce > Creative Process > Creative Process List'.
    """
    import urllib.request, urllib.error
    if not api_key:
        return {"ok": False, "error": "Missing ClickUp API key"}

    headers = {"Authorization": api_key}

    def _get(url: str) -> dict:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())

    out = []
    try:
        teams = _get("https://api.clickup.com/api/v2/team").get("teams", [])
        for team in teams:
            team_name = team.get("name") or team.get("id")
            spaces = _get(
                f"https://api.clickup.com/api/v2/team/{team['id']}/space?archived=false"
            ).get("spaces", [])
            for sp in spaces:
                sp_name = sp.get("name", "")
                # Folders + their lists
                folders = _get(
                    f"https://api.clickup.com/api/v2/space/{sp['id']}/folder?archived=false"
                ).get("folders", [])
                for f in folders:
                    f_name = f.get("name", "")
                    for li in f.get("lists", []) or []:
                        out.append({
                            "id":   li["id"],
                            "name": li.get("name", ""),
                            "path": f"{team_name} > {sp_name} > {f_name} > {li.get('name','')}",
                            "url":  f"https://app.clickup.com/{team['id']}/v/li/{li['id']}",
                        })
                # Folderless lists in the space
                folderless = _get(
                    f"https://api.clickup.com/api/v2/space/{sp['id']}/list?archived=false"
                ).get("lists", [])
                for li in folderless:
                    out.append({
                        "id":   li["id"],
                        "name": li.get("name", ""),
                        "path": f"{team_name} > {sp_name} > {li.get('name','')}",
                        "url":  f"https://app.clickup.com/{team['id']}/v/li/{li['id']}",
                    })
        out.sort(key=lambda x: x["path"].lower())
        return {"ok": True, "lists": out}
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode())
            msg = body.get("err") or body.get("error") or str(body)
        except Exception:
            msg = str(e)
        return {"ok": False, "error": msg}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def test_clickup(api_key: str, list_url: str) -> dict[str, Any]:
    import urllib.request, urllib.error
    if not api_key or not list_url:
        return {"ok": False, "error": "Missing API key or list URL"}
    m = re.search(r'6-(\d+)-', list_url)
    list_id = m.group(1) if m else list_url.rstrip("/").split("/")[-1]
    if not list_id.isdigit():
        return {"ok": False, "error": f"Could not extract numeric list ID from URL"}
    req = urllib.request.Request(
        f"https://api.clickup.com/api/v2/list/{list_id}",
        headers={"Authorization": api_key},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        return {
            "ok":        True,
            "list_id":   list_id,
            "list_name": data.get("name"),
            "folder":    data.get("folder", {}).get("name"),
            "space":     data.get("space", {}).get("name"),
        }
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode())
            msg = body.get("err") or body.get("error") or str(body)
        except Exception:
            msg = str(e)
        return {"ok": False, "error": msg}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# --------------------------------------------------------------------------- #
# API class exposed to the JS frontend via pywebview
# --------------------------------------------------------------------------- #

class API:
    # --- defaults + global config --------------------------------------- #
    def get_defaults(self):
        cfg = load_config()
        d = cfg.get("defaults", {})
        return {
            "clickup_api_key":  "",  # never echo secrets
            "clickup_api_key_set": bool(d.get("clickup_api_key")),
            "clickup_list_url": d.get("clickup_list_url", ""),
            "date_preset":      d.get("date_preset", "maximum"),
            "match_prefix":     d.get("match_prefix", "Ad"),
            "log_level":        d.get("log_level", "INFO"),
        }

    def save_defaults(self, payload: dict):
        cfg = load_config()
        d = cfg.setdefault("defaults", {})
        for key, value in (payload or {}).items():
            if key == "clickup_api_key" and not value:
                continue  # don't blank out secret
            if key in DEFAULT_CONFIG["defaults"]:
                d[key] = value
        save_config(cfg)
        return {"ok": True}

    def get_setup_state(self):
        cfg = load_config()
        return {
            "setup_complete": bool(cfg.get("setup_complete")),
            "connection_count": len(cfg.get("connections", [])),
        }

    def mark_setup_complete(self):
        cfg = load_config()
        cfg["setup_complete"] = True
        save_config(cfg)
        return {"ok": True}

    def get_app_info(self):
        return {
            "name":           APP_NAME,
            "version":        APP_VERSION,
            "support_email":  SUPPORT_EMAIL,
            "support_phone":  SUPPORT_PHONE,
            "config_path":    str(CONFIG_PATH),
            "log_dir":        str(LOG_DIR),
        }

    # --- connections ---------------------------------------------------- #
    def list_connections(self):
        cfg = load_config()
        return list_connections(cfg)

    def get_connection(self, connection_id: str):
        cfg = load_config()
        conn = find_connection(cfg, connection_id)
        if not conn:
            return None
        # Mask secrets
        return {
            **conn,
            "meta_access_token": "",
            "meta_access_token_set": bool(conn.get("meta_access_token")),
            "clickup_api_key": "",
            "clickup_api_key_set": bool(conn.get("clickup_api_key")),
        }

    def save_connection(self, payload: dict):
        cfg = load_config()
        cid = upsert_connection(cfg, payload or {})
        # First connection saved completes setup
        if cfg.get("connections"):
            cfg["setup_complete"] = True
        save_config(cfg)
        return {"ok": True, "id": cid}

    def delete_connection(self, connection_id: str):
        cfg = load_config()
        ok = delete_connection_by_id(cfg, connection_id)
        save_config(cfg)
        return {"ok": ok}

    def set_connection_scheduled(self, connection_id: str, scheduled: bool):
        cfg = load_config()
        conn = find_connection(cfg, connection_id)
        if not conn:
            return {"ok": False, "error": "Connection not found"}
        conn["scheduled"] = bool(scheduled)
        save_config(cfg)
        return {"ok": True}

    # --- credential tests (unchanged — caller passes raw token + URL) ---- #
    def test_meta(self, token: str, account_id: str):
        return test_meta(token, account_id)

    def test_clickup(self, api_key: str, list_url: str):
        return test_clickup(api_key, list_url)

    # --- account / list discovery (powers the modal dropdowns) ----------- #
    def list_meta_ad_accounts(self, token: str = "", connection_id: str = ""):
        """
        Return every Meta ad account the given token can see.
        If `token` is empty and `connection_id` is provided, use the saved
        token from that connection (so the user doesn't have to re-paste it
        when editing an existing connection).
        """
        if not token and connection_id:
            cfg = load_config()
            conn = find_connection(cfg, connection_id)
            if conn:
                token = conn.get("meta_access_token", "")
        if not token:
            return {"ok": False, "error": "No Meta token available"}
        return list_meta_ad_accounts(token)

    def list_clickup_lists(self, api_key: str = "", connection_id: str = ""):
        """
        Return every ClickUp list visible to the given API key.
        Resolution order for the key:
          1. The `api_key` argument (if non-empty)
          2. The saved key on `connection_id` (if provided)
          3. The Defaults clickup_api_key
        """
        if not api_key:
            cfg = load_config()
            if connection_id:
                conn = find_connection(cfg, connection_id)
                if conn and conn.get("clickup_api_key"):
                    api_key = conn["clickup_api_key"]
            if not api_key:
                api_key = cfg.get("defaults", {}).get("clickup_api_key", "")
        if not api_key:
            return {"ok": False, "error": "No ClickUp API key available — set one in Defaults or paste one above"}
        return list_clickup_lists(api_key)

    def verify_connection(self, connection_id: str):
        """Live end-to-end readiness check for one connection."""
        cfg = load_config()
        eff = get_effective_config(cfg, connection_id)
        if not eff:
            return {"ok": False, "error": "Connection not found", "steps": []}
        report = verify_setup(eff)
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            (LOG_DIR / f"verify_{connection_id}_{stamp}.json").write_text(
                json.dumps(report, indent=2, default=str)
            )
        except Exception:
            pass
        return report

    def list_timezones(self):
        """Common timezones for the Schedule tab dropdown."""
        return [
            {"id": "America/New_York",    "label": "Eastern Time (ET)"},
            {"id": "America/Chicago",     "label": "Central Time (CT)"},
            {"id": "America/Denver",      "label": "Mountain Time (MT)"},
            {"id": "America/Phoenix",     "label": "Arizona (MST, no DST)"},
            {"id": "America/Los_Angeles", "label": "Pacific Time (PT)"},
            {"id": "America/Anchorage",   "label": "Alaska Time (AKT)"},
            {"id": "Pacific/Honolulu",    "label": "Hawaii (HST)"},
            {"id": "UTC",                 "label": "UTC"},
            {"id": "Europe/London",       "label": "London (GMT/BST)"},
            {"id": "Europe/Paris",        "label": "Central European (CET/CEST)"},
            {"id": "Asia/Tokyo",          "label": "Tokyo (JST)"},
            {"id": "Australia/Sydney",    "label": "Sydney (AEST/AEDT)"},
        ]

    # --- sync runner ---------------------------------------------------- #
    def run_sync(self, connection_id: str, dry_run: bool = False):
        cfg = load_config()
        eff = get_effective_config(cfg, connection_id)
        if not eff:
            return {"ok": False, "error": "Connection not found"}
        missing = [k for k in ("meta_access_token", "meta_ad_account_id", "clickup_api_key", "clickup_list_url") if not eff.get(k)]
        if missing:
            return {"ok": False, "error": f"Missing config for this connection: {', '.join(missing)}"}
        conn = find_connection(cfg, connection_id) or {}
        return RUNNER.start(
            eff,
            dry_run=bool(dry_run),
            connection_id=connection_id,
            connection_name=conn.get("name", connection_id),
        )

    def get_run_state(self):
        return RUNNER.state()

    def drain_logs(self):
        return RUNNER.drain_logs()

    def clear_logs(self):
        RUNNER.clear_logs()
        return {"ok": True}

    def cancel_sync(self):
        return RUNNER.cancel()

    # --- scheduler (GitHub Actions) ------------------------------------- #
    def github_start_device_flow(self):
        from github_client import start_device_flow
        return start_device_flow()

    def github_poll_device_flow(self, device_code: str, interval: int = 5):
        from github_client import poll_device_flow
        result = poll_device_flow(device_code, interval)
        if result.get("ok"):
            cfg = load_config()
            cfg["github_token"] = result["token"]
            cfg["github_login"] = result.get("login", "")
            save_config(cfg)
        return result

    def github_disconnect(self):
        cfg = load_config()
        cfg["github_token"] = ""
        cfg["github_login"] = ""
        cfg["github_repo"] = ""
        cfg["schedule_enabled"] = False
        save_config(cfg)
        return {"ok": True}

    def github_status(self):
        cfg = load_config()
        return {
            "connected":         bool(cfg.get("github_token")),
            "login":             cfg.get("github_login", ""),
            "repo":              cfg.get("github_repo", ""),
            "schedule_enabled":  cfg.get("schedule_enabled", False),
            "frequency":         cfg.get("schedule_frequency", "daily"),
            "time":              cfg.get("schedule_time", "09:00"),
            "timezone":          cfg.get("timezone", "America/New_York"),
            "weekday":           cfg.get("schedule_weekday", 1),
        }

    def install_schedule(self, schedule: dict):
        """
        Provision/refresh the customer's private GitHub repo with the sync
        code, secrets, and workflow YAML. `schedule` is {frequency, time,
        timezone, weekday}.
        """
        cfg = load_config()
        if not cfg.get("github_token"):
            return {"ok": False, "error": "GitHub is not connected"}

        for key in ("schedule_frequency", "schedule_time", "timezone", "schedule_weekday"):
            ui_key = {
                "schedule_frequency": "frequency",
                "schedule_time":      "time",
                "timezone":           "timezone",
                "schedule_weekday":   "weekday",
            }[key]
            if ui_key in schedule:
                cfg[key] = schedule[ui_key]

        from github_client import provision_schedule
        result = provision_schedule(cfg, sync_engine_source=_sync_engine_files())
        if result.get("ok"):
            cfg["github_repo"] = result.get("repo", "")
            cfg["schedule_enabled"] = True
            save_config(cfg)
        return result

    def uninstall_schedule(self):
        cfg = load_config()
        if not cfg.get("github_token") or not cfg.get("github_repo"):
            cfg["schedule_enabled"] = False
            save_config(cfg)
            return {"ok": True}
        from github_client import disable_schedule
        result = disable_schedule(cfg)
        cfg["schedule_enabled"] = False
        save_config(cfg)
        return result

    def list_runs(self, limit: int = 20):
        cfg = load_config()
        if not cfg.get("github_token") or not cfg.get("github_repo"):
            return {"ok": False, "error": "GitHub is not connected"}
        from github_client import list_workflow_runs
        return list_workflow_runs(cfg, limit=limit)

    def trigger_remote_run(self):
        """workflow_dispatch — fire a run in GitHub Actions right now."""
        cfg = load_config()
        if not cfg.get("github_token") or not cfg.get("github_repo"):
            return {"ok": False, "error": "GitHub is not connected"}
        from github_client import trigger_workflow
        return trigger_workflow(cfg)

    def open_log_dir(self):
        try:
            subprocess.Popen(["open", str(LOG_DIR)])
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

# --------------------------------------------------------------------------- #
# GUI entry
# --------------------------------------------------------------------------- #

def run_gui() -> None:
    import webview
    api = API()
    ui_path = Path(__file__).resolve().parent / "ui" / "index.html"
    if getattr(sys, "frozen", False):
        ui_path = Path(sys._MEIPASS) / "app" / "ui" / "index.html"
    window = webview.create_window(
        title=APP_NAME,
        url=str(ui_path),
        js_api=api,
        width=960,
        height=720,
        min_size=(820, 600),
        background_color="#0b0e14",
    )
    webview.start(debug=False)

def main() -> int:
    # Quiet noisy subprocess stdout when windowed build has sys.stdout = None
    if sys.stdout is None:
        sys.stdout = io.StringIO()
    if sys.stderr is None:
        sys.stderr = io.StringIO()

    run_gui()
    return 0

if __name__ == "__main__":
    sys.exit(main())
