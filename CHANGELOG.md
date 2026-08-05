# Changelog

Notable changes to `gsc-mcp`. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[PEP 440](https://peps.python.org/pep-0440/).

## [Unreleased]

### Changed

- **The PyPI distribution name is now `gsc-indexer-mcp`.** `gsc-mcp` on PyPI is
  an unrelated Search Console MCP server published in April 2026 by another
  author, and `google-search-console-mcp` is a third one. Nothing inside this
  project moves: the import package, the `gsc-mcp` console script, the MCP
  server name and the config directory are all unchanged. A second console
  script, `gsc-indexer-mcp`, is installed as an alias, because a name another
  project also claims is a name that can be taken from under you.

### Added

- `.github/workflows/pypi-publish.yml` — builds a wheel and sdist **without**
  the embedded OAuth client, refuses to publish if one is present anyway, and
  uploads via trusted publishing (OIDC, no stored token). TestPyPI or PyPI is a
  dispatch input. The GitHub release wheel keeps its embedded client; a public
  package index is a different exposure, and a secret on one is a revoked
  secret.

## [0.1.0a1] — 2026-08-05

First tagged release. A pre-release on purpose: `pip` will not install it
without `--pre`, because the parts of this that have been exercised against
real Google infrastructure are newer than the parts that have not. See
[Known gaps](README.md#known-gaps) before depending on it.

### What it does

An MCP server over Google Search Console, in fifteen tools. Two halves that
authenticate separately and stay separate:

- **Read paths** use the Search Console API with your own OAuth client —
  property listing, index status, performance, sitemap submission, and the
  discovery/audit tools that find unindexed URLs worth submitting.
- **The submission path** drives Request Indexing in a real browser through a
  Chrome/Brave extension and a localhost bridge, because Google exposes no API
  for it. It paces submissions, reserves quota against a local ledger, and
  runs as a background job you can poll or stop.

`gsc_core` is the engine and imports nothing from `gsc_mcp`, so it stays
usable without the `mcp` dependency.

### Notable behaviour in this release

- **Quota is tracked per property, on a 24-hour rolling window that frees
  slots one at a time** — not per account, and not in daily batches. A refusal
  from Google now records a 15-minute cooldown rather than writing the
  property off for a day; the previous behaviour backfilled the ledger with
  synthetic slots stamped at the moment of refusal and froze a property for
  hours while capacity was returning every few minutes.
- **`gsc_quota` states that its counts are an estimate.** The ledger sees only
  what this tool spent. Submissions made by hand in the browser, from a phone,
  or on another machine are invisible to it, so `used` is a lower bound and
  `free` an upper bound. The payload carries `counts`,
  `free_is_upper_bound`, and `last_refusal_at` to say so.
- **The extension reports how it clicked** (`trusted` via CDP input, or
  `synthetic` via `el.click()` when the debugger cannot attach) and the bridge
  logs it with the verdict. The fallback is silent otherwise, and diagnosing a
  refusal without knowing which one happened costs an afternoon.
- Secrets never reach a log line, an exception message, or a tool return:
  tokens, the PKCE verifier, the bridge token, and account email addresses are
  all excluded by design and by test.

### Distribution

Built by `.github/workflows/release.yml` as a wheel with the OAuth client
embedded, and uploaded as a workflow artifact. **Not published to a package
index** — `pip install` from an index is not yet a thing anyone can do. A source
checkout downloads the OAuth client from the published `client` release asset
on first `gsc_setup()`; that path has been exercised against the live URL.

### Known constraints

- `mcp` is pinned `>=1.2,<2.0`. `mcp` 2.0 removed `FastMCP` outright, so this
  server takes no `mcp` 2.x fixes and hard-conflicts with anything requiring
  `mcp>=2`.
- Requires Python 3.11+.
- The bridge port is fixed at 8765 with no collision handling.
- macOS and Linux browser detection has only ever run against fixtures.
