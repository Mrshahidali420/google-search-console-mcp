# Security policy

## Reporting a vulnerability

**Please do not open a public issue.** Use
[GitHub's private advisory form](https://github.com/Mrshahidali420/google-search-console-mcp/security/advisories/new),
which reaches the maintainer without disclosing anything.

Expect an acknowledgement within a few days. This is a small project with one
maintainer, so please allow reasonable time for a fix before disclosing.

## Supported versions

Only the latest release. This project is at `0.1.0`; there are no maintained
branches behind it.

## What this software has access to

Worth stating plainly, because it is more than most MCP servers ask for:

- **A Google account with Search Console access**, via OAuth. Tokens are stored
  in `token.json` under your `GSC_MCP_HOME` (`%APPDATA%\gsc-mcp` on Windows,
  `~/.config/gsc-mcp` elsewhere), on your machine only.
- **A real browser session.** Request Indexing has no public API, so submission
  drives your actual signed-in browser through a bundled extension. That
  extension can act as you, in Search Console, in whichever profile it is
  loaded into.
- **A local WebSocket bridge** on `127.0.0.1:8765`, authenticated with a token
  in `bridge_token.txt`. It listens on loopback and pairs against the resolved
  browser profile.

Nothing is transmitted to the author or to any third party. There is no
telemetry. See [Privacy](README.md#privacy) in the README.

## Things worth reporting

Beyond the usual, these are specific to how this project works and are treated
as serious:

- The **bridge token** appearing in `gsc.log`, in a tool result, or anywhere
  else it could be read. It is designed never to be logged.
- Any path by which a process other than the paired extension can drive the
  bridge, or by which a page can reach it.
- An **OAuth client secret** reachable from a published artifact. The PyPI
  wheel is built deliberately without one and CI fails the run if it appears
  there; a wheel on PyPI that contains it is a defect, not a feature.
- Tokens or credentials written outside `GSC_MCP_HOME`, or written with
  permissions wider than the user.

## Not vulnerabilities

- That the extension can act as you in Search Console. That is what it is for,
  and it is why it only ever runs in a browser profile you loaded it into
  yourself.
- Spending Request Indexing quota. Slots are finite and unrecoverable by
  design; the tool documents this rather than preventing it.
- The GitHub release wheel containing an OAuth client. That build is
  intentional and documented — release assets are not scanned or mirrored the
  way PyPI is.
