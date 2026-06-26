# Deploying The Pulse to Cloudflare — runbook

**Approach (Option B):** GitHub Actions runs your existing Python on a schedule
(dashboard every 15 min, live every 5 min) and publishes `metrics.json` +
`live.json` to **Cloudflare Pages**. No laptop, no rewrite. Rolling history
lives in **Cloudflare KV**. The repo is **public but code-only** — your numbers
and secrets never go into it (public repos get unlimited free Actions minutes;
that's the only reason it's public).

You do **two** manual things (create two API tokens). I do everything else via
`deploy/setup.py`.

---

## Step 1 — Create a Cloudflare API token

1. Go to **https://dash.cloudflare.com/profile/api-tokens** → **Create Token**
   → **Create Custom Token**.
2. Permissions (click *+ Add more* for each):
   - **Account** · **Cloudflare Pages** · **Edit**
   - **Account** · **Workers KV Storage** · **Edit**
   - **Account** · **Account Settings** · **Read**
3. **Account Resources:** Include → your account.
4. **Continue to summary** → **Create Token** → copy it.
5. Save it to a file (this keeps it out of chat/transcripts):
   ```bash
   echo 'PASTE_CLOUDFLARE_TOKEN_HERE' > ~/.pulse_cloudflare_token
   ```

## Step 2 — Create a GitHub token

1. Go to **https://github.com/settings/tokens** → **Generate new token** →
   **Generate new token (classic)**.
2. Note: `the-pulse deploy`. Expiration: your call (90 days is fine).
3. Tick scopes: **`repo`** and **`workflow`**.
4. **Generate token** → copy it → save it:
   ```bash
   echo 'PASTE_GITHUB_TOKEN_HERE' > ~/.pulse_github_token
   ```

## Step 3 — Run the deploy

```bash
cd ~/Desktop/newsroom-dashboard
python3 deploy/setup.py
```

It creates the Cloudflare Pages project + KV namespace, creates the public
GitHub repo, pushes the code, loads all secrets, and triggers the first run.
When it finishes it prints your live URL.

> **Live deployment (this account):**
> - Dashboard: https://the-pulse-3er.pages.dev/
> - Real-Time: https://the-pulse-3er.pages.dev/live.html
> - Repo: https://github.com/aashmidha/the-pulse  · Actions tab shows every run.
>
> Cloudflare gave the project the subdomain `the-pulse-3er` (the plain
> `the-pulse` was already taken by another account). That real URL is stored in
> the GitHub Actions variable `PAGES_URL`, which the live job reads.

Optional cleanup once it succeeds:
```bash
rm ~/.pulse_cloudflare_token ~/.pulse_github_token   # tokens are now in GitHub secrets
```

---

## After it's live

- **Watch runs:** the repo's **Actions** tab. Re-run by hand anytime via
  *Run workflow* on either workflow.
- **The site is on the public internet** at `the-pulse.pages.dev`. The numbers
  are reachable by anyone with the URL. If the newsroom wants it locked down,
  the clean fix is **Cloudflare Access** (restrict to `@businesspost.ie` logins,
  with an IP-bypass rule so the wall TV loads without a login). Tell me and I'll
  set it up — it's a follow-up, not required to go live.
- **BART cookie expires** (see HANDOVER §4). When the "Most Read" / engagement
  panels go blank, refresh it: DevTools → Network → `master.php` → Request
  Headers → copy the `bartdemo=` value, then update the secret:
  ```bash
  printf '%s' 'NEW_COOKIE_VALUE' | gh secret set BART_SESSION_COOKIE --repo aashmidha/the-pulse
  ```
- **Cost:** $0. Public repo = unlimited Actions minutes; Cloudflare Pages + KV
  + the trigger Worker are all free at this volume.

## Reliable scheduling — the trigger Worker

GitHub's own cron is "best effort" and in practice drops most `*/5` / `*/15`
runs (it was firing only every 1–4h). So scheduling is driven by a small
Cloudflare Worker (`trigger-worker/`, deployed as **the-pulse-trigger**) whose
cron fires reliably every 5 min and calls GitHub's `workflow_dispatch` API:

- `*/5`  → triggers `refresh-live.yml`
- `*/15` → triggers `refresh-dashboard.yml`

The GitHub `schedule:` blocks are still in the workflows as a sparse **backstop**
(if the Worker ever stops). The Worker holds one secret, **`GH_TOKEN`** — a
GitHub fine-grained PAT scoped to this repo with **Actions: Read+Write**.

Redeploy the Worker after editing it:
```bash
export CLOUDFLARE_API_TOKEN=$(cat ~/.pulse_cloudflare_token)
export CLOUDFLARE_ACCOUNT_ID=797d9b70c94383e96ee6c21f4dd89953
npx wrangler@4 deploy --config trigger-worker/wrangler.toml
```

### ⏰ Token renewal (don't forget)
The Worker's GitHub PAT **expires** (you chose the date when creating it). When
it does, the auto-updates silently stop. To renew: regenerate the fine-grained
token (same repo, Actions: Read+Write), then:
```bash
echo 'NEW_PAT' | npx wrangler@4 secret put GH_TOKEN --config trigger-worker/wrangler.toml
```
The Cloudflare API token in GitHub secrets (`CLOUDFLARE_API_TOKEN`) was rotated
on 2026-06-26; the workers.dev subdomain is `bpie-newsroom`.
