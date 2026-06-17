# ClickUp Engineering Analytics

[![CI](https://github.com/zihar/engineering-productivity/actions/workflows/ci.yml/badge.svg)](https://github.com/zihar/engineering-productivity/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

A Python tool for analyzing engineer productivity from [ClickUp](https://clickup.com) data
via the REST API. It pulls tasks per engineer (across spaces), computes metrics, and then
generates a **Markdown report** ready to share with management.

## Metrics computed

| Metric | Description |
|---|---|
| **Throughput** | Number of tasks completed per engineer, broken down per week (ISO week). |
| **Lead time** | Time from task created → completed (days). Median & average. |
| **Cycle time** | Time a task spends in active statuses (e.g. In Progress, Review). Requires `--deep`. |
| **Time tracked** | Actual time-tracking hours per engineer vs estimate, plus estimation accuracy. |
| **Status flow / bottleneck** | Median/p90 time a task lingers in each status (terminal statuses excluded). Requires `--deep`. |
| **Commit activity (GitLab)** | Commits, active days, +/- lines, & repos per engineer. Source: GitLab API directly (live). Optional. |
| **Task vs commit matrix** | A 2×2 quadrant (ClickUp throughput × commit active days) to spot patterns of task discipline vs code output. |
| **Utilization (underutilized)** | A 0–100 score relative to the team from 4 signals (WIP, commit active days, throughput, story points) to flag engineers with idle capacity. Optional. |

## Setup

```bash
cd engineering-productivity
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp config.example.yaml config.yaml   # then fill in the list of engineers
export CLICKUP_TOKEN="pk_xxxxxxxx"    # token from ClickUp: Settings -> Apps
```

> The token should be provided via the `CLICKUP_TOKEN` environment variable, not written in
> `config.yaml` (that file is already in `.gitignore`).

## Configuration

`config.yaml` just needs a list of engineers (using email or numeric id):

```yaml
team_id: ""               # optional; leave empty to use the first workspace
engineers:
  - name: "Budi"
    email: "budi@example.com"
    chapter: "Backend Golang"   # optional: discipline/chapter for filtering
  - name: "Sari"
    id: 12345678
    chapter: "Frontend Dev"
```

Set `chapter` per engineer (e.g. `Backend Golang`, `Backend .Net`, `Frontend Dev`, `Mobile Dev`) to
filter by chapter in the dashboard (sidebar **Chapter**) or CLI (`--chapter "Backend Golang"`, repeatable).
When filtered to a chapter, the utilization score is computed **relative to that chapter** (fair like-for-like).

### (Optional) GitLab commit activity

Commits are always pulled from the **GitLab API** (live) when GitLab is configured; without that, commit metrics are skipped.

**GitLab API directly (live)** — always up to date, plus real +/- lines.
Generate a token at `https://git.bluebird.id/-/user_settings/personal_access_tokens`
(scope `read_api`), then `export GITLAB_TOKEN=glpat-...`:

```yaml
gitlab:
  url: "https://git.bluebird.id"
  projects: [692, "da/driverapp-gateway"]   # optional seed; id or path
  aliases: {"orang@gmail.com": "orang@bluebirdgroup.com"}  # personal commit email
```

By default the tool **auto-discovers** each engineer's repos (via GitLab push events),
merged with the `projects` seed — so repos that aren't registered (e.g. `argocd/*`)
get picked up too. Disable with `--no-discover` if you want to use only `projects`.

The `--exclude-noise` flag recomputes +/- lines **excluding noise files** (vendor, lockfiles,
generated, etc — see `DEFAULT_NOISE_PATTERNS`; add more via `gitlab.noise_patterns`).
This fetches the diff of each commit (1 call/commit), making it **slower**, so it's optional.

The tool adds a **Commit Activity** section + **Task vs Commit Matrix**, joined via ClickUp id.

View the id/email of workspace members:

```bash
python -m engineering_productivity --list-members
python -m engineering_productivity --list-teams
```

## Usage

```bash
# last 30 days, concise report (fast)
python -m engineering_productivity --days 30 -o reports/bulan-ini.md

# specific range + deep analysis (cycle time & bottleneck)
python -m engineering_productivity --since 2026-05-01 --until 2026-05-31 --deep -o reports/mei.md
```

| Flag | Default | Function |
|---|---|---|
| `--config` | `config.yaml` | Configuration path |
| `--since` / `--until` | — / today | Date range `YYYY-MM-DD` |
| `--days` | `30` | Lookback when `--since` is empty |
| `--deep` | off | Fetch `time_in_status` per task → cycle time & bottleneck |
| `--last-done` | off | Add a *Last completed* column (when each engineer last closed a task, across the period) |
| `--utilization` | off | *Underutilized Engineers* section (team-relative score; pulls WIP & story points) |
| `-o`, `--output` | `reports/report.md` | Output file |

> **Utilization:** score 0–100 = average percentile across signals (lower means more underutilized).
> Story points are read from ClickUp's native `points` field (sprint points); signals with no data
> are skipped automatically. This is a prompt for a capacity conversation, **not** a performance ranking.

## Interactive dashboard

In addition to the Markdown report, there's a Streamlit dashboard for exploration:

```bash
pip install -r requirements.txt
export CLICKUP_TOKEN=pk_...        # and GITLAB_TOKEN=glpat-... if using the GitLab source
streamlit run dashboard.py
```

Features: filter by chapter & period & engineer, toggle `deep`/stale-task filter/noise filter,
concise KPIs, throughput & active-days charts, an interactive **Task vs Commit matrix** (Plotly),
bottleneck table, and a button to download the Markdown report. The **Refresh data** button clears the cache.
The dashboard uses the same pipeline as the CLI (`engineering_productivity.pipeline.gather_report`).

> Run only on localhost — it contains employee productivity data. `deep` & the noise filter
> make each interaction slower (both default to OFF; results are cached).

## Postgres cache (optional, for faster loads)

Without a cache, every run/load re-pulls everything from ClickUp & GitLab — most expensive for **task** queries
(filter by Developer custom field ~10s/page, open-ended query ~333s), `--deep` mode (1 call/task), and GitLab
commits (hundreds of calls). Enable the Postgres cache so data is stored and reused; each load only pulls the **delta**:

- **ClickUp tasks** (done, open, last-done): synced incrementally via `date_updated`. New engineers are
  backfilled once from `task_backfill_since` (default `2026-05-01`); subsequent loads only fetch changed tasks → fast.
  Attribution still via the Developer column.
- **time_in_status** of *done* tasks (immutable) & **commits** per sha.

```bash
createdb engineering_productivity          # a separate database in your Postgres
export EP_STORE_DSN=postgres://localhost:5432/engineering_productivity
```

Or set `store.dsn` in `config.yaml` (and optionally `task_backfill_since`). Enabled automatically when a DSN exists;
without a DSN = live mode (old behavior, fallback path). A second run/dashboard for the same parameters becomes much
faster (tasks & deep from cache, only new commits pulled). Note: `--exclude-noise` is not yet cached (stays live).

## Accuracy notes

- **Attribution via the Developer column:** "who worked on the task" is taken from the custom field
  **Developer** (users type), not from `assignees`. Tasks with an empty Developer are skipped.
  The field is resolved automatically by name (`developer_field_name`, default `Developer`); it can be
  overridden with `developer_field_id`.
- **Shared credit:** a task with multiple Developers is counted for each engineer in that column.
- **Time tracked** is taken from the *time entries* endpoint and credited to the **Developer** on
  the task of that time entry (not the person who logged it), not from the task's `time_spent` field. Note:
  only time entries on tasks that were fetched are counted.
- `--deep` mode makes 1 API call per task → slower & more quota-hungry
  (ClickUp rate limit ~100 req/min, already handled automatically with retry).
- These metrics are a discussion aid, **not** an absolute performance assessment. High throughput
  doesn't necessarily mean productive; always read it alongside context (task complexity, etc).

## Structure

```
engineering_productivity/
  config.py     # load & validate config.yaml (+ token from env)
  client.py     # ClickUp REST API v2 client (pagination + rate-limit retry)
  gitlab.py     # live commit source from the GitLab API (+ auto-discover, noise filter)
  metrics.py    # compute throughput, lead/cycle time, time tracked, status flow
  pipeline.py   # reusable orchestration (used by CLI & dashboard)
  report.py     # render Markdown
  __main__.py   # CLI
dashboard.py    # Streamlit dashboard (streamlit run dashboard.py)
```

## License

[MIT](LICENSE) © Zihar Mehta
