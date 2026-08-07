# Manual smoke checklist

**Run this before every release.** It is the only test of the live path.

The automated suite proves that the code does the right thing when handed
fabricated browsers, fabricated preferences files and a fake Google. It
cannot prove that a real person can sign in, because every OAuth path in it
is exercised against fakes. This checklist is what stands in for that.

**Steps 1-8 have been walked end to end, once, on Windows 11, against a
real Google account with nine properties (2026-08-04).** They found two
real defects, both since fixed — a profile ranking that crashed when no
account was on record, and an extension check that reported a version off
the disk as though it were the one the browser was running. That is the
argument for running this rather than trusting the suite. **Step 9 has
never been run by anyone**, so every claim about submission in this
repository still rests on code review alone.

It comes in two passes. **Steps 1-8 spend no indexing quota** — the most
expensive call in them is `sites.list` — and are the read-only path.
**Step 9 spends real, unrecoverable slots** on a real property and is
optional, gated, and to be run deliberately. Read its own preamble before
starting it.

Budget 20-30 minutes for the read-only pass, most of it waiting for a
browser, and a further hour or so for the submission pass — most of that
spent watching a 130-180 second gap go by.

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

- [ ] The client lists fifteen `gsc_*` tools. (The count is asserted by
      `tests/test_server_smoke.py`; if your client shows a different
      number it is showing you a stale server, not a finding.)

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
Google may warn you that the app is unverified — expected for an app
still in Testing; choose the advanced option and continue. A published
app requesting only the non-sensitive Search Console scopes should not
show it.

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

If it is not, note what `account_on_disk`, `account_discoverable` and
`matches_authorised_account` said for it and file all three — the ranking
picking the wrong profile is worth knowing about. Then **pin the right one
and carry on with the rest of the checklist against it**:

```
gsc_use_browser(browser="brave", profile="Default")
```

using the `browser_key` and `profile` from the entry you actually want.
Every step below follows the pin. `gsc_use_browser(clear=True)` puts it
back. This is not a workaround for a broken step — the detector ranks what
it can see and cannot know which browser you keep the account in, so a
recommendation you disagree with is a supported state, not a failure.

- [ ] After pinning, `gsc_detect_browsers()` reports `pinned` naming your
      choice, and `recommended` is that same entry.

Two things you must **not** treat as failures:

- `matches_authorised_account` is `null` for every profile. That is
  expected on every machine today: the flag reads an account address from
  the stored token, and nothing writes one, because the current scope set
  returns no identity claim. It is inert plumbing, not a bug you found.
  Do not report a `null` here.
- On **Microsoft Edge**, `account_on_disk: true` is not evidence of a Google
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

- [ ] The extension appears in the list, named "GSC MCP Bridge".

If you load it into a *different* profile than the selected one — easily
done, since it goes wherever the browser was open at the time — the
`extension` check in step 7 will say it is not installed **and name the
profile that does have it**. That is the expected output, not a
contradiction: either load it into the selected profile as well, or pin
the other one with `gsc_use_browser()`.

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
- [ ] `browser` names the profile you loaded the extension into — and says
      it is pinned, if you pinned it in step 5.
- [ ] `extension` reports it as installed, gives the version of the copy
      **on disk**, and says the version the browser has actually loaded
      cannot be read. All three parts matter; see 7b for why.
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

**7b. Force a version mismatch — and confirm the doctor does NOT claim to
have caught it.** This reproduces the one situation the mismatch state
exists for: you upgraded gsc-mcp, the extracted copy moved on, and the
browser is still running the build it loaded last week.

> ### Read this before you run 7b — the expected result changed
>
> **This step used to say the check goes red and names both versions. It
> does not, it cannot today, and a run that produced that output would
> itself be the finding.**
>
> The comparison needs the version the BROWSER loaded, which the doctor
> reads from Chromium's preferences file. Chromium records a manifest
> snapshot there for extensions it keeps its own copy of — Web Store,
> sideloaded, component — and records **none** for an unpacked extension,
> which it re-reads from your disk at every load. Unpacked is the only way
> this bridge is ever installed. Measured on a real profile: 22 of 22
> packed entries carried a snapshot, the one unpacked entry carried none.
>
> So the mismatch branch cannot fire on any real machine. It is kept
> because it is correct and starts working the day this ships packed, but
> the number it needs has to come from the extension itself over the
> bridge (`chrome.runtime.getManifest().version`), and that is **deferred**
> — it is a bridge protocol change, not a doctor change, and the bridge is
> not yet exercised against a real submission.
>
> What was fixed instead is the lie this step used to hide: the doctor was
> reporting the DISK version inside a sentence claiming the browser had
> loaded it, and returning green. It was wrong in exactly the case the
> mismatch branch was written for. It now names the number it actually has
> and says the other one cannot be read.

Find the packaged manifest:

```sh
python -c "import gsc_mcp, pathlib; print(pathlib.Path(gsc_mcp.__file__).parent / 'extension' / 'manifest.json')"
```

Note its current `version` so you can put it back. Change it to something
obviously different, e.g. `"99.0.0"`. Do **not** reload the extension in
the browser. Call `gsc_doctor()`.

- [ ] The `extension` check is still `ok: true`. The extension IS
      installed; that part of the claim is true and unaffected.
- [ ] Its `detail` gives `99.0.0` as the version **on disk** and says the
      loaded version cannot be read for an unpacked extension.
- [ ] Its `detail` does **not** say "installed at version 99.0.0", or
      anything else asserting the browser is running that build. It is
      not: the browser is still running the old one. If you see that
      wording, the fix in `acabdcc` has regressed — file it.

There is nothing to reload; the check was never going to change colour.

**Put the packaged manifest back** when you are done. If you are running
from a checkout, `git checkout -- src/gsc_mcp/extension/manifest.json` is
the reliable way — restoring the version field by hand can still leave the
file reformatted. `git diff` should be empty before you report. Re-running
step 1 with a fresh `GSC_MCP_HOME` will *not* undo this edit; only
restoring the file will.

For reference, this sequence was run for real on 2026-08-04 against Chrome
with the extension loaded from the extraction directory. The extracted copy
tracked the packaged manifest to `99.0.0` — that half works, and is why the
warning above about editing the right file still stands — while Chrome kept
running `1.10.0` and recorded nothing about either. That run is what turned
this step from a passing test into a bug report.

## 8. `gsc_list_sites()` — real properties

Call `gsc_list_sites()`.

- [ ] Every property you expect is listed, with the right `permission`
      level.
- [ ] `host` is populated for each.
- [ ] Calling it a second time returns the same list.

This is the end of the read-only path. Stop here unless you are running
the submission pass below.

---

## 9. The submission pass — **this spends real quota**

**Read this whole preamble before you run anything in it.**

Every step below sends a real Request Indexing click through a real
browser in a real Search Console session. A spent slot is unrecoverable
and there are only about eleven per property per rolling day. Nothing in
this section can be undone or refunded.

So:

- Use a **throwaway property** you own and do not care about — not a
  client's, not one you are actively working. Its budget will be
  noticeably down for the next 24 hours.
- Run it on a day you are not doing manual submissions on the same
  property. The two share one budget and the ledger cannot see clicks you
  made by hand.
- Expect it to be slow. The gap between submissions is 130-180 seconds and
  is not adjustable downward. A five-URL run is up to fifteen minutes of
  nothing appearing to happen. **That is the tool working.**

- **Load the extension into exactly one browser profile**, and check
  `gsc_doctor()`'s `extension` detail says so before you start. Copies
  loaded from one directory all present the same ID, so the bridge cannot
  tell them apart; only one drives the run, but the one that wins the race
  may not be the window you are watching. This cost two slots for one URL
  before the bridge refused the second connection.

Record `gsc_quota()`'s `spendable_free` for the property before you start
and again at the end; the difference is what this pass cost.

These are the states the automated suite cannot reach. Each is worth more
than the happy path, because each is a place a fake cannot tell you the
truth.

### 9.1 First-ever pairing

Start with no `bridge_token.txt` in `GSC_MCP_HOME`.

- [ ] The extension asks to pair rather than connecting silently.
- [ ] The bridge verifies the request against the profile it resolved.
- [ ] The extension stores the token and reconnects on its own.
- [ ] The token appears nowhere in the log, nowhere in a tool result, and
      nowhere in the transcript. If you can see it in any of those, stop
      and file that first — it is a worse bug than anything else here.

### 9.2 The browser is closed when the run starts

Close the browser entirely, then call `gsc_request_indexing` with one URL.

- [ ] `auto_launch_browser` opens it.
- [ ] The extension connects and the URL is submitted without a second call.

### 9.3 The MV3 service worker is not running when the run starts

Do **not** try to produce this by idling. The earlier instruction — leave
the browser idle five minutes and Chromium tears the worker down — does
not hold, and a run against it tests nothing. The worker has top-level
listeners on `chrome.tabs.onRemoved`, `chrome.debugger.onDetach`,
`chrome.runtime.onConnect` and `onMessage`, plus a 30s keepalive alarm,
so ordinary browser activity restarts it. Measured on Brave: after an
explicit stop it was connected again in 0.3s, 1.8s and 3.0s across three
attempts, and a liveness probe confirmed a real worker behind the socket
each time — not the half-open corpse of §9.2.

Stop it deliberately instead: `brave://serviceworker-internals/`, find the
`chrome-extension://<id>/` scope, click **Stop**, then submit one URL.

- [ ] The extension reconnects and the run proceeds.
- [ ] It does **not** fail with `extension_not_connected`.
- [ ] `gsc.log` shows one submission, not two — a worker that restarts
      mid-flight must not produce a second Request Indexing click.

The wake poke is a fallback here, not the expected path. Expect the
worker to beat the 8s fast wait on its own, so `extension asleep after
8s` will usually be absent from the log; its absence is a pass. The poke
itself is covered automatically by `test_wake_launches_the_extension_page`
and its never-raises sibling in `tests/test_pairing_verify.py` — there is
no way to isolate its effect by hand on a browser that is in use, because
the tab the poke opens is itself an event that would have woken the
worker anyway.

### 9.4 The network drops mid-URL

Start a job over three or four URLs. Pull the network cable — or disable
the adapter — while one URL is in flight, and restore it inside two
minutes.

- [ ] The URL is re-sent rather than abandoned.
- [ ] The batch survives; the remaining URLs still go.
- [ ] `gsc_job_status` shows one outcome per URL, with no duplicates.

### 9.5 Stopping a job by hand

Start a job over several URLs and call `gsc_stop_job` while one is in
flight.

- [ ] The call returns immediately and says it is stopping.
- [ ] The URL in flight still gets a real outcome — not `error`, not a
      missing row. It has already spent its slot and its row has to settle.
- [ ] No further URL is attempted.
- [ ] The job lands in state `stopped_user`.

### 9.6 A restart mid-job

Start a job, then kill the server process while it is running and start it
again.

- [ ] `gsc_job_status` reports the old job as `failed`, not as still
      running — the startup reconcile closed it.
- [ ] A new job can be started; the dead one does not block it.
- [ ] A submission row the crash left open is settled once it is older
      than fifteen minutes — restart again after that and `gsc_quota`
      stops showing a slot stuck in flight. The grace period is
      deliberate: a row opened moments ago may belong to a submission
      another process still has in flight, and closing it would steal it.

### 9.7 A genuine `Quota Exceeded` from Google

**Last, and only last.** It ends with the property at zero, so every step
above has to be finished before you start it — 9.6 in particular cannot
begin a job with no slots to spend. Keep submitting against the same
property until Google refuses one.

- [ ] The run stops at that URL rather than trying the rest.
- [ ] The job lands in state `stopped_throttled`.
- [ ] `stop_reason` names the refusal.
- [ ] `gsc_quota()` shows that property at zero `spendable_free`, and the
      other properties unchanged — the budget is per property.

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
