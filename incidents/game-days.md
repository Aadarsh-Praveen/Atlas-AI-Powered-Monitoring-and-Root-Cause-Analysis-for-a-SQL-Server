# Game day log

One row per incident run in production. **This file is part of the answer key** (it says what was
injected and when), so it lives here in `incidents/`, which Atlas never reads; the procedure itself is in
the runbook (`knowledge/runbooks/shoplite-production.md`, section 18, "Game days").

| Date | Incident | Started (UTC) | Reverted (UTC) | First alert (rule, time) | Detection delay | Dashboards showed it? | Reverted cleanly? | Ground truth files? | Notes |
|---|---|---|---|---|---|---|---|---|---|
| _(fill in)_ | | | | | | | | | |

Later phases add columns for Atlas: its diagnosis, time to diagnosis, and whether it matched the
ground truth (Phase 10 scores this from the ground-truth storage account, not from this file).
