# AGENTS.md

Guidance for AI agents (and humans) working on this repository.
Keep this file short; details live in `DESIGN.md` and `.agents/skills/`.

## What this is

A personal stock-portfolio web app: Python + FastAPI + Jinja2
(server-rendered), PostgreSQL, two containers via docker compose
(`web` + `db`). The app code is a single module, `web/app/main.py`.
Design decisions and formulas are documented in `DESIGN.md` — read it
before changing calculations.

## Hard rules (public repository)

- This repo is **public on purpose**. Never commit secrets, real
  holdings/trade data, or environment specifics (addresses, hostnames,
  network layout). `.env`, seed data and data-conversion scripts are
  gitignored — keep it that way. CI runs gitleaks on every push.
- `DESIGN.md` is the public-safe design doc: concepts and formulas
  only, no real amounts or position names. Preserve that when editing.

## Domain invariants

- **One source of truth per value.** The dividend forecast shown is
  always the auto-fetched company forecast; it is never edited by hand
  and has no manual override column. Manual fields exist only for what
  cannot be fetched (shareholder-benefit estimates, judgment memos).
  Before adding a second (manual) path for a value, decide which one
  is authoritative — normally the fetched one.
- Prices and dividends are fetched only by the nightly batch
  (01:00 JST) from Yahoo! Finance Japan quote pages. Do not add other
  sources or trigger fetches casually; the fetch is deliberately slow
  (25–45 s per code, randomized) because the source rate-limits.
- Trade status vocabulary is exactly 注文中 / 一部約定 / 約定済.
  A sale's status and profit may be auto-derived from executed shares;
  explicit user input always wins over the derivation.
- The monthly after-tax income target is derived
  (annual dividend target × (1 − tax rate) / 12, rounded half up),
  never stored from direct input.
- Tables may overflow horizontally. Do not wrap or compress cells to
  force a fit; one line per row and readability win.

## Working agreements

- **Tests:** `tests/` (pytest). Run them before pushing changes to
  `web/app` — see `.agents/skills/run-tests/SKILL.md`. Coverage is
  branch coverage (C1, `.coveragerc`); CI fails below 95%.
- **CI constraints:** this repo allows local actions only and
  requires SHA pinning, so workflows must not use external `uses:`
  actions. Checkout in workflows is plain git with `GITHUB_TOKEN`.
- **Lint:** ruff scope is pinned in `ruff.toml` (syntax + pyflakes
  level). Do not widen it or "fix" intentional style (e.g. broad
  `except` around per-code fetches is deliberate: one bad page must
  not fail the whole batch).
- **Dependencies:** versions are pinned as `==x.y.*` in
  `web/requirements.txt`. Dependabot updates are merged one at a time
  with deploy verification — see
  `.agents/skills/dependabot-updates/SKILL.md`. PostgreSQL major
  upgrades need a dump/restore migration, not just an image bump.
- **Commits:** explain *why* in the body when the change is not
  obvious from the diff (a past fix without its reason will be
  re-broken). Squash-merge pull requests.
