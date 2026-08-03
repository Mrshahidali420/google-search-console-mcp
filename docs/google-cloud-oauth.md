# Setting up the Google Cloud OAuth app

Everything here happens in the Google Cloud Console — none of it is a code
change. The one code change comes at the end, and it is two lines in
`src/gsc_mcp/deps.py`.

Console UI names move around. If a heading below does not match what you
see, search the console rather than hunting for the exact wording.

## The short version

**Both Search Console scopes are non-sensitive.** The console confirms it:
Google Auth Platform → Data access lists `.../auth/webmasters` and
`.../auth/webmasters.readonly` under *Your non-sensitive scopes*, with
*Your sensitive scopes* and *Your restricted scopes* both empty.

That removes the whole verification track. No review, no third-party
security assessment, no 100-user cap, no homepage or privacy-policy
prerequisite, no demo video. You create the app, you publish it, you are
done — and there is no reason to downgrade to `webmasters.readonly`,
so `gsc_submit_sitemaps` (the one call needing write access,
`api.py:218`) keeps working.

---

## Steps

- [ ] **1. Create a Cloud project.** One project dedicated to this app.
      Don't reuse a project used for client work — the OAuth app's
      identity belongs to the project.

- [ ] **2. Enable the API.** APIs & Services → Library → **Google Search
      Console API** (`searchconsole.googleapis.com`) → Enable.

      Separate from all the consent-screen work, and easy to miss because
      nothing prompts for it. Without it every call returns a 403 saying
      the API is disabled.

      Do **not** enable the "Web Search Indexing API". Despite the name it
      only accepts `JobPosting` and `BroadcastEvent` pages. Request
      Indexing goes through the bundled browser extension precisely
      because Google exposes no public API for it.

- [ ] **3. Configure the consent screen.** Google Auth Platform →
      Branding. App name, user support email, developer contact email.

      The app name shows on the consent screen as "<name> wants access to
      your Google Account". Don't put "Google" in it — names implying
      Google built or endorses the app get rejected. `GSC MCP` is fine.

- [ ] **4. Audience: External.**

- [ ] **5. Add the scope.** Data access → Add or remove scopes →
      `https://www.googleapis.com/auth/webmasters`

      Full `webmasters`, not `.readonly`, because sitemap submission
      writes. This is the only scope the server requests
      (`gauth.py:34`), so adding `.readonly` as well just puts an extra
      line on the consent screen.

- [ ] **6. Publish the app.** Audience → Publishing status → **Publish
      app**.

      Don't skip this. A new app starts in **Testing**, where only listed
      test users can sign in *and Google expires refresh tokens after
      seven days* — sign-in works, then breaks a week later looking
      exactly like a bug in this code. Publishing costs nothing here
      because the scopes are non-sensitive.

      The Audience page keeps showing a "0 users / 100 user cap" bar
      afterwards. Ignore it: by its own wording the cap applies only to
      apps "requesting unapproved sensitive or restricted scopes", and
      this app requests neither. The bar renders for every External app
      regardless.

- [ ] **7. Create the client.** Clients → Create client → **Desktop app**.

      Desktop app is the right type: the server runs on the user's own
      machine and takes the redirect on a loopback address, which is the
      only client type Google allows to do that with an arbitrary port.
      The flow already uses PKCE with S256 (`gauth.py:52-56`), which is
      what actually secures an installed-app client.

- [ ] **8. Point the server at it.** Environment variables, which always
      win over the embedded constants:

      ```
      GSC_MCP_CLIENT_ID=<the client id>
      GSC_MCP_CLIENT_SECRET=<the client secret>
      ```

      Never commit these. See below for why the repo values stay empty.

- [ ] **9. Run the manual smoke test.** `docs/manual-smoke.md`. Nothing in
      this codebase has ever authenticated against a real Google account,
      so this is the first real exercise of the OAuth path. It spends real
      quota slots — roughly eleven per property per rolling day, and they
      do not come back.

---

## Shipping the client to users

Decision D1 says the product ships its own client so a user just clicks
Allow. No tracked file holds that client, and both facts are true at once,
because **the repo, the released package and a release asset are three
different places**.

A Desktop-app client secret is not confidential — an installed app cannot
keep one, which is why PKCE exists, and anyone can unzip a wheel and read
it. So shipping it is normal practice. Committing it to a *public repo*
is the one form that is actually worse, for two reasons:

- git history is permanent, so a rotation never removes the old value;
- GitHub's secret scanning reads repository content and reports Google
  client secrets to Google, which can revoke them. That would break every
  user of every release at once, not just whoever pushed it. Release
  assets are not scanned.

There are therefore two delivery paths, one per audience.

### Installed users: baked into the wheel

Injected at build time. Four pieces:

| Piece | Where |
|---|---|
| Generator | `scripts/write_embedded.py` — writes `src/gsc_mcp/_embedded.py` from `GSC_MCP_CLIENT_ID` / `GSC_MCP_CLIENT_SECRET`, and refuses if either is unset |
| Reader | `deps._embedded_client()` — imports it under `try/except ImportError`, falling back to `("", "")` |
| Ignore rule | `.gitignore` — the generated file never reaches git |
| Packaging | `artifacts` under `[tool.hatch.build.targets.wheel]` — re-includes it, since hatchling excludes VCS-ignored files by default |

That last one is the trap. Without it the build still *succeeds* and
silently produces a wheel with no client, which is why
`.github/workflows/release.yml` unpacks the wheel it just built and fails
if the client is missing or empty.

To cut a release: **Actions → Release build → Run workflow**. It needs two
repository secrets under Settings → Secrets and variables → Actions,
`GSC_MCP_CLIENT_ID` and `GSC_MCP_CLIENT_SECRET`. The wheel lands as a build
artifact; publishing it to PyPI is a separate decision and needs its own
token.

To do the same locally:

```
GSC_MCP_CLIENT_ID=... GSC_MCP_CLIENT_SECRET=... python scripts/write_embedded.py
python -m build --wheel
```

### Source checkouts: downloaded on first setup

A wheel carries the client; a `git clone` cannot. So a source checkout
downloads it the first time `gsc_setup()` runs and caches it in
`GSC_MCP_HOME/client.json`, hardened to the current user like the token
file. Every run after that is offline.

The asset hangs off a **permanent `client` tag**, not `latest`:

```
https://github.com/Mrshahidali420/google-search-console-mcp/releases/download/client/client.json
```

Version releases never touch it, and rotating it never needs a release —
which also means there is no release that can break first-run setup by
forgetting to carry the asset. To publish or rotate:

```
GSC_MCP_CLIENT_ID=... GSC_MCP_CLIENT_SECRET=... python scripts/publish_client.py
gh release create client --title "Bundled OAuth client" --notes "..."   # first time only
gh release upload client client.json --clobber
```

Then delete the local `client.json`. It is gitignored, and
`test_embedded_client.py` asserts that line still exists.

Be clear about what this is and is not. That URL is in public source and
serves the client to anyone who requests it — no more private than a wheel
anyone can unzip. The gain is **operational**: rotate in one place, revoke
if abused, and nothing for the scanner to find. The security of an
installed-app client is PKCE, unchanged either way.

`shipped_client` validates what comes back before caching it — HTTPS on
every redirect hop, a size cap enforced while reading, both fields
present and non-empty, and a `client_id` ending in
`.apps.googleusercontent.com`. Without that last check a captive portal's
login page caches successfully and surfaces days later as an opaque
`invalid_client` from Google, mid-consent.

### Precedence, and the test suite

`deps.oauth_client()` reads, in order: **environment → embedded (build) →
cached download**. All three are local reads; only `gsc_setup()` ever
touches the network. So a self-hoster can point any build at their own
Cloud project with `GSC_MCP_CLIENT_ID` / `GSC_MCP_CLIENT_SECRET` without
patching source or clearing a cache. Each source supplies both halves or
neither — a half-set environment falls through whole rather than pairing
one exported variable with a cached secret.

The test suite runs as if no client existed anywhere: `tests/conftest.py`
blanks the embedded constants, blanks the cache, and makes any request for
the shipped client raise. Without the first two, the suite's result would
depend on files that happen to exist on the machine running it. Without
the third, every "no client configured" test silently makes a live request
to GitHub — and still passes, because a 404 and being offline produce the
same next step.
