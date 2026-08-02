# Google Search Console MCP Server

**An MCP server that gives Claude and other AI agents real control over Google Search Console** — check whether a URL is indexed, find the pages Google is ignoring, request indexing, submit sitemaps, and pull search analytics, all from a conversation.

[![CI](https://github.com/Mrshahidali420/google-search-console-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/Mrshahidali420/google-search-console-mcp/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![License: FSL-1.1-ALv2](https://img.shields.io/badge/license-FSL--1.1--ALv2-green)](LICENSE)
[![Status: pre-alpha](https://img.shields.io/badge/status-pre--alpha-orange)](#project-status)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen)](CONTRIBUTING.md)

> Built for SEO practitioners who are tired of clicking "Request Indexing" 11 times a day, and for the AI agents that can do it for them.

---

## Project status

**Pre-alpha. The engine is built; the MCP tools are not.**

You cannot connect this to Claude yet. What exists today is the foundation everything else sits on — storage, quota accounting, OAuth, config — covered by 152 passing tests. Tools land in the next milestone.

| Milestone | Scope | State |
|---|---|---|
| 1. Foundation | Paths, logging, SQLite store, quota engine, OAuth + PKCE, config | **Done** |
| 2. MCP surface | The tools below, exposed over MCP | Next |
| 3. Submission | Browser-driven Request Indexing, browser/profile auto-detection | Planned |
| 4. Reporting | Indexation audits, discovery loops, bulk runs | Planned |

Watch or star the repo if you want to know when milestone 2 ships.

## Planned tools

| Tool | What it does |
|---|---|
| `gsc_list_sites` | List every Search Console property the account can reach |
| `gsc_check_status` | Index status for a URL via the URL Inspection API |
| `gsc_report_check` | Bulk index-status sweep across a property |
| `gsc_report_discover` | Find URLs Google has not indexed |
| `gsc_request_indexing` | Submit a URL for indexing, quota permitting |
| `gsc_submit_sitemaps` | Submit or resubmit sitemaps |
| `gsc_performance` | Clicks, impressions, CTR and position from Search Analytics |
| `gsc_quota` | What is left today, per property |
| `gsc_doctor` | Diagnose auth, config and environment problems |

## Why quota accounting is the hard part

Most tools in this space get Google's limits wrong, then get throttled and blame detection. Both limits that matter are **per property, not per account**:

| Limit | Value | Mechanic |
|---|---|---|
| Request Indexing | ~11 slots per property | Rolling — each slot frees 24h + 1 min after its own use |
| URL Inspection | 2,000 per day per property | Daily reset |
| URL Inspection | 600 per minute per property | Rate limit |

Properties are independent, so eight properties means eight independent budgets. This server tracks slots individually rather than counting a daily total, so it knows the exact minute the next slot opens — and it deliberately over-counts rather than under-counts when a race is possible, because a short wait is cheaper than a hard `Quota Exceeded`.

## Requirements

- Python 3.11 or newer
- A Google account with Search Console properties

## Development

```bash
git clone https://github.com/Mrshahidali420/google-search-console-mcp.git
cd google-search-console-mcp
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"   # POSIX: .venv/bin/python
.venv/Scripts/python -m pytest -v
```

## Architecture

`gsc_core` is a standalone engine with no MCP dependency, so it can be driven by the MCP server, a CLI, or a future desktop app without change.

| Module | Responsibility |
|---|---|
| `gsc_core/paths.py` | Where files live, per platform |
| `gsc_core/runlog.py` | Logging to stderr and file — never stdout, which MCP reserves for JSON-RPC |
| `gsc_core/store.py` | SQLite: sites, urls, submissions, jobs, quota slots |
| `gsc_core/quota.py` | Per-property rolling slot accounting |
| `gsc_core/gauth.py` | OAuth 2.0 with PKCE S256, hardened token storage, refresh |
| `gsc_core/config.py` | User-tunable settings with validation |

## Known gaps

Stated plainly, because they are the things a reviewer should look at first:

- No test proves `icacls` actually applied an ACL on Windows — the Windows test only observes that the call was made, so `_harden` could no-op there and the suite would stay green. The POSIX equivalents now execute on Linux and macOS on every push, so this gap is Windows-only.
- Nothing has authenticated against a real Google account. Every OAuth path is tested against fakes.
- Google OAuth verification for the sensitive `webmasters` scope has not started.

## Contributing

Contributions are genuinely welcome — issues, pull requests, bug reports, docs fixes, all of it. Start with [CONTRIBUTING.md](CONTRIBUTING.md). Good first issues are the CI legs listed under Known gaps.

## License

[Functional Source License 1.1 with an Apache 2.0 future grant](LICENSE) (`FSL-1.1-ALv2`).

In plain terms: **use it, modify it, contribute to it, run it on client work — just don't sell a competing product built from it.** Every release converts to Apache 2.0 two years after publication, so nothing here is locked away permanently.
