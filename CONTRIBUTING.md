# Contributing

Contributions are welcome from anyone — open an issue, open a pull request, or just tell me something is wrong. You do not need permission to start.

## Good places to start

The [Known gaps](README.md#known-gaps) section of the README is the honest list of what is weakest right now. In particular:

- **A Windows ACL read-back test.** This is the highest-value contribution available today. `_harden` shells out to `icacls` on Windows, but the only test watches for the call — it cannot tell a real ACL from a complete no-op. A test that writes a token, reads the resulting ACL back, and asserts that no account beyond the owner has read access would close the last permission gap. CI already runs Windows jobs, so it would be enforced from the day it lands.
- **Documentation.** If something in the README confused you, that is a bug in the README.

## Setup

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"   # POSIX: .venv/bin/python
.venv/Scripts/python -m pytest -v
```

The full suite should be green before and after your change. It currently runs in about seven seconds, so there is no excuse for skipping it.

## How this codebase is built

**Tests come first.** Write the failing test, watch it fail, then make it pass. This is not ceremony — during the foundation build, ten tests were found that passed even with the code they were guarding deleted. If you add a test, delete the line it protects and confirm the test actually goes red. A test that cannot fail is worse than no test, because it looks like coverage.

**Nothing in library code writes to stdout.** MCP frames JSON-RPC there, so a stray `print` corrupts the protocol. Use `gsc_core.runlog`.

**Secrets never reach a log or an exception message.** That means the PKCE verifier, the OAuth `state`, the authorization code, access tokens and refresh tokens — at any log level, in any traceback.

**No real client data in the repo.** Tests use `example.com` and reserved addresses. There is a test that enforces this; please do not work around it.

**Every MCP tool call gets its own database connection.** `store.tx()` re-entrancy is connection-scoped, not task-scoped, so two concurrent tasks sharing one connection will silently nest transactions.

## Pull requests

1. Branch off `master`.
2. Keep the change focused — one concern per PR reviews far faster than five.
3. Describe what you changed and why. If it fixes a bug, say how you reproduced it.
4. Green tests, or an explanation of which fail and why.

I read every PR. If yours sits untouched for a week, ping it — that means I missed it, not that I ignored it.

## Licensing of contributions

This project is licensed under the [Functional Source License 1.1 with an Apache 2.0 future grant](LICENSE).

By submitting a contribution you agree that:

- Your contribution is licensed under the same FSL-1.1-ALv2 terms as the rest of the project, including its automatic conversion to Apache License 2.0 two years after each release.
- You grant the maintainer a perpetual, worldwide, irrevocable, royalty-free licence to use, reproduce, modify, sublicense and distribute your contribution, including under different licence terms. This exists so the project can be dual-licensed or relicensed without hunting down every past contributor — a practical necessity, not a claim on your ownership.
- You retain copyright in your contribution.
- You have the right to grant the above — if your employer owns your output, get their sign-off first.

If any of that is a problem for you, open an issue and we will work it out before you spend time on code.

## Code of conduct

Be decent. Disagree about code as much as you like; don't make it personal. I will remove people who make this an unpleasant place to work.
