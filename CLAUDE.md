# The Newsroom Live — project context

Business Post newsroom wall dashboards + daily/weekly metrics logged to a Google Sheet.
Python runs in GitHub Actions → publishes `metrics.json` / `live.json` to Cloudflare Pages;
rolling state in Cloudflare KV; scheduled by a Cloudflare Worker that dispatches workflows.

- Repo `aashmidha/the-pulse` · live at https://tv.businesspost.group/ (+ `/live.html`)
- Local: `~/Desktop/newsroom-dashboard`

**👉 Before doing anything substantial, read [`HANDOFF.md`](HANDOFF.md)** — it has the architecture,
every data source + credential, how each metric is computed, the scheduling pattern, the
hard-won gotchas, and what's currently pending/in-progress. `DEPLOY.md` is the ops runbook.

Quick reminders (full detail in HANDOFF.md):
- **Always pass `--remote` to `wrangler kv key get/put`** (v4 defaults to local state).
- Engagement is now **BART-only** (`/ma/segments`, no Piano, includes B2Cd), cached daily in KV.
- Daily sheet jobs run off `refresh-dashboard.yml` once/day via a `daily_done.json` marker — not a
  narrow time window (Cloudflare drops single low-frequency cron fires).
- BART `/rfv` feed lacks `SubStatus`/`Userstatus` — get cohorts via `/ma/segments`; get per-day
  per-user reads via `/user/UID/articles` (NOT Piano Analytics — PA leaves ~85% of pageviews unattributed).
- **In progress:** switching the `B2C eng.`/`B2B eng.` "avg pageviews" from the 90-day `V` to per-day
  reads via `/user/UID/articles`; plus Engagement-tab "Day" column + duplicate-row fix. See HANDOFF §9.

Never commit secrets or real numbers (repo is public). Secrets live in GitHub Actions / the Worker;
local creds in `.dev.vars` and `~/BART List/enricher_settings.json`.
