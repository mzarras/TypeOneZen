# Zenbot / TypeOneZen — MacBook Air Deployment Runbook

This is a step-by-step runbook for a Claude Code session running **on the
target MacBook Air** to execute. Each step is imperative and has a
`Verify:` block — run it, confirm the expected result, and only then move
to the next step. If a verify step fails, stop and fix it before
continuing; later steps assume earlier ones succeeded.

Placeholders you'll fill in as you go: `YOUR_MAC_USERNAME`, `+1XXXXXXXXXX`
(the phone number that texts Zenbot), your real Nightscout URL/token, your
Dexcom/COROS credentials, your Anthropic API key. Never commit real values
— `.env` and `~/.openclaw/openclaw.json` both live outside git (the
former is `.gitignore`'d in this repo; the latter lives in `~/.openclaw/`,
not in any repo).

## Known Issues Found While Building This Kit

Read this before starting — it'll save time later. These were confirmed
by directly reading/running the code in this repo (branch
`nightscout-integration`), not assumed:

1. **`python3 db.py` crashes on a truly-fresh machine.** `db.py`'s
   `init_db()` creates an index on the `alert_log` table
   (`idx_alert_log_rule_time`), but `alert_log` itself is only ever
   `CREATE TABLE`'d inside `monitor.py`'s `ensure_tables()` — never inside
   `db.py`. Confirmed by running `db.py` against a fresh `$HOME`: it
   creates `glucose_readings`, `insulin_doses`, `workouts`, `meals`,
   `notes`, then raises `sqlite3.OperationalError: no such table:
   main.alert_log` and exits non-zero — **before** it reaches the
   `ensure_sync_schema()` call, so `sync_state` doesn't get created
   either. Section 4 below documents the two-command workaround
   (`python3 db.py` then `python3 monitor.py --dry-run`, which creates
   `alert_log`, `alert_snoozes`, and `sync_state` via its own
   `ensure_tables()`). This is an existing bug in the repo, not something
   this kit patches — `setup/` only adds new files, per this task's
   constraints.
2. **`parsers/export_correlatewell.py` is deliberately gitignored** — per
   its own docstring, it's personal tooling that connects directly to the
   CorrelateWell production Postgres instance, and per `.gitignore`
   (`parsers/export_correlatewell.py` is explicitly excluded) it will
   **not** be present after `git clone` on the Air. `parsers/
   parse_correlatewell.py` (the importer half) is a normal tracked file
   and will be there. Section 5 Stage 1 covers the manual-transfer step
   this implies.
3. **`TZ_HOME` support is inconsistent across the codebase.** As of this
   branch it's honored by `examples/openclaw-skill/scripts/tz_query.py`,
   `examples/openclaw-skill/scripts/tz_log.py`,
   `parsers/export_correlatewell.py`, and
   `scripts/log_omnipod_screenshot.py` (all read
   `os.environ.get("TZ_HOME", Path.home() / "TypeOneZen")`). It is **not**
   honored by the scripts that actually run on cron — `db.py`, `poller.py`,
   `ns_sync.py`, `monitor.py`, `parsers/parse_glooko.py`,
   `parsers/parse_fit.py`, `parsers/parse_correlatewell.py`,
   `parsers/generate_summary.py`, `parsers/refresh_summary.py`,
   `parsers/fetch_coros.py`, `scripts/daily_summary.py`, and
   `scripts/write_daily_memory.py` all still hardcode
   `Path.home() / "TypeOneZen"` directly (confirmed by grepping each for
   `TZ_HOME`). Practical effect: **the repo must still be cloned to
   literally `~/TypeOneZen`** — setting `TZ_HOME` elsewhere would only
   affect the four scripts above and would desync them from everything
   else reading/writing the real `~/TypeOneZen/data/TypeOneZen.db`.
   `setup/install.sh` warns (but doesn't block) if it detects the repo
   isn't at `~/TypeOneZen`.
4. **Monitor cron cadence is documented inconsistently within this repo
   itself.** `README.md` and `CLAUDE.md` say `monitor.py` runs every 5
   minutes; the OpenClaw skill's `SKILL.md` "Operational Context" section
   says "runs every 15 min via cron." `setup/crontab.txt` uses 5 minutes
   (matching README/CLAUDE.md). Worth reconciling those two docs at some
   point, but out of scope for this kit (`SKILL.md` is an existing file).
5. **OpenClaw's `nativeSkills` config key** (referenced in this repo's own
   `README.md`) could not be found anywhere in the current
   `docs.openclaw.ai` pages fetched for this task. See Section 7 for the
   full research trail — short version: it doesn't seem to be needed
   either way, since skill discovery is fully automatic by file location.

## 0. Prerequisites

### 0.1 macOS + Homebrew

```bash
sw_vers
```
**Verify:** prints a macOS version (any reasonably current one is fine).

Install Homebrew if `brew` isn't already there:
```bash
command -v brew || /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```
**Verify:**
```bash
brew --version
```
Expect a version string, no error.

### 0.2 git

```bash
command -v git || brew install git
```
**Verify:** `git --version` prints a version.

### 0.3 python3 (3.9+)

macOS ships a python3, but prefer Homebrew's so `pip3 install --user`
behaves predictably:
```bash
command -v python3 || brew install python3
```
**Verify:**
```bash
python3 -c 'import sys; print(sys.version_info[:2])'
```
Expect `(3, 9)` or higher.

### 0.4 imsg CLI (iMessage bridge)

```bash
brew install steipete/tap/imsg
```
**Verify:**
```bash
imsg --help
```
Expect usage output, no error. Note the actual path (`which imsg`) — on
Apple Silicon this is normally `/opt/homebrew/bin/imsg`, which is what
`monitor.py`, `scripts/daily_summary.py`, and `setup/openclaw.json.example`
all hardcode. If `which imsg` differs, you'll need to update those
hardcoded paths (existing files) and `setup/openclaw.json.example`'s
`cliPath`.

### 0.5 Sign into iMessage with the dedicated Apple ID

Zenbot needs its own iMessage identity, separate from your personal one,
so alerts and conversations don't get mixed into your main Messages
history:

1. Open **Messages.app**.
2. Messages menu > **Settings** (or **Preferences**) > **iMessage** tab.
3. Sign in with the **dedicated Apple ID** for Zenbot (create one at
   appleid.apple.com first if you haven't).
4. Confirm the phone number/email you'll text Zenbot at is reachable —
   you'll add it to `ALERT_PHONE` in `.env` and `allowFrom` in
   `openclaw.json` later.

**Verify:** Messages.app shows the dedicated Apple ID as signed in
(Settings > iMessage > "You can be reached for messages at...").

### 0.6 Grant Full Disk Access + Automation to your terminal

`imsg` reads the Messages database directly and drives Messages.app via
Automation — both require explicit permission grants (verified against
`docs.openclaw.ai/channels/imessage`).

1. **System Settings > Privacy & Security > Full Disk Access** — click
   `+`, add your terminal app (Terminal.app, iTerm, or whichever app will
   actually run cron/launchd jobs — permissions are per-process, so grant
   it to whatever runs the crontab, not just your interactive shell).
2. **System Settings > Privacy & Security > Automation** — this list
   populates the first time an app *tries* to control Messages, so it may
   not show an entry yet. Trigger it now:
   ```bash
   imsg chats --limit 1
   ```
   This should prompt for Automation permission the first time (a macOS
   dialog: "Terminal wants to control Messages"). Approve it, then check
   it's listed and enabled under **System Settings > Privacy & Security >
   Automation > Terminal > Messages**.

**Verify:**
```bash
imsg chats --limit 1
```
Expect at least one chat listed (or an empty-but-successful result), not
a permissions error. If you get a permissions error, re-check both
Full Disk Access and Automation, then re-run.

### 0.7 Anthropic API key

Get a key from https://console.anthropic.com (API Keys section). This is
for OpenClaw/Zenbot's brain — TypeOneZen's own scripts (`poller.py`,
`ns_sync.py`, `monitor.py`, the parsers) never call the Anthropic API
themselves; they're plain data-layer scripts.

```bash
echo 'export ANTHROPIC_API_KEY="sk-ant-...(your real key)..."' >> ~/.zshrc
source ~/.zshrc
```
**Verify:**
```bash
[ -n "$ANTHROPIC_API_KEY" ] && echo "set (${#ANTHROPIC_API_KEY} chars)"
```
Expect `set (NN chars)`, not empty.

## 1. Clone repos to `~/TypeOneZen`

```bash
cd ~
git clone https://github.com/mzarras/TypeOneZen.git ~/TypeOneZen
cd ~/TypeOneZen
git checkout nightscout-integration
```
**Verify:**
```bash
pwd && git branch --show-current
```
Expect `/Users/YOUR_MAC_USERNAME/TypeOneZen` and `nightscout-integration`.

Clone `nightscout-client` as a sibling checkout (gives you an editable
install for `nscli` + makes `setup/install_skills.sh` able to find the
`skills/nightscout` skill automatically):
```bash
cd ~
git clone https://github.com/mzarras/nightscout-client.git ~/nightscout-client
cd ~/TypeOneZen
python3 -m pip install --user -e ~/nightscout-client
```
If that first `pip install` fails with an externally-managed-environment
error, retry with `--break-system-packages` appended.

**Verify:**
```bash
python3 -c "import nightscout_client; print(nightscout_client.__file__)"
nscli --help
```
Expect a path under `~/nightscout-client/...` and `nscli` usage output.
If `nscli` isn't on your PATH after the editable install, check
`~/Library/Python/3.*/bin` and add it to PATH in `~/.zshrc`.

If you'd rather not maintain a sibling checkout, `pip3 install
git+https://github.com/mzarras/nightscout-client.git` also works (this is
what `setup/install.sh` falls back to in step 2 below) — you just won't
get `nscli` guidance auto-discovered by `setup/install_skills.sh` in
Section 7 unless you point it at the install location with `NSCLIENT_DIR`.

## 2. Run `setup/install.sh`

```bash
cd ~/TypeOneZen
bash setup/install.sh
```

This is idempotent — installs `requirements.txt`, installs
`nightscout-client` from GitHub if the sibling-checkout editable install
in step 1 wasn't done, creates `data/imports/{glooko,fit,correlatewell}`,
`logs/`, `summaries/`, copies `.env.example` → `.env` (only if `.env`
doesn't already exist), checks for `imsg`, checks for `sqlite3`.

**Verify:**
```bash
ls data/imports/ logs/ summaries/ .env
python3 -c "import nightscout_client, dotenv, requests, pydexcom; print('deps OK')"
```
Expect the three `data/imports/` subdirs, `logs/`, `summaries/`, `.env`
all present, and `deps OK` printed with no ImportError.

## 3. Configure `.env`

```bash
cd ~/TypeOneZen
```
Open `.env` (created from `.env.example` in step 2) and set every value:

| Variable | Guidance |
|---|---|
| `DEXCOM_USERNAME` | Your Dexcom Share **follower/share** username (the account with Share enabled on the G7). |
| `DEXCOM_PASSWORD` | That account's password. |
| `DEXCOM_OUTSIDE_US` | `true` only if you use the non-US Dexcom Share server; otherwise `false`. |
| `ALERT_PHONE` | The real phone number (E.164, e.g. `+15551234567`) or iMessage-registered email that receives alerts and talks to Zenbot. Never commit the real value — it's already `.gitignore`'d via `.env`. |
| `COROS_EMAIL` / `COROS_PASSWORD` | COROS Training Hub login, used by `parsers/fetch_coros.py`. |
| `USER_NAME` | First name only — used in `scripts/daily_summary.py`'s greeting text. |
| `NIGHTSCOUT_URL` | Your real Nightscout site URL, e.g. `https://your-nightscout-site.example.com` (used verbatim from `.env.example`'s placeholder format). |
| `NIGHTSCOUT_TOKEN` | **A read-only access token** — create a dedicated subject (e.g. `zenbot-read`) with the `readable` role under Nightscout **Admin Tools > Subjects**. **Never** put your `API_SECRET` here — that grants full read/write access to the site including pump commands. |

**Verify:** confirm no placeholder values remain:
```bash
grep -E "your_|YourFirstName|example\.com|XXXXXXXXXX" .env || echo "no placeholders left"
```
Expect `no placeholders left`. If anything matches, you missed a field.

## 4. Initialize the database

```bash
cd ~/TypeOneZen
python3 db.py
```

**This will print a traceback and exit non-zero** — see "Known Issues" #1
above. Expected output ends with:
```
sqlite3.OperationalError: no such table: main.alert_log
```
That's fine — `glucose_readings`, `insulin_doses`, `workouts`, `meals`,
and `notes` were created before the crash. Immediately run:

```bash
python3 monitor.py --dry-run
```

This creates the remaining tables (`alert_log`, `alert_snoozes`,
`sync_state`) via its own `ensure_tables()`, which runs before any
network/iMessage calls — safe to run even before Nightscout/Dexcom are
fully verified. It'll likely print "no data" style output for the actual
rules; that's expected on an empty DB.

**Verify:**
```bash
sqlite3 data/TypeOneZen.db ".tables"
```
Expect exactly these 8 tables (order may vary):
```
alert_log        insulin_doses     notes
alert_snoozes     meals             sync_state
glucose_readings  workouts
```

## 5. Data backfill (exact order matters for source labeling — importers dedup at minute granularity, so overlap is safe, but run them in this sequence)

### Stage 1 — CorrelateWell historical export (blood sugar + fitness back to fall 2025)

CorrelateWell is a separate app (its own Postgres DB) holding Dexcom +
Strava/HealthKit history back to fall 2025 — TypeOneZen never talks to it
directly at runtime, this is a one-time (or occasionally re-run) backfill.

`parsers/export_correlatewell.py` is **gitignored on purpose** (it
connects straight to CorrelateWell's production Postgres and is treated
as personal tooling, not something to ship in the public repo) — `git
clone` on the Air will **not** include it. `parsers/parse_correlatewell.py`
(the importer) is a normal tracked file and will be there.

**Get the export script onto the Air** — copy it from wherever your
existing checkout has it (e.g. AirDrop, `scp`, or a synced private
location — not iMessage/anything that'd put DB credentials in a chat log):
```bash
# from your current machine, replace the destination with however you're
# reaching the Air (scp shown as an example — AirDrop works too):
scp parsers/export_correlatewell.py YOUR_MAC_USERNAME@<air-hostname-or-ip>:~/TypeOneZen/parsers/export_correlatewell.py
```

**Verify it landed:**
```bash
test -f ~/TypeOneZen/parsers/export_correlatewell.py && echo "present"
```

**Run the export.** It needs network access to CorrelateWell's Postgres —
if the Air can't reach it directly (production DB is likely
VPC/IP-allowlisted), run the export on a machine that *can* reach it
instead and transfer the resulting CSVs (not the DB credentials) to the
Air. Either way, find your real `user_id` first so you don't pull in
demo/seed data:
```bash
# on whichever machine has DB access:
psql "$CW_DATABASE_URL" -c "SELECT id, email FROM users;"   # find your real user_id

python3 -m pip install --user psycopg2-binary   # optional but recommended — see below

export CW_DATABASE_URL="postgres://user:pass@host:5432/health_app_dev"  # or CW_DB_HOST/PORT/NAME/USER/PASSWORD/SSL individually
export CW_USER_ID="<your-real-users.id-uuid>"

python3 parsers/export_correlatewell.py --out data/imports/correlatewell
```
If `psycopg2-binary` isn't installed, the script doesn't fail — it prints
the equivalent `psql \copy` commands to run by hand instead. Either way
you end up with `data/imports/correlatewell/glucose.csv` and
`workouts.csv`; copy those two files to
`~/TypeOneZen/data/imports/correlatewell/` on the Air if you ran the
export elsewhere.

**Import on the Air:**
```bash
cd ~/TypeOneZen
python3 parsers/parse_correlatewell.py --dir data/imports/correlatewell --dry-run   # sanity-check counts first
python3 parsers/parse_correlatewell.py --dir data/imports/correlatewell
```

**Verify:**
```bash
sqlite3 data/TypeOneZen.db "SELECT source, COUNT(*), MIN(timestamp), MAX(timestamp) FROM glucose_readings GROUP BY source;"
sqlite3 data/TypeOneZen.db "SELECT activity_type, COUNT(*), MIN(started_at), MAX(started_at) FROM workouts;"
```
Expect a `correlatewell` row in the glucose query with `MIN(timestamp)`
around fall 2025, and workout rows starting around the same time (source
app — Strava vs HealthKit — is inside each row's `notes` JSON, since
`workouts` has no dedicated source column).

### Stage 2 — Glooko 3-month CSV export

Export a CSV from Glooko (account > export/reports), then:
```bash
mkdir -p ~/TypeOneZen/data/imports/glooko
# copy the exported CSV(s) into ~/TypeOneZen/data/imports/glooko/
cd ~/TypeOneZen
python3 parsers/parse_glooko.py
```

**Verify:**
```bash
sqlite3 data/TypeOneZen.db "SELECT source, COUNT(*), MIN(timestamp), MAX(timestamp) FROM glucose_readings GROUP BY source;"
sqlite3 data/TypeOneZen.db "SELECT CASE WHEN source_id IS NOT NULL THEN 'nightscout' ELSE 'glooko/manual' END AS src, type, COUNT(*), MIN(timestamp), MAX(timestamp) FROM insulin_doses GROUP BY src, type;"
```
Expect a `glooko` row in the first query with a multi-month `MIN`/`MAX`
range, and `glooko/manual` rows (types `bolus`/`correction`/`basal`) in
the second. (`insulin_doses` has no `source` text column — only the
nullable `source_id`, populated by `ns_sync.py`; that's why the second
query derives a pseudo-source from whether it's `NULL`.) Note Glooko's
`carbs_data` files are intentionally skipped by `parse_glooko.py` (daily
summaries only) — meals don't come from Glooko.

### Stage 3 — Nightscout go-live sync

```bash
cd ~/TypeOneZen
python3 ns_sync.py --since 2026-07-08
```

**Verify:**
```bash
sqlite3 data/TypeOneZen.db "SELECT source, COUNT(*), MIN(timestamp), MAX(timestamp) FROM glucose_readings GROUP BY source;"
sqlite3 data/TypeOneZen.db "SELECT CASE WHEN source_id IS NOT NULL THEN 'nightscout' ELSE 'glooko/manual' END AS src, type, COUNT(*), MIN(timestamp), MAX(timestamp) FROM insulin_doses GROUP BY src, type;"
sqlite3 data/TypeOneZen.db "SELECT source, COUNT(*), MIN(timestamp), MAX(timestamp) FROM meals GROUP BY source;"
sqlite3 data/TypeOneZen.db "SELECT value FROM sync_state;"
```
Expect a `nightscout` row in the glucose query starting at/after
`2026-07-08`, `nightscout` insulin rows, a `nightscout` meals row (if any
pump-logged carbs since go-live), and non-empty sync cursors in
`sync_state`.

### Final coverage check

```bash
sqlite3 data/TypeOneZen.db "
SELECT COUNT(DISTINCT date(timestamp)) AS days_with_data,
       MIN(timestamp) AS earliest,
       MAX(timestamp) AS latest,
       COUNT(*) AS total_readings
FROM glucose_readings;
"
```
Expect `earliest` near fall 2025 (once Stage 1 is done) or your Glooko
export's start date (if Stage 1 is still pending), and `latest` within
the last few minutes (proof `ns_sync.py`/`poller.py` are current — run
them again now if `latest` looks stale, since cron isn't installed yet).

Check for large gaps (>4 hours with no readings) that might indicate a
backfill ordering mistake or a real sensor gap:
```bash
sqlite3 data/TypeOneZen.db "
WITH ordered AS (
  SELECT timestamp,
         LAG(timestamp) OVER (ORDER BY timestamp) AS prev_ts
  FROM glucose_readings
)
SELECT prev_ts, timestamp,
       ROUND((julianday(timestamp) - julianday(prev_ts)) * 24, 1) AS gap_hours
FROM ordered
WHERE gap_hours > 4
ORDER BY gap_hours DESC
LIMIT 20;
"
```
A handful of gaps (sensor changes, travel) is normal; a huge number
clustered right at a source boundary usually means a stage was skipped or
run out of order.

## 6. Install cron

```bash
cd ~/TypeOneZen
crontab setup/crontab.txt
```

**Read `setup/crontab.txt`'s header first** if you already have other
cron jobs on this Mac — `crontab <file>` replaces your entire user
crontab, it doesn't merge. The header documents the merge procedure.

**Verify:**
```bash
crontab -l
```
Expect the TypeOneZen jobs (poller, ns_sync, monitor every 5 min;
refresh_summary at 3am; fetch_coros every 6h; daily_summary at 8am/9pm;
write_daily_memory at 10:30pm) plus the `PATH=` line at top.

Wait ~5 minutes, then:
```bash
tail -20 ~/TypeOneZen/logs/cron.log
tail -20 ~/TypeOneZen/logs/monitor.log
```
Expect fresh log lines with no `Error:`/`Traceback` (a `NIGHTSCOUT
unreachable` or similar transient network line is fine to see once; a
repeated crash on every run is not).

Also set up the lid-closed / no-sleep configuration described at the
bottom of `setup/crontab.txt` now, so the box doesn't drop off mid-setup:
```bash
sudo pmset -a disablesleep 1
sudo pmset -a sleep 0 displaysleep 5
pmset -g | grep -E "^ sleep|disablesleep"
```
**Verify:** output includes `sleep      0` and `disablesleep    1`.

## 7. Install OpenClaw + both skills + config

### 7.1 Install OpenClaw

```bash
curl -fsSL https://openclaw.ai/install.sh | bash
```
**Verify:**
```bash
openclaw --version
```
Expect a version string.

### 7.2 Onboard

Two ways to pay for the model — pick one:

**Option A — Claude subscription (Max plan), preferred if available.** As of
mid-2026 Anthropic allows third-party Agent-SDK-based harnesses like OpenClaw
to draw from a Pro/Max subscription's normal usage limits (policy history:
cut off 2026-04-04, reinstated via "Agent SDK credits" 2026-05-14, and the
planned separate-credit split was paused 2026-06-15 — usage draws from the
regular subscription limits again). This shares limits with interactive
Claude Code use on the same account, and the policy has changed twice in
three months — so verify current behavior, and keep Option B configured as
fallback:

```bash
openclaw onboard --install-daemon
openclaw models auth login --provider anthropic --method cli   # subscription OAuth
# (or: openclaw models auth setup-token --provider anthropic)
openclaw gateway restart
```

**Option B — API key (predictable, pay-per-token).** Haiku 4.5 at $1/$5 per
MTok; with the script-routing skill design a typical month should be a few
dollars:

```bash
openclaw onboard --install-daemon --anthropic-api-key "$ANTHROPIC_API_KEY"
```
If `--anthropic-api-key` isn't accepted by your installed version, run
`openclaw onboard --install-daemon` interactively instead and paste the
key when prompted.

**Verify:**
```bash
openclaw gateway status
```
Expect it reporting the gateway running (default port 18789 per
`docs.openclaw.ai/start/getting-started`).

### 7.3 Configure `openclaw.json`

```bash
cp ~/TypeOneZen/setup/openclaw.json.example ~/.openclaw/openclaw.json
```
Edit `~/.openclaw/openclaw.json` and replace:
- `YOUR_MAC_USERNAME` in `channels.imessage.dbPath` with your real macOS
  short username (`whoami`).
- `+1XXXXXXXXXX` in `channels.imessage.allowFrom` with the real number
  from `ALERT_PHONE` in `.env`.

Read the comments in that file for what's VERIFIED against
`docs.openclaw.ai` vs best-effort — this task required not guessing at
config keys, so every key is sourced. Summary of what was and wasn't
confirmed:

**VERIFIED (fetched directly from docs.openclaw.ai, 2026-07-09):**
- Config file location: `~/.openclaw/openclaw.json`
- Model ref format: `"provider/model"`, e.g. `"anthropic/claude-opus-4-8"`
  (source: `docs.openclaw.ai/providers/anthropic`)
- `agents.defaults.workspace`, `agents.defaults.model.primary`
  (source: `docs.openclaw.ai/gateway/configuration`,
  `docs.openclaw.ai/providers/anthropic`)
- `ANTHROPIC_API_KEY` env var, `${VAR_NAME}` substitution in config values
  (source: `docs.openclaw.ai/providers/anthropic`,
  `docs.openclaw.ai/gateway/configuration`)
- `channels.imessage.{enabled,cliPath,dbPath,dmPolicy,allowFrom,
  groupPolicy,groupAllowFrom,includeAttachments,mediaMaxMb}`, `dmPolicy`
  values (`pairing`/`allowlist`/`open`/`disabled`), `imsg` bridge
  requirement, Full Disk Access + Automation permission requirement
  (source: `docs.openclaw.ai/channels/imessage`)
- Skill auto-discovery: `<workspace>/skills/<name>/SKILL.md` picked up
  automatically, no config registration needed, workspace/skills has
  highest precedence among 6 discovery locations
  (source: `docs.openclaw.ai/tools/skills`)
- Install: `curl -fsSL https://openclaw.ai/install.sh | bash`, onboarding
  via `openclaw onboard --install-daemon`, gateway status via
  `openclaw gateway status`, dashboard via `openclaw dashboard`
  (source: `docs.openclaw.ai/start/getting-started`)

**NOT VERIFIED / open questions:**
- The exact model ID `claude-haiku-4-5` doesn't appear in any fetched
  docs example (the Anthropic provider page's examples listed
  `claude-opus-4-8`, `claude-sonnet-5`, `claude-fable-5`,
  `claude-mythos-5`, `claude-opus-4-7`/`4-6`, `claude-sonnet-4-6` — no
  Haiku variant). It's used here per this task's explicit requirement,
  not independently re-confirmed. **Run `openclaw models list --provider
  anthropic` after onboarding and confirm `claude-haiku-4-5` (or the
  closest current Haiku alias) is actually listed before relying on it.**
- `nativeSkills` (referenced in this repo's own `README.md`) — not found
  in any fetched docs.openclaw.ai page. Likely unnecessary given
  auto-discovery is documented as automatic, but flagged since the
  README explicitly calls it out as needed.
- Heartbeat/proactive messaging config — only vague references found
  (`cron`, `hooks`, `commitments.enabled` sections exist per the
  configuration-reference page,  but no concrete schema was shown on the
  pages that loaded). Deliberately omitted from
  `openclaw.json.example` rather than guessed — see that file's trailing
  comment block for the full reasoning. Not needed for this deployment
  anyway, since TypeOneZen's own cron jobs (Section 6) handle proactive
  alerts directly via iMessage.
- Alternate install command seen on the plain GitHub README fetch:
  `npm install -g openclaw@latest`. The curl installer above (from the
  "getting started" docs page) was used as primary since it's from the
  more authoritative onboarding-specific page; either may work.

**Verify:**
```bash
openclaw config validate 2>/dev/null || python3 -c "import json5" 2>/dev/null || echo "spot-check manually: cat ~/.openclaw/openclaw.json"
```
(There's no independently-verified `openclaw config validate` command —
if your installed version has one, use it; otherwise just re-read the
file for typos and confirm the two placeholders above were actually
replaced.)

### 7.4 Install the skills

```bash
cd ~/TypeOneZen
bash setup/install_skills.sh
```
**Verify:**
```bash
ls ~/.openclaw/workspace/skills/
cat ~/.openclaw/workspace/skills/typeonezen/SKILL.md | head -5
```
Expect `typeonezen/` (always) and `nightscout/` (if the sibling checkout
from Section 1 was done — otherwise the script prints exactly what it
searched and how to fix it).

### 7.5 Pair iMessage

Text the dedicated Zenbot Apple ID's number/email from your **own**
phone (the number you put in `allowFrom`), anything, e.g. "hello".

With `dmPolicy: "allowlist"` (as configured above) and your real number
already in `allowFrom`, this should just work — no pairing approval step
needed. If you used `dmPolicy: "pairing"` instead:
```bash
openclaw pairing list imessage
openclaw pairing approve imessage <CODE>
```

**Verify:**
```bash
openclaw channels status --probe
```
Expect the iMessage entry reporting `works` (or equivalent success
status per `docs.openclaw.ai/channels/imessage`).

## 8. End-to-end verification checklist

Run each of these and confirm the expected result before considering
setup done:

```bash
cd ~/TypeOneZen

# 1. nscli reaches your live Nightscout site
nscli status
# Expect: reachable / OK, not a connection error.

# 2. tz_query.py returns current BG + pump context
python3 examples/openclaw-skill/scripts/tz_query.py now
# Expect: JSON with glucose_mg_dl, a recent timestamp, and a non-null
# "nightscout" block (IOB/COB/loop/reservoir) if the loop is live.

# 3. monitor.py dry-run runs clean
python3 monitor.py --dry-run
# Expect: rule output printed, no traceback.

# 4. imsg can send a real test message
imsg send --to "$ALERT_PHONE" --text "Zenbot setup test $(date)"
# Expect: the message actually arrives on your phone within a few seconds.
```

5. **Ask Zenbot over iMessage:** from your phone, text the Zenbot number
   `what's my bg`. Expect a reply within a few seconds citing an actual
   number and freshness (e.g. "118 mg/dL, flat. Last reading 3 minutes
   ago.") — not a generic/refused answer. If it doesn't answer, check:
   `openclaw gateway status`, `openclaw channels status --probe`, and
   `~/.openclaw` logs (exact log path wasn't confirmed in the docs pages
   fetched for this task — check `openclaw --help` or `openclaw logs
   --help` on your installed version if `~/.openclaw/logs/` doesn't
   exist).

## 9. Ongoing ops

**Log locations:**
- `~/TypeOneZen/logs/cron.log` — poller.py, ns_sync.py, refresh_summary.py,
  fetch_coros.py, daily_summary.py, write_daily_memory.py (all cron jobs
  except monitor.py)
- `~/TypeOneZen/logs/monitor.log` — monitor.py's own stdout/stderr
- `~/TypeOneZen/logs/*.log` (via `RotatingFileHandler`, 5MB/3 backups) —
  each script also keeps its own structured log (e.g. `poller.log`,
  `fetch_coros.log`) per `CLAUDE.md`'s documented convention
- `~/.openclaw/workspace/memory/YYYY-MM-DD.md` — nightly Zenbot memory
  files from `scripts/write_daily_memory.py`
- OpenClaw's own gateway/agent logs — location not independently
  confirmed in this task's docs research; check `openclaw --help` /
  `openclaw logs` on the installed version, or `~/.openclaw/` generally

**What runs when** (see `setup/crontab.txt` for exact lines):
- Every 5 min: `poller.py` (Dexcom), `ns_sync.py` (Nightscout),
  `monitor.py` (alerts)
- Every 6 hours: `parsers/fetch_coros.py --days 3`
- Daily 3am: `parsers/refresh_summary.py`
- Daily 8am / 9pm: `scripts/daily_summary.py --period morning|evening`
- Daily 10:30pm: `scripts/write_daily_memory.py`

**Snoozing alerts:**
```bash
cd ~/TypeOneZen
python3 monitor.py --snooze SUSTAINED_HIGH          # snooze one rule, default 120 min
python3 monitor.py --snooze ALL --snooze-duration 480  # snooze everything for 8h
python3 monitor.py --snooze-status                  # see active snoozes
python3 monitor.py --unsnooze                       # clear all snoozes
```

**Updating the skill after repo changes:** any time `examples/
openclaw-skill/` changes in this repo (or `nightscout-client`'s
`skills/nightscout` changes), re-run:
```bash
cd ~/TypeOneZen
git pull
bash setup/install_skills.sh
```
It's idempotent (`rsync -a --delete`) — safe to run any time, always
leaves the workspace copy exactly matching the source. No OpenClaw
restart should be required per the auto-discovery/file-watch behavior
described at `docs.openclaw.ai/tools/skills` (`skills.load.watch`), but
if changes don't seem to take effect, restart the gateway:
```bash
openclaw gateway restart 2>/dev/null || (openclaw gateway stop && openclaw gateway status)
```
(exact restart subcommand not independently confirmed — check `openclaw
gateway --help` on your installed version).

**Updating TypeOneZen itself:**
```bash
cd ~/TypeOneZen
git pull
bash setup/install.sh   # re-run — idempotent, picks up new deps/dirs
```
