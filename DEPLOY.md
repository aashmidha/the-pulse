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
When it finishes it prints your live URL (`https://the-pulse.pages.dev`).

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
  printf '%s' 'NEW_COOKIE_VALUE' | gh secret set BART_SESSION_COOKIE --repo <you>/the-pulse
  ```
- **Cost:** $0. Public repo = unlimited Actions minutes; Cloudflare Pages + KV
  are free at this volume.
- **Scheduler note:** GitHub cron is best-effort and can run a few minutes late
  under load, and scheduled workflows auto-pause after 60 days with no repo
  activity (a manual *Run workflow* or any push resets that).
