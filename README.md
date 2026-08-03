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

Eight tools are registered on the server and covered by a wire-level smoke test that connects a real MCP client session and confirms every tool answers with a description. Storage, quota accounting, OAuth, and config are the foundation underneath them. The sign-in path now exists end to end and can be walked by hand — see [docs/manual-smoke.md](docs/manual-smoke.md) — but the submission path does not, and no real Google account has authenticated against this code yet; see Known gaps.

| Milestone | Scope | State |
|---|---|---|
| 1. Foundation | Paths, logging, SQLite store, quota engine, OAuth + PKCE, config | **Done** |
| 2. MCP surface | The tools below, exposed over MCP | **Done** |
| 3A. Onboarding | Guided sign-in, browser/profile detection, the bridge extension | **Done** |
| 3B. Submission | Browser-driven Request Indexing, job control | Next |
| 4. Reporting | Indexation audits, discovery loops, bulk runs | Planned |

Watch or star the repo if you want to know when Milestone 3B ships.

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
| `gsc_setup` | Walk through sign-in and setup; idempotent, returns the single next step |
| `gsc_detect_browsers` | Locate installed browsers and profiles for browser-driven submission |

### Planned

Not yet built — tracked for future milestones:

| Tool | What it does | Milestone |
|---|---|---|
| `gsc_request_indexing` | Submit a URL for indexing, quota permitting | 3B |
| `gsc_start_indexing_job` | Kick off a batch indexing-request job | 3B |
| `gsc_job_status` | Check on a running indexing job | 3B |
| `gsc_stop_job` | Cancel a running indexing job | 3B |
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

## Getting started

Once the server is installed and connected, the whole of setup is one tool
called repeatedly. `gsc_setup()` is idempotent: it never resumes a session,
it re-reads the whole state on every call, and it hands back the single
next thing to do. Call it in a loop until it returns `ok: true`.

**1. Install and connect** — see [Install](#install) above.

**2. Set the two environment variables.** `GSC_MCP_CLIENT_ID` and
`GSC_MCP_CLIENT_SECRET`, from your own Google Cloud OAuth client. A release
build will eventually embed a verified client and this step will go away;
it has not shipped, and `EMBEDDED_CLIENT_SECRET` in `gsc_mcp/deps.py` is an
empty string by design because a real secret can never be committed to a
public repository.

**3. Run `gsc_setup()`.** It opens a Google consent screen in your browser
and returns the URL as well, so a headless machine can still complete it by
hand. Approve it, then call `gsc_setup()` again — the second call collects
the redirect, stores the token, and moves on. Nothing about your sign-in is
returned to the caller: not the token, not the authorization code, not the
PKCE verifier.

**4. Load the bridge extension.** `gsc_setup()` will tell you where it
extracted the extension to and which browser profile to load it into. In
that browser: open its extensions page (`chrome://extensions`,
`brave://extensions`, `edge://extensions`, and so on — `gsc_setup()` gives
you the exact URL for your browser), turn on **Developer mode**, choose
**Load unpacked**, and select the folder it named.

> **You will see a warning banner, and it is expected.** The extension asks
> for the `debugger` permission, so Chromium shows a prominent bar saying
> an extension is debugging your browser, and may warn you when you enable
> Developer mode. That permission is not incidental. Search Console applies
> a soft throttle to Request Indexing clicks that did not come from a real
> pointer, and synthetic DOM clicks trip it. The extension therefore issues
> *trusted* input events through the Chrome DevTools Protocol instead,
> which is what `debugger` grants and the only way to grant it. The banner
> is Chromium correctly reporting a real capability — read it as "yes, this
> is the extension you just installed", not as malware. It only ever
> attaches to `search.google.com`, the one host in its `host_permissions`.

**5. Run `gsc_doctor()`.** Seven checks, in order: `oauth_client`, `token`,
`config`, `store`, `properties`, `browser`, `extension`. Every failing one
carries a concrete `fix`. Sample output, on a machine where everything is
working:

```json
{
  "ok": true,
  "checks": [
    {"name": "oauth_client", "ok": true, "detail": "configured", "fix": ""},
    {"name": "token", "ok": true, "detail": "token file present", "fix": ""},
    {"name": "config", "ok": true, "detail": "config valid", "fix": ""},
    {"name": "store", "ok": true, "detail": "schema version 2", "fix": ""},
    {"name": "properties", "ok": true, "detail": "2 properties", "fix": ""},
    {"name": "browser", "ok": true,
     "detail": "Google Chrome / Default is the profile to use", "fix": ""},
    {"name": "extension", "ok": true,
     "detail": "the gsc-mcp bridge extension is installed in Google Chrome / Default at version 1.10.0; whether its background service worker is running is not checked in this milestone",
     "fix": ""}
  ]
}
```

### Reading the browser and extension checks

These two are about your local machine rather than your Google account, and
they are worded carefully because the failure modes are easy to misread.

**"Could not be checked" never means "not installed."** The extension check
reads your browser's own preferences files. Those files are frequently
locked, mid-write, cloud-synced, or held open by antivirus. When a read
does not happen, the check says the question *could not be checked* and
that the extension may already be there. That is not a polite way of saying
it is missing, and the fix is to run `gsc_doctor()` again (closing the
browser first if it is running) — **not** to reinstall an extension that is
sitting right where you put it. The same distinction runs through
`gsc_detect_browsers`, whose `has_extension` field is three-valued: `true`
present, `false` every preferences file was read and it was not among them,
`null` the check could not be performed.

**Microsoft Edge can report a Microsoft account where a Google one is
expected.** Edge stores signed-in account addresses in the same file and
the same key Chrome uses for Google accounts, but by default it signs
profiles in to *Microsoft* identities. Nothing on disk tells the two apart.
So for an Edge profile, an address found is not evidence of a Google
sign-in — and if your Microsoft address happens to be the same as your
Google one, what looks like a confirmed match is not confirmed at all. The
tools hedge this rather than assert it: an Edge profile is reported with
"this profile's Google sign-in could not be confirmed". Check it yourself
before relying on it. Brave, Vivaldi, Opera and plain Chromium record no
Google account at all and are hedged for the different reason that there is
nothing to read.

**A changed extension ID means re-pairing, not breakage.** The extension
ships with no manifest `key`, so Chromium derives its ID by hashing the
absolute path it was loaded from. That ID is stable for as long as the
extraction directory is stable — and it changes if that directory moves:
an upgrade that relocates the config directory, a different `GSC_MCP_HOME`,
a migration to a new machine. When it changes, the extension check stops
recognising the loaded copy and reports it as not installed. Nothing is
broken and nothing is corrupted; the fix is to load the unpacked extension
again from the new folder, exactly as in step 4.

**A green `extension` check means registered, not running.** It says the
extension is loaded into that profile at that version. Whether its MV3
background service worker is alive needs a live connection from the bridge,
and that check arrives with Milestone 3B.

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
| `gsc_core/browsers.py` | Which Chromium browsers are installed, and where they keep their state |
| `gsc_core/profiles.py` | Which profiles each browser has, and which Google account is signed into each |
| `gsc_core/pairing.py` | Where the bridge extension is extracted to, and what ID Chromium gave it |

## Privacy

To tell you which browser profile to use, this tool reads the profile list and the signed-in account address out of the browser's own files on your machine — `Local State` and each profile's `Preferences` / `Secure Preferences`. The same files are read a second time, for a different reason, to find out whether the bridge extension is loaded in that profile and at what version.

That read is entirely local. The address is used in memory to show you which profile is signed into which account, and:

- it is never transmitted anywhere,
- it is never written to disk by this tool,
- it is never written to the log, at any level — failures reading these files are logged by exception type name only, precisely so that neither an address nor a path containing your Windows username can end up in a log file you might attach to a bug report.

Nothing in the tool opens these files for writing. No address is ever returned by a tool either, in either direction — not one found in a profile, and not your own authorised address. A tool result is rendered into a transcript and retained by whatever MCP client is driving the server, none of which this project controls, so a profile is identified by browser and profile directory and nothing else.

## Known gaps

Stated plainly, because they are the things a reviewer should look at first:

- No test proves `icacls` actually applied an ACL on Windows — the Windows test only observes that the call was made, so `_harden` could no-op there and the suite would stay green. The POSIX equivalents now execute on Linux and macOS on every push, so this gap is Windows-only.
- **The sign-in path now exists and is testable; the submission path still does not.** `gsc_setup`, `gsc_detect_browsers` and the two new `gsc_doctor` checks make it possible to walk from a clean install to a signed-in, extension-loaded machine. Nothing yet submits a URL for indexing — `gsc_request_indexing` and the job tools land in Milestone 3B.
- **No real Google account has authenticated against this code.** Every OAuth path is exercised against fakes; the live path is [docs/manual-smoke.md](docs/manual-smoke.md), and that checklist has not yet been run for real. Until it has, "you can sign in" is a claim the test suite cannot support.
- macOS and Linux browser detection has only ever run against fixtures, never on real hardware, here or in CI. The first person to run the smoke checklist on a Mac or a Linux box is performing that test.
- `gsc_detect_browsers` reports `matches_authorised_account` as `null` on every real machine today. The flag reads an `account_email` key from the stored token, and nothing writes it: the current scope set returns no identity claim and the consent step does not record the authorising account. The plumbing is correct and inert. Treat the field as "unknown", not as "does not match".
- The `extension` check reports whether the extension is **registered**, not whether it is working. MV3 worker-liveness detection needs a live bridge connection and arrives with 3B.
- Google OAuth verification for the sensitive `webmasters` scope has not started.
- No OAuth client is embedded yet, so users must supply their own Google Cloud credentials (see Install above).
- `mcp` is pinned `>=1.2,<2.0`. `mcp` 2.0 removed `FastMCP` outright — confirmed directly against the 2.0 wheel, which has no `fastmcp` module at all — so this server does not receive any `mcp` 2.x fixes, and it hard-conflicts with any other installed package that requires `mcp>=2`. Lifting the ceiling means porting this server to whatever construction API replaced it.

## Contributing

Contributions are genuinely welcome — issues, pull requests, bug reports, docs fixes, all of it. Start with [CONTRIBUTING.md](CONTRIBUTING.md). Good first issues are the CI legs listed under Known gaps.

## License

[Functional Source License 1.1 with an Apache 2.0 future grant](LICENSE) (`FSL-1.1-ALv2`).

In plain terms: **use it, modify it, contribute to it, run it on client work — just don't sell a competing product built from it.** Every release converts to Apache 2.0 two years after publication, so nothing here is locked away permanently.
