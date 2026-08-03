# Setting up the Google Cloud OAuth app

Everything in this file happens in the Google Cloud Console and in your own
web hosting — none of it is a code change, and none of it can be done from
this repo. The one code change at the end is two lines in
`src/gsc_mcp/deps.py`.

Console UI names move around. If a heading below does not match what you
see, search the console for the API or setting rather than hunting for the
exact wording.

---

## Phase A — a working client, today

This unblocks the manual smoke test in `docs/manual-smoke.md`, which has
never run, because nothing in this codebase has ever authenticated against
a real Google account. No verification is needed for this phase.

- [ ] **1. Create a Cloud project.** One project, dedicated to this app.
      Do not reuse a project you already use for client work — the OAuth
      app's identity, and later its verification, belongs to the project.

- [ ] **2. Enable the API.** APIs & Services → Library → **Google Search
      Console API** (`searchconsole.googleapis.com`) → Enable.

      Do **not** enable the separate "Web Search Indexing API". Despite the
      name it only accepts `JobPosting` and `BroadcastEvent` pages, and it
      is not what this tool uses. Request Indexing goes through the browser
      extension precisely because Google exposes no public API for it.

- [ ] **3. Configure the consent screen.** Google Auth Platform → Branding.
      App name, user support email, developer contact email.

      The app name must not imply Google built or endorses it. "GSC MCP"
      is fine; "Google Search Console MCP" invites a rejection at
      verification. Skip the logo for now — uploading one adds a separate
      brand review to the timeline.

- [ ] **4. Audience: External.** Leave publishing status on **Testing**,
      and add your own Google account under Test users.

- [ ] **5. Add the scope.** Data access → Add scopes →
      `https://www.googleapis.com/auth/webmasters`

      This is the single scope the server requests (`gauth.py:34`). Full
      `webmasters`, not `webmasters.readonly`, because sitemap submission
      writes. It is classed **sensitive** — which matters in Phase B.

- [ ] **6. Create the client.** Clients → Create client → **Desktop app**.
      Copy the client ID and client secret.

      Desktop app is the right type: the server runs on the user's own
      machine and receives the redirect on a loopback address. The flow
      already uses PKCE with S256 (`gauth.py:52-56`), which is what
      actually secures an installed-app client.

- [ ] **7. Point the server at it.** Environment variables, which always
      win over the embedded constants:

      ```
      GSC_MCP_CLIENT_ID=<the client id>
      GSC_MCP_CLIENT_SECRET=<the client secret>
      ```

- [ ] **8. Run the manual smoke test.** `docs/manual-smoke.md`. It spends
      real quota slots — roughly eleven per property per rolling day, and
      they do not come back.

### The seven-day trap

While publishing status is **Testing**, Google expires refresh tokens
after **seven days**. Sign-in will appear to work and then break a week
later, in a way that looks like a bug in this code and is not. Phase B is
what removes it. If you hit it before then, sign in again.

---

## Phase B — verification, so other people can use it

Required before the app leaves the 100-user cap that applies to every
unverified app. Budget 2–6 weeks, most of it waiting on Google's replies.

**The good news:** `webmasters` is a *sensitive* scope, not a *restricted*
one. Restricted scopes (Gmail, Drive) require a paid third-party CASA
security assessment. Sensitive scopes do not. You avoid the expensive
part.

**The long poles are the prerequisites, not the review.** Google will not
start until all of these exist, so start them first:

- [ ] **A homepage** on a domain you own, publicly reachable, that
      describes what the app does. Not a GitHub repo page.
- [ ] **A privacy policy** at a URL on that *same* domain, linked from the
      homepage. It must say what Google user data the app touches, why,
      and that you comply with the Google API Services User Data Policy
      including the Limited Use requirements.
- [ ] **Domain ownership verified in Search Console** under the same Google
      account that owns the Cloud project. Fitting, given what this tool
      does.
- [ ] **A demo video** on YouTube (unlisted is fine) showing the OAuth
      consent screen — with the client ID visible in the address bar — the
      grant, and what the app then does with the data.
- [ ] **A scope justification**: one paragraph on why the app cannot work
      without full `webmasters`. Say that it submits sitemaps and reads
      URL inspection results, and that the read-only variant cannot submit.

Then: Google Auth Platform → Publishing status → **Publish app** →
Prepare for verification → submit. Expect a round or two of questions.

---

## The decision this forces

`EMBEDDED_CLIENT_ID` and `EMBEDDED_CLIENT_SECRET` are empty strings
(`deps.py:37-38`) and a real secret must never be committed to a public
repo. Decision D1 says the product ships its own client so a user just
clicks Allow. Those two facts collide, and the collision is real:

A Desktop-app client secret is **not** confidential — Google's own docs
acknowledge that an installed app cannot keep one, which is why PKCE
exists. So embedding it is not a security failure in the usual sense.
What it does mean on a *public* repo is that the value is in git history
permanently, and anyone can stand up a consent screen carrying your app's
name and your verification.

Three ways to go, and this is your call:

1. **Embed both.** What D1 assumes. Standard practice for installed apps.
   Cost: your published app's identity is reusable by anyone, and every
   user counts against your app's standing with Google.
2. **Distribute the client ID, keep the secret out of git**, injected at
   package-build time. Reduces the exposure without changing the user's
   experience. Verify in the console whether your client type will accept
   a token exchange without the secret — that behaviour has changed before
   and I would not build on my recollection of it.
3. **Leave both empty** — every user creates their own Cloud client. No
   verification, no user cap, no shared identity. Cost: real setup
   friction, which is exactly what D1 wanted to remove.

Phase A works identically under all three, so nothing here blocks starting.
