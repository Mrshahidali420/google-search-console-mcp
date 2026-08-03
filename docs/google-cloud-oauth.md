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

`EMBEDDED_CLIENT_ID` and `EMBEDDED_CLIENT_SECRET` are empty strings
(`deps.py:37-38`). Decision D1 says the product ships its own client so a
user just clicks Allow. Both can be true, because **the repo and the
released package are different places**.

A Desktop-app client secret is not confidential — an installed app cannot
keep one, which is why PKCE exists, and anyone can unzip a wheel and read
it. So embedding it in a release is normal practice. What committing it to
a *public repo* costs you is cheap rotation: a value in git history is
permanent, while a value baked into a build can be changed in the console
and rebuilt whenever you want.

So: keep the constants empty in git, and inject at build time. The
mechanism to use is a gitignored `_embedded.py` that `deps.py` imports
inside a `try/except ImportError`, falling back to `""`. CI writes that
file from repository secrets immediately before `python -m build`. No
tracked source is mutated, the fallback stays explicit and testable, and a
source checkout behaves exactly as it does today.

This is not yet built.
