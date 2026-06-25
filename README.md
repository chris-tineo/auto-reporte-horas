# auto-reporte-horas

Automates filling timesheets across several jobs/systems (Python + Playwright),
plus the end-to-end monthly process for the **Syneos work group** (submit,
manager notification, invoice, email, timecard export, SharePoint upload).

Each company is a YAML in `companies/`; `bot.py` is generic and driven by those
configs. Companies that belong to the same engagement are grouped with a `job:`
field so their hours stay identical.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
copy .env.example .env      # only needed for password-auth companies
```

Python 3.11+. No DB; state lives in `companies/*.yaml`, `week_note.txt`, and the
saved browser sessions under `auth/`.

## Core concepts

- **Company** — one `companies/<name>.yaml`. `active: true/false`, `auth`, `flow`,
  `url`, `login_marker`, and flow-specific selectors/values.
- **Auth**
  - `password` — unattended login from `.env` (`<NAME>_USER` / `<NAME>_PASS`).
    Supports multi-step logins via a `login.steps` list (e.g. BigTime: email →
    Next → user/pass → Login).
  - `session` — for SSO/MFA. Log in by hand once (`--login`), the session is
    saved to `auth/<name>_state.json` and reused until it expires. Expiry is
    detected via `login_marker`.
- **Flows** (`flow:` in the YAML)
  - `simple` — one project dropdown + hours field(s) + save.
  - `modal_entries` — open a modal per day, fill fields, add-to-list, save
    (ASP.NET Zero, e.g. Bertoni).
  - `weekly_grid` — fill per-day hour cells in a pre-existing weekly row, save;
    optional `--submit` (BigTime, e.g. KPI Partners).
  - `oracle_api` — Oracle Fusion Redwood's grid can't be driven reliably, so we
    replay the UI's own backend call: capture a session bearer token + the
    current timecard, then POST entries (`TIME_SAVE`) or submit (`TIME_SUBMIT`).
- **Jobs** — companies sharing a `job:` value are one engagement. `--job <name>`
  runs them all, and their worked days are defined **once** in `week_note.txt`
  under `[job:<name>]`, so they can't drift out of sync.
- **week_note.txt** (gitignored) — per-week worked days + observation:
  ```
  [job:syneos]
  week 2026-W23 | worked: Mon-Fri | obs: <English task summary>
  ```
  `obs` is always in English and is only used by Bertoni; KPI/Syneos ignore it.

## Companies configured

| Company | System | Flow | Auth | Notes |
|---|---|---|---|---|
| `bertoni` | ASP.NET Zero "Time Management" | `modal_entries` | session (O365 SSO) | 8h/day, project KPI Partners Inc, weekly `obs` |
| `kpi_partners` | BigTime | `weekly_grid` | password (2-step) | row Syneos Health SOW, 8h/day; `--submit` only on weeks ending Friday |
| `syneos` | Oracle Fusion Cloud (Redwood) | `oracle_api` | session (Azure AD SSO+MFA) | biweekly timecard, 8h/day, submit via API |

These three are one job (`job: syneos`): Syneos is the end client, KPI Partners
contracted the staffing, Bertoni pays. Their hours must match at month end.

## Weekly: fill hours

1. Generate `week_note.txt` (dictated each Friday; `obs` translated to English).
2. Run the fill:
   ```powershell
   python bot.py --job syneos                 # all 3 companies, current ISO week
   python bot.py --company bertoni --month 2026-06   # one company, a whole month
   python bot.py --company syneos --from 2026-06-22 --to 2026-06-30
   python bot.py --company kpi_partners --week 2026-W27 --dry-run
   ```
   Flags: `--week YYYY-Www`, `--month YYYY-MM`, `--from/--to YYYY-MM-DD`,
   `--dry-run`, `--submit` (weekly_grid / oracle_api), `--login`.

**Always validate a new company with `--dry-run` first.**

## Monthly: the Syneos work group (`monthly.py`)

One command runs the day-24 and day-25 process. Config in `monthly_config.yaml`.

```powershell
# Day 24 — submit Syneos, Teams to manager, invoice, monthly email
python monthly.py --month 2026-06 --day 24 --desc "UDP ... (month summary)"
python monthly.py --month 2026-06 --day 24 --desc "..." --send

# Day 25 — export Syneos timecard PDF, upload to SharePoint
python monthly.py --month 2026-06 --day 25
python monthly.py --month 2026-06 --day 25 --send
```

**Safe by default**: without `--send` the email is left as a *draft* and
Teams/submit/upload run dry. `--desc` (the month's English work summary) is
provided per run. Invoice number comes from `boleta/config.yaml` (`next_invoice`)
or `--invoice`. Hours = worked weekdays for the month × rate (`$30/h`).

Pieces it orchestrates: `bot.py` (Syneos submit), `boleta.py` (invoice PDF),
inline KPI grid screenshots + a generated Syneos approval table embedded in the
email, the boleta attachment, the Oracle Print→PDF export, and the SharePoint
upload (with a folder safeguard).

## Sessions & credentials

- Sessions: `auth/*_state.json` — `bertoni` (also used for Outlook),
  `kpi_partners`, `syneos`, `teams`, `kpi_sharepoint`. Created via `--login`
  (bot) or the one-off login helpers. All gitignored.
- `.env`: only `KPI_PARTNERS_USER/PASS` (BigTime, no MFA). Gitignored.
- When a session expires, re-run the relevant `--login`.

## Safeguards & compliance

- Real writes/sends are gated: `--send`, recipient/folder checks, single-period
  submit guards. Email defaults to a draft; uploads verify the target folder.
- Logging hours automatically (especially future or unworked days) can conflict
  with each company's internal policy. This is the user's call, not the tool's.

## Repo layout

```
bot.py                # generic hour-filling CLI (flows, jobs, week_note)
monthly.py            # monthly orchestrator for the Syneos group
boleta.py             # invoice (boleta) PDF generator
monthly_config.yaml   # orchestrator config (recipients, sessions, URLs)
companies/*.yaml      # one per company
boleta/               # invoice template + config (next_invoice)
auth/                 # saved sessions            (gitignored)
logs/                 # per-company logs          (gitignored)
week_note.txt         # weekly worked days + obs  (gitignored)
img/ export/          # generated email images / timecard exports (gitignored)
```

## Pending / to validate next month

- Submit **and** export of **both** biweekly periods on the 24th/25th (the
  1–15 period needs period navigation; only the loaded period is handled today).
- Real Teams message to the Syneos manager (the send path is built but only
  self-tested).
- Scheduling: semi-manual by nature (monthly `--desc` + MFA session refresh).
