# Role: Meta-to-ClickUp Automation Orchestrator

## Architecture (DOE Framework)
- **Directives** → `/directives/` — The *What*: business rules, matching logic, field mappings
- **Executions** → `/executions/` — The *How*: Python scripts that hit the APIs
- **Environment** → `.env` — Secrets only. Never hardcoded in scripts
- **Logs** → `/logs/` — Structured JSON logs for every run
- **Tmp** → `/tmp/` — Scratch space for intermediate data

## Operational Rules
1. Always extract `LIST_ID` dynamically from `CLICKUP_LIST_URL` in `.env`
2. Always fetch ClickUp Custom Field IDs via API at runtime — never hardcode UUIDs
3. Use regex `Ad\d+` (case-insensitive) to match task names to Meta ad names
4. Never store API keys in scripts — use `os.getenv()` only
5. Log every run to `/logs/sync_YYYYMMDD.json`

## Self-Annealing Protocol
When a sync fails or partially fails, apply this recovery ladder:

1. **Field name mismatch** → Try fuzzy matching (difflib) against available ClickUp field names.
   - If confidence > 0.8: use the match, log a WARNING, update `/directives/field_aliases.md`
   - If confidence ≤ 0.8: skip field, log ERROR, add to `/tmp/unmatched_fields.txt` for review
2. **API rate limit (429)** → Exponential backoff: wait 2s, 4s, 8s, then fail with clear error
3. **Meta token expired** → Log ERROR "META_ACCESS_TOKEN may be expired. Long-lived tokens last 60 days."
4. **ClickUp task fetch incomplete** → Auto-paginate using `page` parameter until no more tasks
5. **Ad not found in Meta** → Log WARNING "No Meta ad found for [AdXX] in task [task name]", continue

## Trigger
Run by typing: **"Perform daily ad sync"**
The orchestrator reads `directives/meta_sync.md` and executes `executions/sync_engine.py`.
