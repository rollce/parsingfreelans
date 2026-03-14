# Freelans Bot

![Python](https://img.shields.io/badge/Python-3.12-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-async-green)
![Telegram](https://img.shields.io/badge/Telegram-inline--bot-2AABEE)
![Status](https://img.shields.io/badge/Status-RU--first%20PoC-orange)

> **RU-first open-source PoC** for end-to-end freelance flow automation:
> marketplace parsing, lead scoring, LLM proposal generation, and Telegram control.

## What This Project Does

**Freelans Bot** automates the following pipeline:

1. Collects new projects from freelance marketplaces.
2. Filters and scores leads for your target stack.
3. Generates proposal drafts via LLM (*semi-auto from Telegram buttons*).
4. Delivers leads and statuses to Telegram.
5. Runs delivery/infrastructure diagnostics through runtime menus and scripts.

## Supported Platforms

- `flru`
- `freelance_ru`
- `kwork`
- `workzilla`
- `youdo`
- `yandex_uslugi`
- `freelancejob`

Notes:

- `yandex_uslugi` uses `feed_urls` (multiple search streams).
- `youdo` uses an anti-bot profile (jitter/fingerprint/proxy hooks).

## Architecture

- `src/freelans_bot/adapters/` - platform adapters and Playwright logic.
- `src/freelans_bot/core/orchestrator.py` - pipeline: `collect -> score -> draft -> apply -> notify`.
- `src/freelans_bot/services/` - scoring, proposal generation, selector auditing.
- `src/freelans_bot/integrations/telegram.py` - Telegram notifier.
- `src/freelans_bot/storage/db.py` - SQLite data layer (leads, events, metrics).
- `src/freelans_bot/app.py` - FastAPI app + background worker.

## Quick Start

```bash
cp .env.example .env
# Fill TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, OPENROUTER_API_KEY

python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/playwright install chromium
```

### Save Platform Sessions

```bash
.venv/bin/python scripts/save_session.py --list
.venv/bin/python scripts/save_session.py --platform flru
.venv/bin/python scripts/save_session.py --platform freelance_ru
.venv/bin/python scripts/save_session.py --platform kwork
.venv/bin/python scripts/save_session.py --platform workzilla
.venv/bin/python scripts/save_session.py --platform youdo
.venv/bin/python scripts/save_session.py --platform yandex_uslugi
.venv/bin/python scripts/save_session.py --platform freelancejob
```

Custom login URL (if needed):

```bash
.venv/bin/python scripts/save_session.py --platform youdo --login-url "https://youdo.com/<login-url>"
```

### Run Locally

```bash
.venv/bin/python -m uvicorn freelans_bot.app:app --host 0.0.0.0 --port 8000
```

API checks:

- `GET /health`
- `GET /stats`
- `GET /events`
- `GET /leads`

## Telegram Control

No command-driven UX is required. The bot uses **inline buttons** and edits a single message thread.

Main sections:

- **Status**
- **Leads**
- **Flow**
- **Logs**
- **Accounts**
- **Profile**
- **Settings**
- **Reports**

Runtime tuning is supported without redeploy: filters, intervals, alerts, backup/cleanup,
delivery-health, heartbeat, pending reanimate, failover, and related limits.

## Performance Defaults

Memory optimization is enabled by default:

- shared Playwright browser per platform adapter (contexts are recycled),
- resource blocking for heavy types (`image,media,font`),
- smaller default viewport (`1280x720`) to reduce renderer memory.

Main tuning flags:

- `PLAYWRIGHT_BLOCK_RESOURCES=true`
- `PLAYWRIGHT_BLOCK_RESOURCE_TYPES=image,media,font`
- `PLAYWRIGHT_REUSE_BROWSER=false` (lower idle RAM/cost)
- `PLAYWRIGHT_BROWSER_RECYCLE_CONTEXTS=24`
- `PLAYWRIGHT_BROWSER_MAX_AGE_MINUTES=30`
- `PLAYWRIGHT_LAUNCH_ARGS=...`
- `EVENTS_MAX_ROWS=5000` (caps event-log table growth)

## Quality & Diagnostics Scripts

### Base Pre-deploy Pack

```bash
.venv/bin/python scripts/check_max_lines.py --max-lines 1000 --include-untracked
.venv/bin/python scripts/check_selectors_registry.py --min-selectors 6
.venv/bin/python scripts/check_secrets_hygiene.py --include-untracked --max-issues 20
```

### Secrets Check (No Plain Secret Output)

```bash
# machine output
.venv/bin/python scripts/check_secrets_hygiene.py --include-untracked --json --json-pretty

# save JSON artifact
.venv/bin/python scripts/check_secrets_hygiene.py --include-untracked --json --json-out tmp/secrets-check.json

# exclude service folders
.venv/bin/python scripts/check_secrets_hygiene.py --include-untracked --exclude-path tmp/,backups/
```

The script masks sensitive matches in logs (`1234...2345`, `sk-o...6789`) to prevent CI log leaks.

### Delivery Diagnostics

```bash
# full report to Telegram
.venv/bin/python scripts/send_delivery_diagnostics_report.py --hours 24

# state JSON
.venv/bin/python scripts/send_delivery_diagnostics_report.py --hours 24 --only-alert-state --state-json --state-out tmp/delivery-state.json

# only on state change
.venv/bin/python scripts/send_delivery_diagnostics_report.py --hours 24 --dry-run --only-on-state-change --state-in tmp/delivery-state.json --stdout-summary --quiet
```

### State Contract & Diff

```bash
# validate state contract/types
.venv/bin/python scripts/check_delivery_state_contract.py --state tmp/delivery-state.json --strict-types --json --json-pretty

# diff two snapshots
.venv/bin/python scripts/diff_delivery_state.py --prev tmp/state-prev.json --curr tmp/state-curr.json --ignore-generated-at --exit-on-change

# diff with filters + artifacts
.venv/bin/python scripts/diff_delivery_state.py \
  --prev tmp/state-prev.json \
  --curr tmp/state-curr.json \
  --field alert_state,alert_components \
  --required-field alert_state,alert_components \
  --json --json-pretty --json-out tmp/state-diff.json \
  --text-out tmp/state-diff.txt
```

## Railway Deployment

`Dockerfile` already contains pre-check gates. Deployment fails if:

- a file exceeds `1000` lines,
- selector registry is invalid,
- a secret pattern is found in tracked/untracked workspace files.

Minimum web service command:

```bash
python -m uvicorn freelans_bot.app:app --host 0.0.0.0 --port ${PORT:-8000}
```

Cron examples:

```bash
python scripts/send_selectors_report.py --min-selectors 6
python scripts/send_weekly_digest.py --days 7
python scripts/send_weekly_super_report.py --days 7
python scripts/send_stale_pending_report.py --days 45 --threshold 40
python scripts/send_delivery_health_report.py --hours 24
python scripts/send_delivery_mini_report.py --hours 24
python scripts/send_delivery_diagnostics_report.py --hours 24 --only-on-alert --stdout-summary --quiet
```

## Open-source Status

This repository is maintained as an **open-source PoC**.

- Changelog roadmap: `TODO.md`
- Secret rotation notes: `docs/SECRETS_ROTATION.md`
- GitHub profile snippet: `docs/GITHUB_PROFILE_README_SNIPPET.md`

## GitHub Profile Snippet

Use this file for your profile repository README:

- [docs/GITHUB_PROFILE_README_SNIPPET.md](docs/GITHUB_PROFILE_README_SNIPPET.md)

## Security Checklist (Before Push)

```bash
.venv/bin/python scripts/check_secrets_hygiene.py --include-untracked --max-issues 20
git grep -nE "([0-9]{8,12}:[A-Za-z0-9_-]{30,}|sk-or-v1-[A-Za-z0-9]{20,})" || true
```

> *Never commit real API tokens into `.env.example`, README files, scripts, or CI configs.*

## Contribution

PR rules:

- Keep one logical change-set per PR.
- Run checks from *Quality & Diagnostics Scripts* before opening a PR.
- Do not introduce files larger than `1000` lines.
- Document every new runtime flag in README.

## License

This project is licensed under the **MIT License**.
See [LICENSE](LICENSE).
