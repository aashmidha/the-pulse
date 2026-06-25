#!/usr/bin/env python3
"""
One-shot deploy for The Pulse (Option B: scheduled Python -> Cloudflare Pages).

What it does (idempotent — safe to re-run):
  1. Reads your two API tokens from files in your home dir (never printed).
  2. Cloudflare: finds your account, creates a KV namespace + a Pages project.
  3. GitHub: logs in, creates a PUBLIC repo, pushes the code.
  4. Pushes every secret (from your local .dev.vars + enricher_settings.json)
     and the Pages project/URL into GitHub — so the scheduled jobs can run.
  5. Triggers the first dashboard run.

Run it from this folder:  python3 deploy/setup.py
Prereqs: see DEPLOY.md (two token files + `gh` installed).
"""
import json, os, subprocess, sys, urllib.request, urllib.error, pathlib

HOME = pathlib.Path.home()
ROOT = HOME / "Desktop" / "newsroom-dashboard"
PROJECT = os.environ.get("PAGES_PROJECT", "the-pulse")
KV_TITLE = f"{PROJECT}-state"
GH_TOKEN_FILE = HOME / ".pulse_github_token"
CF_TOKEN_FILE = HOME / ".pulse_cloudflare_token"
ENRICHER = HOME / "BART List" / "enricher_settings.json"

os.chdir(ROOT)


def step(msg): print(f"\n=== {msg} ===")
def ok(msg):   print(f"  ✓ {msg}")


def sh(cmd, inp=None, check=True):
    r = subprocess.run(cmd, input=inp, text=True, capture_output=True)
    if check and r.returncode != 0:
        sys.exit(f"FAILED: {' '.join(cmd)}\n{r.stdout}\n{r.stderr}")
    return r


def cf(path, method="GET", body=None):
    req = urllib.request.Request(
        "https://api.cloudflare.com/client/v4" + path, method=method,
        headers={"Authorization": f"Bearer {CF_TOKEN}", "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body else None)
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())


# ---- tokens -------------------------------------------------------------
# GitHub uses your existing `gh auth login` (browser) — no token file needed.
# Only the Cloudflare token file is required.
if not CF_TOKEN_FILE.exists():
    sys.exit(f"Missing token file: {CF_TOKEN_FILE}\nSee DEPLOY.md for how to create it.")
CF_TOKEN = CF_TOKEN_FILE.read_text().strip()

# ---- 1. Cloudflare account ---------------------------------------------
step("Cloudflare: account")
accs = cf("/accounts")
if not accs.get("success"):
    sys.exit(f"Cloudflare token rejected: {accs.get('errors')}")
ACC = accs["result"][0]["id"]
os.environ["CLOUDFLARE_API_TOKEN"] = CF_TOKEN
os.environ["CLOUDFLARE_ACCOUNT_ID"] = ACC
ok(f"account {ACC[:6]}…  ({accs['result'][0].get('name','')})")

# ---- 2. KV namespace ----------------------------------------------------
step("Cloudflare: KV namespace (rolling history/state)")
nss = cf(f"/accounts/{ACC}/storage/kv/namespaces?per_page=100").get("result", [])
kv = next((n for n in nss if n["title"] == KV_TITLE), None)
if not kv:
    kv = cf(f"/accounts/{ACC}/storage/kv/namespaces", "POST", {"title": KV_TITLE})["result"]
    ok(f"created namespace '{KV_TITLE}'")
else:
    ok(f"namespace '{KV_TITLE}' already exists")
KV_ID = kv["id"]

# ---- 3. Pages project ---------------------------------------------------
step("Cloudflare: Pages project")
exists = cf(f"/accounts/{ACC}/pages/projects/{PROJECT}")
if exists.get("success"):
    ok(f"project '{PROJECT}' already exists")
else:
    res = cf(f"/accounts/{ACC}/pages/projects", "POST",
             {"name": PROJECT, "production_branch": "main"})
    if not res.get("success"):
        sys.exit(f"Could not create Pages project: {res.get('errors')}")
    ok(f"created project '{PROJECT}'")
PAGES_URL = f"https://{PROJECT}.pages.dev"

# ---- 4. gather secrets from local files --------------------------------
step("Collecting secrets from your local files")
dev = {}
for line in (ROOT / ".dev.vars").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        dev[k.strip()] = v.strip().strip('"').strip("'")
enr = json.loads(ENRICHER.read_text())
secrets = {
    "PA_ACCESS_KEY": dev["PA_ACCESS_KEY"],
    "PA_SECRET_KEY": dev["PA_SECRET_KEY"],
    "PA_SITE_ID": dev["PA_SITE_ID"],
    "MANUAL_SHEET_URL": dev["MANUAL_SHEET_URL"],
    "BART_SESSION_COOKIE": dev["BART_SESSION_COOKIE"],
    "BART_NAME": dev["BART_NAME"],
    "PIANO_AID": enr["piano_aid"],
    "PIANO_API_TOKEN": enr["piano_token"],
    "PIANO_BASE_URL": enr.get("piano_base", "https://api-eu.piano.io"),
    "BART_BASE_URL": enr.get("bart_base", "https://bonnieraws.dergan.net"),
    "BART_KEY": enr["bart_key"],
    "CLOUDFLARE_API_TOKEN": CF_TOKEN,
    "CLOUDFLARE_ACCOUNT_ID": ACC,
    "CLOUDFLARE_KV_ID": KV_ID,
}
ok(f"{len(secrets)} secrets ready (values not shown)")

# ---- 5. GitHub auth + repo ---------------------------------------------
step("GitHub: using your existing browser login")
auth = sh(["gh", "auth", "status"], check=False)
if auth.returncode != 0:
    sys.exit("GitHub CLI not logged in. Run:  gh auth login  (choose 'Login with a web browser')")
USER = sh(["gh", "api", "user", "-q", ".login"]).stdout.strip()
REPO = f"{USER}/{PROJECT}"
ok(f"logged in as {USER}")

step("GitHub: commit + push (public repo, code only)")
sh(["git", "branch", "-M", "main"])
sh(["git", "add", "-A"])
sh(["git", "commit", "-m",
    "Deploy The Pulse to Cloudflare (Option B: scheduled Python -> Pages)"], check=False)
if sh(["gh", "repo", "view", REPO], check=False).returncode != 0:
    sh(["gh", "repo", "create", REPO, "--public", "--source=.",
        "--remote=origin", "--push"])
    ok(f"created + pushed {REPO}")
else:
    sh(["git", "remote", "add", "origin", f"https://github.com/{REPO}.git"], check=False)
    sh(["git", "push", "-u", "origin", "main", "--force-with-lease"], check=False)
    ok(f"pushed to existing {REPO}")

# ---- 6. secrets + variables --------------------------------------------
step("GitHub: set Actions secrets")
for k, v in secrets.items():
    sh(["gh", "secret", "set", k, "--repo", REPO], inp=v)
    print(f"  ✓ {k}")
step("GitHub: set Actions variables")
for k, v in {"PAGES_PROJECT": PROJECT, "PAGES_URL": PAGES_URL}.items():
    sh(["gh", "variable", "set", k, "--repo", REPO, "--body", v])
    print(f"  ✓ {k} = {v}")

# ---- 7. first run -------------------------------------------------------
step("Kicking off the first dashboard refresh")
sh(["gh", "workflow", "run", "refresh-dashboard.yml", "--repo", REPO], check=False)

print(f"""
==========================================================
 DONE. The Pulse is deploying.
   Live site : {PAGES_URL}
   Actions   : https://github.com/{REPO}/actions
 The first dashboard run is in progress (~2 min). The live
 screen starts on its own 5-min schedule. Give it ~15 min
 to fully populate, then open the site.
==========================================================
""")
