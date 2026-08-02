# Google Search Console MCP Server

**An MCP server that gives Claude and other AI agents real control over Google Search Console** — list your properties, check whether a URL is indexed, submit sitemaps, pull search analytics, and diagnose setup problems, all from a conversation.

[![CI](https://github.com/Mrshahidali420/google-search-console-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/Mrshahidali420/google-search-console-mcp/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![License: FSL-1.1-ALv2](https://img.shields.io/badge/license-FSL--1.1--ALv2-green)](LICENSE)
[![Status: pre-alpha](https://img.shields.io/badge/status-pre--alpha-orange)](#project-status)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen)](CONTRIBUTING.md)

> Built for SEO practitioners tired of checking index status and submitting sitemaps by hand, one property at a time, and for the AI agents that can do it for them.

---

## Project status

**Pre-alpha. The MCP surface is wired up; browser-driven submission is not.**

Six tools are registered on the server and covered by a wire-level smoke test that connects a real MCP client session and confirms every tool answers with a description. Storage, quota accounting, OAuth, and config are the foundation underneath them. Nothing in this project has ever authenticated against a real Google account — every OAuth path is exercised against fakes; see Known gaps.

| Milestone | Scope | State |
|---|---|---|
| 1. Foundation | Paths, logging, SQLite store, quota engine, OAuth + PKCE, config | **Done** |
| 2. MCP surface | The tools below, exposed over MCP | **Done** |
| 3. Submission | Browser-driven Request Indexing, browser/profile auto-detection | Next |
| 4. Reporting | Indexation audits, discovery loops, bulk runs | Planned |

Watch or star the repo if you want to know when milestone 3 ships.

## Tools

Shipped and registered on the MCP server today:

| Tool | What it does |
|---|---|
| `gsc_list_sites` | List every Search Console property the account can reach |
| `gsc_doctor` | Diagnose auth, config and environment problems |
| `gsc_check_status` | Index status for one or more URLs via the URL Inspection API (read-only, spends no Request-Indexing slot) |
| `gsc_quota` | Request-Indexing and URL Inspection budget remaining today, per property |
| `gsc_performance` | Clicks, impressions, CTR and position from Search Analytics |
| `gsc_submit_sitemaps` | Submit or resubmit sitemaps to a property |

### Planned

Not yet built — tracked for future milestones:

| Tool | What it does | Milestone |
|---|---|---|
| `gsc_setup` | Interactive OAuth client / consent setup | 3 |
| `gsc_detect_browsers` | Locate installed browsers and profiles for browser-driven submission | 3 |
| `gsc_request_indexing` | Submit a URL for indexing, quota permitting | 3 |
| `gsc_start_indexing_job` | Kick off a batch indexing-request job | 3 |
| `gsc_job_status` | Check on a running indexing job | 3 |
| `gsc_stop_job` | Cancel a running indexing job | 3 |
| `gsc_find_unindexed` | Find URLs Google has not indexed | 4 |
| `gsc_audit` | Bulk indexation audit across a property | 4 |

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
- Your own Google OAuth client (see Install below) — **this package does not embed one yet**

## Install

```bash
git clone https://github.com/Mrshahidali420/google-search-console-mcp.git
cd google-search-console-mcp
python -m venv .venv
.venv/Scripts/python -m pip install -e .   # POSIX: .venv/bin/python
```

### You must bring your own OAuth client

`gsc-mcp` does not ship an embedded Google OAuth client. The Google Cloud
app for this project does not exist yet, and a real client secret can
never be committed to a public repository — so `EMBEDDED_CLIENT_ID` and
`EMBEDDED_CLIENT_SECRET` in `gsc_mcp/deps.py` are empty strings by design.
Until a verified app ships, every install needs its own:

1. In [Google Cloud Console](https://console.cloud.google.com/), create a
   project, enable the **Search Console API**, and create an **OAuth 2.0
   Client ID** (Desktop app type).
2. Set the two environment variables below before starting the server.
   Without them, any tool that needs to talk to Google returns
   `{"ok": false, "error": "not_configured", ...}` rather than doing
   anything — `gsc_list_sites`, `gsc_check_status`, `gsc_performance`, and
   `gsc_submit_sitemaps` all behave this way. `gsc_doctor` and `gsc_quota`
   are the two exceptions: `gsc_doctor` still runs and reports
   `oauth_client: not ok` as one line in its checks list rather than
   failing outright, which makes it the right first tool to run when
   something is stuck; `gsc_quota` is local-only and returns `[]` on an
   empty store regardless of OAuth configuration.

```bash
export GSC_MCP_CLIENT_ID="your-client-id.apps.googleusercontent.com"
export GSC_MCP_CLIENT_SECRET="your-client-secret"
# Windows PowerShell:
#   $env:GSC_MCP_CLIENT_ID = "your-client-id.apps.googleusercontent.com"
#   $env:GSC_MCP_CLIENT_SECRET = "your-client-secret"
```

### Connect it to an MCP client

Point your MCP client at the installed console script. For Claude
Desktop, add to its `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "gsc-mcp": {
      "command": "C:\\path\\to\\google-search-console-mcp\\.venv\\Scripts\\gsc-mcp.exe",
      "env": {
        "GSC_MCP_CLIENT_ID": "your-client-id.apps.googleusercontent.com",
        "GSC_MCP_CLIENT_SECRET": "your-client-secret"
      }
    }
  }
}
```

On POSIX, use `.venv/bin/gsc-mcp` as the command instead. Any other
MCP-speaking client that can launch a stdio server works the same way —
the entry point is the `gsc-mcp` console script installed above, and the
server talks the standard MCP stdio transport (`gsc_mcp.server:main`).

This has not yet been exercised against a real Claude Desktop session or
a real Google account; see Known gaps.

## Development

```bash
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
- No OAuth client is embedded yet, so users must supply their own Google Cloud credentials (see Install above).
- `mcp` is pinned `>=1.2,<2.0`. `mcp` 2.0 removed `FastMCP` outright — confirmed directly against the 2.0 wheel, which has no `fastmcp` module at all — so this server does not receive any `mcp` 2.x fixes, and it hard-conflicts with any other installed package that requires `mcp>=2`. Lifting the ceiling means porting this server to whatever construction API replaced it.

## Contributing

Contributions are genuinely welcome — issues, pull requests, bug reports, docs fixes, all of it. Start with [CONTRIBUTING.md](CONTRIBUTING.md). Good first issues are the CI legs listed under Known gaps.

## License

[Functional Source License 1.1 with an Apache 2.0 future grant](LICENSE) (`FSL-1.1-ALv2`).

In plain terms: **use it, modify it, contribute to it, run it on client work — just don't sell a competing product built from it.** Every release converts to Apache 2.0 two years after publication, so nothing here is locked away permanently.
