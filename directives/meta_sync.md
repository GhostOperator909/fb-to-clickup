# Directive: Daily Meta Ads → ClickUp Sync

## Goal
Pull yesterday's Meta Ad performance metrics and write them into matching ClickUp task cards.

## Matching Logic
The Ad code (e.g. Ad01, Ad12) is extracted from the ClickUp task using a two-step lookup:
1. Check the task's **"Ad##" custom field** (short_text, ID: 3bd733ab-7ec7-461e-a957-8d07b09bd67b) first
2. If empty, fall back to regex `Ad\d+` search in the **task name**

- **Normalize**: Uppercase the code before comparing (AD01 == ad01 == Ad01)
- **Source**: Match the code against the `ad_name` field returned by Meta Insights API
- **Rule**: One-to-one. First match wins. If multiple Meta ads contain the same code, log a WARNING and use the first result.

> **Note on Meta ad naming**: Meta ads must contain the Ad code (e.g. "Ad01") somewhere in their
> `ad_name`. Current Meta ad names use date-based naming (e.g. "5.15.24 | SP1 | FTC > …").
> Add the Ad## tag to Meta ad names, OR use the `ad_id` → ClickUp custom field mapping approach.

## Meta Fields to Fetch
```
ad_id, ad_name, spend, impressions, frequency, clicks, ctr,
actions, purchase_roas, video_thruplay_watched_actions, cpp,
date_start, date_stop
```

## ClickUp Field Name → Meta Metric Mapping
(Exact names as returned by the ClickUp API for list 901324828380)

| ClickUp Field Name                     | Meta Source Key                          | Transform          |
|----------------------------------------|------------------------------------------|--------------------|
| Amount spent (USD)                     | spend                                    | float              |
| Impressions                            | impressions                              | int                |
| Frequency                              | frequency                                | float              |
| Clicks (all)                           | clicks                                   | int                |
| Outbound CTR (click-through rate)      | ctr                                      | float              |
| ThruPlays                              | actions[video_thruplay_watched_actions]  | int                |
| Purchases                              | actions[purchase]                        | int                |
| Purchases conversion value             | actions[purchase_value]                  | float              |
| Cost per purchase (USD)                | cpp                                      | float              |
| Purchase ROAS (return on ad spend)     | purchase_roas[0].value                   | float              |
| Reporting starts                       | date_start                               | date ms epoch      |
| Reporting ends                         | date_stop                                | date ms epoch      |

## Success Criteria
- All matched tasks updated with available metrics
- Summary log written to `/logs/sync_YYYYMMDD.json`
- Console output: "Synced X cards | Skipped Y (no match) | Errors Z"

## Known Field Aliases
(Updated automatically by self-annealing — do not edit manually)
See `/directives/field_aliases.md`
