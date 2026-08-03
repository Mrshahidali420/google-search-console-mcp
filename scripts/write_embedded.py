"""Generate src/gsc_mcp/_embedded.py from the environment.

Run immediately before `python -m build` to bake this project's own OAuth
client into a release wheel (decision D1), so an installed user never has
to create one in the Cloud Console:

    GSC_MCP_CLIENT_ID=... GSC_MCP_CLIENT_SECRET=... python scripts/write_embedded.py

The generated file is gitignored. Nothing here writes to a tracked path,
and this script never prints either value — CI logs are public on a public
repository, and a secret echoed into a build log is as leaked as one
committed.

One script rather than a shell line in the workflow and a second one in the
docs: the generated module's attribute names have to match what
deps._embedded_client() reads, and two copies of that contract drift.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parents[1] / "src" / "gsc_mcp" / "_embedded.py"

TEMPLATE = '''"""The OAuth client for this build. GENERATED — do not edit, do not commit.

Written by scripts/write_embedded.py at build time. This file is
gitignored; see deps._embedded_client() for how it is read and why its
absence is a supported state rather than an error.
"""
from __future__ import annotations

CLIENT_ID = {client_id!r}
CLIENT_SECRET = {client_secret!r}
'''


def main() -> int:
    client_id = os.environ.get("GSC_MCP_CLIENT_ID", "")
    client_secret = os.environ.get("GSC_MCP_CLIENT_SECRET", "")

    # Refuse rather than write a half-populated file. A build whose secret
    # was unset would otherwise produce a wheel that imports fine and fails
    # only when a user tries to sign in — the failure furthest from the
    # cause. deps tolerates a half-written file so a bad build degrades
    # gracefully; this makes sure it does not get made in the first place.
    missing = [name for name, value in
               (("GSC_MCP_CLIENT_ID", client_id),
                ("GSC_MCP_CLIENT_SECRET", client_secret)) if not value]
    if missing:
        print(f"refusing to write {TARGET.name}: {', '.join(missing)} not set",
              file=sys.stderr)
        return 1

    TARGET.write_text(
        TEMPLATE.format(client_id=client_id, client_secret=client_secret),
        encoding="utf-8")
    # Names the file, never the values.
    print(f"wrote {TARGET.relative_to(Path.cwd())}"
          if TARGET.is_relative_to(Path.cwd()) else f"wrote {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
