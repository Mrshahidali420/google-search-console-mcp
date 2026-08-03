# Manual smoke checklist

**Run this before every release.** It is the only test of the live path.

The automated suite proves that the code does the right thing when handed
fabricated browsers, fabricated preferences files and a fake Google. It
cannot prove that a real person can sign in, because every OAuth path in it
is exercised against fakes and no real Google account has ever
authenticated against this code. This checklist is what stands in for that,
and until someone has run it, "you can sign in with gsc-mcp" is a claim the
repository cannot support.

It spends **no indexing quota.** Nothing in Milestone 3A submits anything —
the most expensive call in here is `sites.list`. The quota-spending smoke
test, covering `gsc_request_indexing` and the job tools, arrives with
Milestone 3B and will need its own checklist and its own throwaway
property.

Budget 20-30 minutes, most of it waiting for a browser.

---

## Before you start

You need:

- A Google account that owns at least one Search Console property. Two is
  better — the `properties` check reports a count, and a count of 1 is
  indistinguishable from an off-by-one.
- Your own Google Cloud OAuth client (Desktop app type) with the **Search
  Console API** enabled. See the README's install section.
- A Chromium browser: Chrome, Brave, Edge, Vivaldi, Opera or Chromium.
- A terminal in the repo, with the project's virtualenv active.

Record, as you go: your OS and version, browser and version, Python
version, and the commit you tested. A pass on one platform is not a pass on
the others.

> ### If you are on macOS or Linux, read this
>
> Browser and profile detection on macOS and Linux **has only ever run
> against fixtures** — synthetic directory trees built by the test suite.
> It has never run against a real browser install, on real hardware,
> anywhere, including CI. Windows is the only platform where anyone has
> watched it work.
>
> That means the first person to run this checklist on a Mac or a Linux box
> is not confirming a known-good path; they are performing the test. Expect
> steps 5 and 6 to be where it breaks — an app bundle in an unusual
> location, a Flatpak or Snap install with its own data directory, a
> `.desktop` file whose `Exec=` line has flags in it. If detection finds
> nothing or finds the wrong thing, that is a **finding, not a
> misconfiguration on your part**. Write down the exact install location
> and how the browser was installed (App Store, direct download, Homebrew,
> apt, Flatpak, Snap) and file it. That report is worth more than a pass.

---

## 1. Start from a clean home

Every step below must run against a config directory that has never been
used, so that a token, a database or an extracted extension left over from
development cannot make a broken step look like a working one.

```bash
# POSIX
export GSC_MCP_HOME="$(mktemp -d)/gsc-mcp-smoke"
```

```powershell
# Windows PowerShell
$env:GSC_MCP_HOME = Join-Path $env:TEMP "gsc-mcp-smoke-$(Get-Random)"
```

Set your OAuth client in the same shell:

```bash
export GSC_MCP_CLIENT_ID="your-client-id.apps.googleusercontent.com"
export GSC_MCP_CLIENT_SECRET="your-client-secret"
```

- [ ] `GSC_MCP_HOME` points at a directory that does not exist yet, or is
      empty.
- [ ] Both client variables are set in **this** shell.

## 2. Connect an MCP client

Launch the server from the same shell, through whatever MCP client you are
testing with, so it inherits the three variables above. If your client
launches the server itself from a config file, put the variables in that
config's `env` block instead and confirm the client was restarted
afterwards.

- [ ] The client lists eight `gsc_*` tools.

## 3. First `gsc_setup()` — expect a consent URL

Call `gsc_setup()`.

- [ ] `done` is `["oauth_client"]`.
- [ ] `next.step` is `"consent"`.
- [ ] `next.url` starts with `https://accounts.google.com/`.
- [ ] A browser tab opened at that URL by itself. (If you are on a headless
      machine it will not have; use the URL from the response.)
- [ ] **The response contains no token, no authorization code, and no
      field named `state` or `verifier`.** Read the raw JSON for this one,
      not your client's rendering of it.

## 4. Approve the consent screen, then call `gsc_setup()` again

Sign in as the account that owns your properties and approve the scope.
Google will warn you that the app is unverified — expected, verification
has not started; choose the advanced option and continue.

Then call `gsc_setup()` a second time.

- [ ] `done` now contains `"consent"`.
- [ ] A token file now exists under `GSC_MCP_HOME`.
- [ ] On POSIX, that file's mode is `600`.

If the second call still returns `step: "consent"`, call it once more
before treating it as a failure — the receiver may not have been reached
yet. If it returns `"the sign-in did not complete"`, that is a real
finding: capture the log file and file it.

## 5. Confirm the recommended profile is the right one

Call `gsc_detect_browsers()`.

- [ ] Every Chromium browser you actually have installed appears.
- [ ] Every profile of each one appears.
- [ ] `recommended` names a real profile.
- [ ] **No email address appears anywhere in the response.** Search the raw
      JSON for `@`.
- [ ] No filesystem path appears anywhere in the response.

Now the part that matters, and the part no unit test can do: **open that
recommended profile in that browser and confirm it is signed in as the
account you just authorised.** Check the avatar in the top right.

- [ ] The recommended profile really is signed in as that account.

If it is not, that is the finding this step exists for. Note what
`signed_in`, `account_discoverable` and `matches_authorised_account` said
for it, and file all three.

Two things you must **not** treat as failures:

- `matches_authorised_account` is `null` for every profile. That is
  expected on every machine today: the flag reads an account address from
  the stored token, and nothing writes one, because the current scope set
  returns no identity claim. It is inert plumbing, not a bug you found.
  Do not report a `null` here.
- On **Microsoft Edge**, `signed_in: true` is not evidence of a Google
  sign-in. Edge stores Microsoft account addresses in the same file and the
  same key Chrome uses for Google ones, so an Edge profile may show as
  signed in when it is signed in to Microsoft — and if your Microsoft and
  Google addresses are the same, an apparent match is not a match. Verify
  by eye in the browser, as above, and trust that over the flag.

## 6. Load the unpacked extension

Call `gsc_setup()` again. It should now report `step: "extension"` with a
`path`.

- [ ] `next.path` names a directory that exists and contains
      `manifest.json`.

In the recommended browser and profile:

1. Open the extensions page. `gsc_setup()`'s action string gives you the
   exact URL for your browser — use that rather than guessing, because not
   every Chromium fork uses its own scheme.
2. Turn on **Developer mode**.
3. Choose **Load unpacked** and select the folder from `next.path`.

- [ ] The extension appears in the list, named "GSC Indexer Bridge".

> **You will now see a warning banner. It is expected and it is correct.**
> Chromium will show a bar saying an extension is debugging your browser,
> and may separately warn you about running extensions in Developer mode.
> The extension requests the `debugger` permission because Search Console
> soft-throttles Request Indexing clicks that did not come from a real
> pointer; synthetic DOM clicks trip that throttle, so the extension issues
> trusted input events through the Chrome DevTools Protocol instead, and
> `debugger` is the only permission that grants those. The banner is
> Chromium accurately reporting a real capability. Do not dismiss the
> browser, and do not file it as a security finding — but **do** file it if
> the banner names any host other than `search.google.com`.

- [ ] The banner, if it names a host, names only `search.google.com`.

## 7. `gsc_doctor()` — seven green checks

Call `gsc_doctor()`.

- [ ] `ok` is `true`.
- [ ] There are exactly seven checks, named in this order: `oauth_client`,
      `token`, `config`, `store`, `properties`, `browser`, `extension`.
- [ ] `properties` reports the number of properties you actually have.
- [ ] `browser` names the profile you loaded the extension into.
- [ ] `extension` reports it as installed, at the version in the packaged
      `manifest.json`.
- [ ] **No email address appears in any `detail` or `fix`.** Search the raw
      JSON for `@`.

Note that a green `extension` check means the extension is **registered**,
not that it is working. Its detail says so. Whether the MV3 service worker
is alive is not checked in this milestone.

### Then break it deliberately, twice

A checklist that only ever sees the happy path does not test the
diagnostics, and the diagnostics are most of what shipped.

**7a. Remove the extension.** Delete it from the extensions page and call
`gsc_doctor()` again.

- [ ] The `extension` check is now `ok: false`.
- [ ] Its `detail` says it is **not installed**.
- [ ] Its `fix` names your browser's extensions page and tells you to use
      Developer mode and Load unpacked.
- [ ] Its `fix` points you at `gsc_setup()` for the folder to select rather
      than spelling out a path. **No absolute path appears anywhere in the
      doctor's output** — a doctor result is retained by your MCP client,
      and on Windows a path under the config directory contains your
      account name. If you see one, that is a finding.

Load it again and confirm the check goes green.

**7b. Force a version mismatch.** This reproduces the one situation the
mismatch state exists for: you upgraded gsc-mcp, the extracted copy moved
on, and the browser is still running the build it loaded last week.

> **Edit the *packaged* manifest, not the extracted one.** Editing the
> extracted copy does nothing: `extension_dir()` compares it against the
> packaged version on every call, sees they differ, and silently
> re-extracts *before* the check compares anything. The check comes back
> green and you learn nothing. This is the same fact that makes the state
> detectable at all — what the browser recorded at load time is the only
> number that can go stale.

Find the packaged manifest:

```sh
python -c "import gsc_mcp, pathlib; print(pathlib.Path(gsc_mcp.__file__).parent / 'extension' / 'manifest.json')"
```

Note its current `version` so you can put it back. Change it to something
obviously different, e.g. `"99.0.0"`. Do **not** reload the extension in
the browser. Call `gsc_doctor()`.

- [ ] The `extension` check is `ok: false`.
- [ ] Its `detail` names **both** numbers — the one the browser loaded and
      the one now on disk.
- [ ] Its `fix` tells you to click **Reload**, and does *not* tell you to
      Load unpacked again.

Then click Reload in the browser and confirm the check goes green.

**Put the packaged manifest back** when you are done. If you are running
from a checkout, `git checkout -- src/gsc_mcp/extension/manifest.json` is
the reliable way — restoring the version field by hand can still leave the
file reformatted. `git diff` should be empty before you report. Re-running
step 1 with a fresh `GSC_MCP_HOME` will *not* undo this edit; only
restoring the file will.

For reference, this sequence has been walked end to end without a browser,
with the Preferences entry written by hand. The extracted version tracks
the packaged one to `99.0.0` while the recorded one stays behind, and the
check reports `loaded at version <old>, but the copy on disk is now version
99.0.0` with a Reload fix. If you see anything else, that is a finding.

## 8. `gsc_list_sites()` — real properties

Call `gsc_list_sites()`.

- [ ] Every property you expect is listed, with the right `permission`
      level.
- [ ] `host` is populated for each.
- [ ] Calling it a second time returns the same list.

This is the end of the read-only path. Stop here.

---

## Optional: confirm the ID changes when the directory moves

Worth doing once per release, because the behaviour looks like breakage the
first time anyone meets it in the field.

The extension ships with no manifest `key`, so Chromium derives its ID by
hashing the absolute path it was loaded from. Move the extraction directory
— easiest done by setting a different `GSC_MCP_HOME` and running
`gsc_setup()` again — and the previously loaded copy stops matching.

- [ ] `gsc_doctor()`'s `extension` check now reports not installed.
- [ ] Loading the unpacked extension from the new folder makes it green
      again.

That is re-pairing, not breakage: nothing is corrupted and there is no
stale state to clean up. It matters because a `pip install --upgrade` that
relocates the config directory produces exactly this, and a user who reads
it as a broken install will go looking for a problem that does not exist.

---

## Reporting

Open an issue with:

- OS and version, browser and version, Python version, commit tested.
- Which numbered step failed, and the tool's exact response for it.
- The log file from `GSC_MCP_HOME`. It contains no addresses, tokens or
  paths by design — failures are logged by exception type name only — so it
  is safe to attach. Read it before you attach it anyway; if you find an
  address, a token or a path in there, **that is the most important bug in
  the report.**
