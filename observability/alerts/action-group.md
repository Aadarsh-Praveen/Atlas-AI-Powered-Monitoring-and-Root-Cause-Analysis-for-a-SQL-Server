# Action group: email

Who is told when an alert fires. One action group, used by every rule. Phase 7 adds a webhook to Atlas
to this same group.

Portal: **Monitor** → **Alerts** → **Action groups** → **Create**.

| Tab | Field | Value |
|---|---|---|
| Basics | Resource group / Region | `rg-shoplite-prod` / Global |
| Basics | Action group name | `ag-shoplite-email` |
| Basics | Display name | `shoplite` (12 characters at most; shown in emails and texts) |
| Notifications | Type | **Email/SMS message/Push/Voice** |
| Notifications | Name | `email-owner` |
| Notifications | Email | your own address (it isn't written in this repo on purpose) |
| Notifications | **Enable the common alert schema** | **Yes** |
| Tags | | `project=atlas`, `env=prod`, `app=shoplite` |

**Review + create** → **Create**. Azure emails a confirmation that you've been added to the group.

**Why the common alert schema:** every alert type (log search, metric, availability) then sends its
details in the same JSON shape. Emails look consistent, and Atlas's webhook (Phase 7) parses one format.

**Test it now:** the action group → **Test action group** → sample type **Log search alerts** →
**Test**. The email arrives within a few minutes. (The full end-to-end test alert is in
`observability/test-alert.md`.)
