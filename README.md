# gsc-mcp

A Google Search Console MCP server for AI agents: index-status checks,
unindexed-URL discovery, Request-Indexing submission, sitemap submission,
performance analytics, and indexation audits.

**Status: in development.** This repository currently contains the engine
foundation only — no MCP tools yet. See the plan sequence in the design spec.

## Requirements

- Python 3.11 or newer
- A Google account with Search Console properties

## Development

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"
.venv/Scripts/python -m pytest -v
```

## Layout

| Module | Responsibility |
|---|---|
| `gsc_core/paths.py` | Where files live, per platform |
| `gsc_core/runlog.py` | Logging to stderr and file — never stdout |
| `gsc_core/store.py` | SQLite: sites, urls, submissions, jobs, quota slots |
| `gsc_core/quota.py` | Per-property rolling slot accounting |
| `gsc_core/gauth.py` | OAuth with PKCE, token storage and refresh |
| `gsc_core/config.py` | User-tunable settings |

## Quota model

Both Google limits that matter are **per property**, not per account:

| Limit | Value | Mechanic |
|---|---|---|
| Request Indexing | ~11 slots per property | Rolling — each slot frees 24h + 1min after its own use |
| URL Inspection | 2000/day per property | Daily |
| URL Inspection | 600/min per property | Rate |

Properties are independent, so a user with eight properties has eight
independent budgets.

## Licence

MIT
