# Access Log Analyzer

A CLI tool for Jira and Confluence Data Center administrators to analyze API usage patterns and predict rate limiting issues before migrating to Atlassian Cloud.

## What It Does

When migrating from Jira/Confluence Data Center to Atlassian Cloud, your instance will be subject to strict API rate limits. This tool reads your existing DC access logs and calculates:

1. **Points-based hourly quota** — Would your current API usage exceed Cloud's hourly point limits?
2. **Burst rate limits** — Are any integrations sending too many requests per second?
3. **Per-issue write limits** — Are any automations updating the same issue too frequently?

## Atlassian Cloud Rate Limits

Based on [Atlassian's rate limiting documentation](https://developer.atlassian.com/cloud/jira/platform/rate-limiting/) (points-based quotas enforced from **March 2, 2026**). All quotas are measured in **points per hour** and reset at the top of each UTC hour. Your hourly quota depends on your rate limit **tier** and, for Tier 2, your customer edition and user count.

**Tier 1 — Global Pool (default for most apps):** a single shared quota of **65,000 points/hour** across all tenants.

**Tier 2 — Per-Tenant Pool (apps with sustained high usage, after Atlassian review):** a separate quota per tenant, based on edition and licensed user count, capped at **500,000 points/hour**.

| Tier / Edition | Points/Hour |
|------|------------|
| Tier 1 — Global Pool (default) | 65,000 (shared across all tenants) |
| Tier 2 — Free | 65,000 |
| Tier 2 — Standard | 100,000 + (10 × users) |
| Tier 2 — Premium | 130,000 + (20 × users) |
| Tier 2 — Enterprise | 150,000 + (30 × users) |

> Tier 2 per-tenant quotas are capped at 500,000 points/hour for Standard, Premium, and Enterprise editions.
>
> **Points cost model:** every request costs **1 base point**. Read (`GET`/query) requests add points per object returned — **1 point** per core object (issues, projects, dashboards, attachments) and **2 points** per identity/access object (users, groups, roles, permissions). Write requests (`POST`/`PUT`/`PATCH`/`DELETE`/mutation) are charged the base cost only (1 point, no per-object cost).

## Installation

> **Note:** Requires Python 3.11. This project is currently constrained to `<3.14` because `pymupdf==1.23.8` did not build cleanly under Python 3.14 in this environment.

```bash
# Install uv, Python 3.11, tkinter, and poppler if needed
brew install uv python@3.11 python-tk@3.11 poppler

cd access-log-analyzer
uv python install 3.11
uv sync --python 3.11 --group dev
```

This creates a local `.venv`, installs runtime dependencies from `pyproject.toml`, and includes the development tools.

## Development Commands

```bash
# Lint
uv run ruff check .

# Type check
uv run ty check .

# Refresh lockfile after dependency changes
uv lock --python 3.11
```

## Usage

### GUI App (Recommended)
```bash
uv run access-log-analyzer-gui
```

### CLI

```bash
# Analyze a Jira access log against Standard plan limits
uv run access-log-analyzer --log /path/to/jira/access.log --product jira

# Analyze against Premium plan limits
uv run access-log-analyzer --log /path/to/jira/access.log --product jira --plan premium

# Analyze against Enterprise max limits (large orgs with 500k points/hour)
uv run access-log-analyzer --log /path/to/jira/access.log --product jira --plan enterprise-max

# Analyze a Confluence access log
uv run access-log-analyzer --log /path/to/confluence/access.log --product confluence

# Try with the included sample log
uv run access-log-analyzer --log sample_logs/jira-access.log --product jira
```

If you prefer, these equivalent direct commands also work:

```bash
uv run python app.py
uv run python main.py --log sample_logs/jira-access.log --product jira
```

## Dependency Files

- `pyproject.toml` is the source of truth for project metadata, runtime dependencies, dev dependencies, and tool config.
- `uv.lock` locks exact dependency versions for reproducible installs.
- `.python-version` pins the repository to Python 3.11 for uv.
- `requirements.txt` is kept for compatibility, but uv is now the default workflow.

## Where to Find Your DC Access Logs

**Jira DC:**
```
<jira-home>/log/access.log
# or
/var/atlassian/application-data/jira/log/access.log
```

**Confluence DC:**
```
<confluence-home>/logs/access.log
# or
/var/atlassian/application-data/confluence/logs/access.log
```

## Output

The tool generates a report with 5 sections:
1. **Summary** — Overall stats
2. **Hourly Quota Analysis** — Points used per hour vs Cloud limits
3. **Burst Rate Analysis** — Requests/second per endpoint
4. **Per-Issue Write Analysis** — Write frequency per issue
5. **Recommendations** — Actionable steps to address any issues

## Risk Levels

| Icon | Meaning |
|------|---------|
| 🟢 OK | Within safe limits |
| 🟡 WARNING | >75% of limit, monitor closely |
| 🔴 BREACH | Would exceed Cloud rate limits |
