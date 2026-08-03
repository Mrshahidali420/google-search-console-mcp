"""Write the client.json that gets uploaded to the `client` release tag.

Run this to publish the shipped OAuth client for the first time, or to
rotate it after changing the secret in the Cloud Console:

    GSC_MCP_CLIENT_ID=... GSC_MCP_CLIENT_SECRET=... python scripts/publish_client.py
    gh release create client --title "Bundled OAuth client" --notes "..."   # first time only
    gh release upload client client.json --clobber

A DEDICATED TAG, never `latest`. `shipped_client.CLIENT_URL` points at
`releases/download/client/client.json`, which no version release touches.
Cutting 0.2.0 does not involve this file, and rotating this file does not
require a release — the two lifecycles stay apart, and there is no release
that can break first-run setup by forgetting to carry the asset.

The output file is gitignored and must never be committed: a secret in git
history is permanent, and GitHub's scanner reports Google client secrets to
Google, which can revoke them — breaking every user of every release at
once. A release asset is not scanned. That is the entire reason this is an
upload rather than a tracked file.

Nothing here prints either value: this is run from a shell whose history is
kept, and possibly from CI whose logs are public.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

TARGET = Path.cwd() / "client.json"
ID_SUFFIX = ".apps.googleusercontent.com"


def main() -> int:
    client_id = os.environ.get("GSC_MCP_CLIENT_ID", "")
    client_secret = os.environ.get("GSC_MCP_CLIENT_SECRET", "")

    missing = [name for name, value in
               (("GSC_MCP_CLIENT_ID", client_id),
                ("GSC_MCP_CLIENT_SECRET", client_secret)) if not value]
    if missing:
        print(f"refusing to write {TARGET.name}: {', '.join(missing)} not set",
              file=sys.stderr)
        return 1

    # The same check shipped_client._validate applies on download, made
    # here so a typo is caught before the asset is published rather than by
    # every user's first run.
    if not client_id.endswith(ID_SUFFIX):
        print(f"refusing to write {TARGET.name}: GSC_MCP_CLIENT_ID does not "
              f"look like a Google client id (expected it to end in "
              f"{ID_SUFFIX})", file=sys.stderr)
        return 1

    TARGET.write_text(
        json.dumps({"client_id": client_id, "client_secret": client_secret},
                   indent=2),
        encoding="utf-8")
    # ASCII only: a Windows console on a legacy code page raises
    # UnicodeEncodeError on an em dash, which would fail the script AFTER
    # it had already written the file.
    print(f"wrote {TARGET.name} - upload it with:\n"
          f"  gh release upload client {TARGET.name} --clobber\n"
          f"then delete it locally.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
