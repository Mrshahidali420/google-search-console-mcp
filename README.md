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

**Pre-alpha. The whole surface is wired up, including submission; none of it has met a real Google account yet.**

Fourteen tools are registered on the server and covered by a wire-level smoke test that connects a real MCP client session and confirms every tool answers with a description. Storage, quota accounting, OAuth, and config are the foundation underneath them. Sign-in and submission both exist end to end in code and can be walked by hand — see [docs/manual-smoke.md](docs/manual-smoke.md) — but no real Google account has authenticated against this code, and no URL has been submitted through it; see Known gaps.

| Milestone | Scope | State |
|---|---|---|
| 1. Foundation | Paths, logging, SQLite store, quota engine, OAuth + PKCE, config | **Done** |
| 2. MCP surface | The tools below, exposed over MCP | **Done** |
| 3A. Onboarding | Guided sign-in, browser/profile detection, the bridge extension | **Done** |
| 3B. Submission | Browser-driven Request Indexing, job control | **Code complete, unverified** |
| 4. Reporting | Indexation discovery and audits | **Code complete, unverified** |

Watch or star the repo if you want to know when the milestones above are verified against live properties.

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
| `gsc_request_indexing` | Submit up to five URLs for indexing, one at a time. **Blocks for minutes** — see [Submitting URLs](#submitting-urls) |
| `gsc_start_indexing_job` | Queue a background submission run over any number of URLs; returns at once |
| `gsc_job_status` | Progress and state for one submission job, or the most recent |
| `gsc_stop_job` | Ask a running submission job to stop after the URL in flight |
| `gsc_find_unindexed` | Find which of a property's URLs are not indexed, and why — see [Finding what is not indexed](#finding-what-is-not-indexed) |
| `gsc_audit` | The current indexation position for a property, read from the local store; spends no quota |

## Why quota accounting is the hard part

Most tools in this space get Google's limits wrong, then get throttled and blame detection. Both limits that matter are **per property, not per account**:

| Limit | Value | Mechanic |
|---|---|---|
| Request Indexing | ~11 slots per property | Rolling — each slot frees 24h + 1 min after its own use |
| URL Inspection | 2,000 per day per property | Daily reset |
| URL Inspection | 600 per minute per property | Rate limit |

Properties are independent, so eight properties means eight independent budgets. This server tracks slots individually rather than counting a daily total, so it knows the exact minute the next slot opens — and it deliberately over-counts rather than under-counts when a race is possible, because a short wait is cheaper than a hard `Quota Exceeded`.

## Finding what is not indexed

`gsc_find_unindexed` collects candidate URLs — from the property's registered sitemaps, from URLs already in the local store, or both — inspects the ones whose last inspection has gone stale, and reports each unindexed URL with a reason. `limit` caps how many URLs are **inspected**, not how many come back: an inspection spends budget, and a cap that only trimmed the output would pay full price for an answer it discarded. Which URLs a capped run reaches follows the store's own URL ordering (alphabetical), not staleness, so a capped run is a sample rather than a worst-first sweep.

`gsc_audit` answers the same question from the store alone — no HTTP inspection, no budget spent. It is point-in-time: it reports what the last inspection found, and carries `as_at` and a `stale` count so you can tell how old that picture is. There are deliberately no movement numbers.

### Reason codes

There are **ten** of them. `submitting_helps` on each row is the one to act on before calling `gsc_request_indexing`:

| Reason | `submitting_helps` | What it means |
|---|---|---|
| `discovered-not-indexed` | yes | Google knows the URL but has not crawled it |
| `unknown-to-google` | yes | Google has never seen the URL |
| `crawled-not-indexed` | yes | Google crawled it and chose not to index it — improve it, **then** submit |
| `404` | no | The URL returns not-found |
| `redirect` | no | The URL redirects elsewhere |
| `noindex` | no | A noindex directive on the page or its response |
| `soft-404` | no | Returns 200 but reads as an error or empty page |
| `robots-blocked` | no | robots.txt blocks the URL |
| `duplicate` | no | Google chose a different canonical |
| `alt-canonical` | no | An alternate page pointing at its own canonical — no action |

If you have read the eight-code list in the design notes and counted ten here, the extra two are `discovered-not-indexed` and `unknown-to-google`, kept separate from `crawled-not-indexed` on purpose. All three answer *yes* to "can a quota slot move this", but not to "what do I do first", and that is the part you act on. Discovered and unknown are pages Google has not judged — it has not fetched them yet, so submitting is the whole remedy. Crawled-not-indexed is a page it fetched and passed on; a fresh crawl can reverse that verdict, but resubmitting byte-identical content re-crawls to the same one. Improve the page, then submit it.

Slots are roughly eleven per property per rolling day and unrecoverable, and `crawled-not-indexed` is usually the biggest bucket on a real site. `submitting_helps: true` means the slot *can* work, not that today is the day to spend it.

A URL whose state could not be established — a failed inspection, or a result a re-check could not confirm — is reported as `undetermined`, never as unindexed. Absence of evidence is not a finding.

## Submitting URLs

Google has no API for Request Indexing that a tool like this can use, so submission goes through **your own browser**: the bridge extension, loaded into the profile you paired during `gsc_setup`, clicks Request Indexing in a real Search Console session. That has three consequences worth knowing before you spend anything.

**It is slow, and the slowness is the feature.** Submissions are paced 130–180 seconds apart. That gap is not a placeholder and not a bug report — it is the interval proven not to draw a throttle over long runs. Five URLs is therefore up to fifteen minutes of wall clock, and the tool that does it blocks for all of it.

**Quota is per property and small.** Roughly eleven slots per property, on a rolling 24-hour window — each slot frees 24 hours and a minute after its own use, not at midnight. Properties are independent budgets. Call `gsc_quota` first and act on `spendable_free`, not `free`: `spendable_free` subtracts the `daily_reserve` you set aside in config. A spent slot is unrecoverable; there is no undo.

**One run at a time.** The bridge drives a single browser tab in your real profile and listens on one fixed local port, so a second run while one is going is refused outright rather than queued. That covers both directions: `gsc_start_indexing_job` while a job or a `gsc_request_indexing` call is in flight, and `gsc_request_indexing` while a job is running.

### Which tool

| You have | Use | Because |
|---|---|---|
| One to five URLs, and you can wait | `gsc_request_indexing` | Synchronous. Returns the outcome of every URL. **Hard-capped at five** — `sync_submit_cap` in config can lower that, never raise it. |
| More than five, or you want your session back | `gsc_start_indexing_job` | Returns a `job_id` immediately; the run continues in the background. No cap. |
| A job in flight | `gsc_job_status` | Progress, per-URL results, and whether a worker is still on it. Called with no argument it reports the most recent job. |
| A job you want to end | `gsc_stop_job` | Stops after the URL in flight, never mid-URL: a submission already sent has spent its slot and its ledger row has to settle with the real outcome. |

### What a run does when things go wrong

A run **stops early** rather than burning the rest of the batch against a server that is already refusing: a `quota_exceeded`, a captcha, a rate limit, or a signed-out session ends it. `stopped_early` and `stop_reason` in the result say so, and the URLs never attempted keep their slots. A job that ended this way lands in state `stopped_throttled`; one you stopped by hand lands in `stopped_user`.

URLs that could not be routed to any known property come back as `no_property`, and ones that found no spendable slot as `no_quota`. Neither reached the browser and neither cost anything — they are reported apart from failures on purpose, because the fixes are different: run `gsc_list_sites` for the first, wait for the second.

If the server is restarted while a job is running, that job's row is closed out as `failed` at the next startup, and any submission row left open is settled against the property's ledger. Nothing is silently resumed — a background worker does not survive the process that owns it.

### Before your first submission

The extension must be loaded in the browser profile you paired, and the browser must be one this server can drive. `gsc_setup` walks both; `gsc_doctor` re-checks them. If the browser is closed, `auto_launch_browser` (on by default) opens it. The first run of all pairs the extension to the bridge, which needs the browser window in front of you.

## Requirements

- Python 3.11 or newer
- A Google account with Search Console properties
- A Chromium browser — Chrome, Brave, Edge, Vivaldi, Opera or Chromium. `gsc_setup`
  loads a browser extension into one of your existing profiles, and there
  is no way to complete setup without one. Firefox and Safari are not
  Chromium and will not work.
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
Until a published app ships an embedded client, every install needs its own:

All of this happens in [Google Cloud Console](https://console.cloud.google.com/),
free, and takes about five minutes. Do the steps in order — step 4 is the
one people skip, and skipping it fails at the very end.

1. **Create a project.** Use the project dropdown in the top bar →
   **New project**. Any name. Wait for it to be created, then make sure it
   is the project selected in that dropdown; everything below applies to
   the selected project only.

2. **Enable the Search Console API.** Search "Search Console API" in the
   console's search bar, open it, and click **Enable**. Without this, every
   call returns a 403 that mentions the API being disabled.

3. **Configure the OAuth consent screen.** In the left menu: **APIs &
   Services → OAuth consent screen**.
   - User type: **External**. ("Internal" only exists on Workspace
     accounts and restricts the app to your own organisation.)
   - App name: anything — you are the only user who will see it.
   - User support email and developer contact email: your own address.
   - You do **not** need to add scopes here. This server requests its
     scope at sign-in time, and adding it on this screen does not change
     what you are granted.
   - Save through to the end.

4. **Publish the app** — or add yourself as a Test user. Either works;
   publishing is better, and this is the step people skip.

   Every new app starts in **Testing** status, where Google lets only
   listed test users sign in. Everyone else gets `403: access_denied` on
   the consent screen — *after* creating the project, enabling the API,
   creating the client, setting the environment variables and running
   `gsc_setup`. Nothing earlier warns you. A Testing app also expires its
   refresh token every 7 days, so sign-in breaks a week later in a way
   that looks like a bug here and is not.

   Both Search Console scopes are classed **non-sensitive** by Google, so
   publishing costs nothing: no verification, no security assessment, no
   user cap. **Audience → Publishing status → Publish app**, and both
   problems above disappear.

   If you would rather stay in Testing, find **Test users** (on newer
   consoles: **Audience → Test users**) and add the Google address that
   owns your Search Console properties. That works indefinitely for your
   own account, at the cost of signing in again every 7 days.

5. **Create the OAuth client.** **APIs & Services → Credentials → Create
   credentials → OAuth client ID**, application type **Desktop app**.
   Copy the client ID and client secret it shows you — the secret is
   shown once, though you can always create another client.

   Desktop app is the right type: this server signs you in over a
   loopback redirect on `127.0.0.1`, which is what that type allows. Do
   not pick "Web application" and do not add a redirect URI by hand.

6. Set the two environment variables below before starting the server.
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
background service worker is alive is a separate question, and only the
bridge can answer it: at submission time it waits for a connection, wakes
an evicted worker if none arrives, and fails with `extension_not_connected`
if that does not work either. `gsc_doctor` still reports registration only.

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
| `gsc_core/bridge.py` | The localhost WebSocket server the browser extension connects back to |
| `gsc_core/submit.py` | The per-URL submission loop: routing, atomic quota reservation, outcomes |

## Privacy

To tell you which browser profile to use, this tool reads the profile list and the signed-in account address out of the browser's own files on your machine — `Local State` and each profile's `Preferences` / `Secure Preferences`. The same files are read a second time, for a different reason, to find out whether the bridge extension is loaded in that profile and at what version.

That read is entirely local. The address is used in memory to show you which profile is signed into which account, and:

- it is never transmitted anywhere,
- it is never written to disk by this tool,
- it is never written to the log, at any level — failures reading these files are logged by exception type name only, precisely so that neither an address nor a path containing your Windows username can end up in a log file you might attach to a bug report.

Nothing in the tool opens these files for writing. No address is ever returned by a tool either, in either direction — not one found in a profile, and not your own authorised address. A tool result is rendered into a transcript and retained by whatever MCP client is driving the server, none of which this project controls, so a profile is identified by browser and profile directory and nothing else.

One path is the exception, and it is deliberate. When `gsc_setup` finds the bridge extension is not loaded yet, its result carries the directory the extension was unpacked to — on Windows, `C:\Users\<your username>\AppData\Roaming\gsc-mcp\extension`. On most machines that path contains your operating-system account name. It is returned because "Load unpacked" in Chrome asks you to pick that exact folder, and a set-up instruction you cannot follow is not a privacy win. It is the only path any tool returns, it appears only in the one step that needs it, and it is never written to the log. If your account name is something you would rather not have in a transcript, run that step, then clear the transcript.

## Known gaps

Stated plainly, because they are the things a reviewer should look at first:

- No test proves `icacls` actually applied an ACL on Windows — the Windows test only observes that the call was made, so `_harden` could no-op there and the suite would stay green. The POSIX equivalents now execute on Linux and macOS on every push, so this gap is Windows-only.
- **The submission path exists in code and has never submitted a URL.** Every layer of it — the extension bridge, the pacing, the quota reservation, the run loop, the job worker — is exercised against fakes only. No browser has been driven, no Request Indexing button has been clicked, and no slot has been spent by this code. The states a fake cannot reach are the submission pass in [docs/manual-smoke.md](docs/manual-smoke.md); until someone walks it, "it submits URLs" is a claim the test suite cannot support.
- **No real Google account has authenticated against this code.** Every OAuth path is exercised against fakes; the live path is [docs/manual-smoke.md](docs/manual-smoke.md), and that checklist has not yet been run for real. Until it has, "you can sign in" is a claim the test suite cannot support.
- **The bridge port is fixed at 8765 with no collision handling.** A second `gsc-mcp` process, or anything else already on that port, fails to bind and the submission tool reports it as an unexpected error. Making the port dynamic needs a matching extension change.
- macOS and Linux browser detection has only ever run against fixtures, never on real hardware, here or in CI. The first person to run the smoke checklist on a Mac or a Linux box is performing that test.
- `gsc_detect_browsers` reports `matches_authorised_account` as `null` on every real machine today. The flag reads an `account_email` key from the stored token, and nothing writes it: the current scope set returns no identity claim and the consent step does not record the authorising account. The plumbing is correct and inert. Treat the field as "unknown", not as "does not match".
- The `extension` check reports whether the extension is **registered**, not whether it is working. Only the bridge learns whether the MV3 worker is alive, and only at submission time.
- No OAuth client is embedded yet, so users must supply their own Google Cloud credentials (see Install above). Google classes both Search Console scopes as **non-sensitive**, so the app that will carry that client needs no verification and no user cap — only publishing.
- `mcp` is pinned `>=1.2,<2.0`. `mcp` 2.0 removed `FastMCP` outright — confirmed directly against the 2.0 wheel, which has no `fastmcp` module at all — so this server does not receive any `mcp` 2.x fixes, and it hard-conflicts with any other installed package that requires `mcp>=2`. Lifting the ceiling means porting this server to whatever construction API replaced it.

## Contributing

Contributions are genuinely welcome — issues, pull requests, bug reports, docs fixes, all of it. Start with [CONTRIBUTING.md](CONTRIBUTING.md). Good first issues are the CI legs listed under Known gaps.

## License

[Functional Source License 1.1 with an Apache 2.0 future grant](LICENSE) (`FSL-1.1-ALv2`).

In plain terms: **use it, modify it, contribute to it, run it on client work — just don't sell a competing product built from it.** Every release converts to Apache 2.0 two years after publication, so nothing here is locked away permanently.
