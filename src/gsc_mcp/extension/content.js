// GSC Indexer Bridge — content script. Runs ONLY on search.google.com.
// Executes: inspect URL -> (already indexed?) -> Test Live URL ->
// Request Indexing -> outcome. Mirrors indexer_core.py signal matching.

const QUOTA_SIGNALS = [
  "exceeded your daily quota", "reached your daily quota",
  "exceeded the daily quota", "reached your daily limit for this tool",
  "daily quota for this tool", "try again tomorrow",
];
const TRANSIENT_SIGNALS = [
  "we had a problem", "try again later", "something went wrong",
  "an error occurred", "couldn't complete", "could not complete",
  "an unexpected error",
];
const SUBMIT_RETRIES = 3;

// Set by a "Stop" from the popup; the job's wait loops bail out when true.
let aborted = false;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const rand = (lo, hi) => lo + Math.random() * (hi - lo);
const pageText = () => (document.body ? document.body.innerText : "").toLowerCase();
const hasAny = (signals) => { const t = pageText(); return signals.some((s) => t.includes(s)); };

// Set an input's value so a JS framework (React/Angular) actually sees the
// change. A raw `el.value = x` bypasses React's value tracker and is silently
// reverted; calling the native prototype setter then firing a bubbling `input`
// event makes the framework commit the value. execCommand is tried first as it
// most faithfully mimics real typing (what Playwright's fill() achieves via CDP).
function setNativeValue(el, value) {
  el.focus();
  try { el.select(); } catch {}
  let ok = false;
  try { ok = document.execCommand("insertText", false, value); } catch {}
  if (!ok || el.value !== value) {
    const proto = Object.getPrototypeOf(el);
    const desc = Object.getOwnPropertyDescriptor(proto, "value");
    if (desc && desc.set) desc.set.call(el, value);
    else el.value = value;
    el.dispatchEvent(new Event("input", { bubbles: true }));
  }
  el.dispatchEvent(new Event("change", { bubbles: true }));
}

function report(id, stage) { chrome.runtime.sendMessage({ type: "progress", id, stage }); }
function finish(id, outcome, detail) { chrome.runtime.sendMessage({ type: "result", id, outcome, detail }); }

// Record how the most recent click was performed so the popup can show it
// live: "trusted" (real CDP input) vs "synthetic" (isTrusted:false fallback —
// throttle risk). The popup reads `lastClick` and reflects it dynamically.
function recordClick(mode) {
  try { chrome.storage.local.set({ lastClick: { mode, at: Date.now() } }); } catch {}
}

// Round-trip a message to the service worker and await its reply.
function bgRequest(msg) {
  return new Promise((resolve) => {
    try {
      chrome.runtime.sendMessage(msg, (resp) => {
        if (chrome.runtime.lastError) {
          resolve({ ok: false, error: chrome.runtime.lastError.message }); return;
        }
        resolve(resp || { ok: false });
      });
    } catch (e) { resolve({ ok: false, error: String((e && e.message) || e) }); }
  });
}

// Click `el` as a HUMAN would: ask the service worker to dispatch a real
// trusted CDP click (isTrusted:true + cursor path) via chrome.debugger. A
// content script's own el.click() is isTrusted:false with no pointer movement —
// exactly the signal GSC's bot-guard uses to soft-throttle automation ("try
// again later"), even on the real logged-in profile. Falls back to a synthetic
// click if the debugger can't attach (e.g. DevTools open) so the run still
// completes. Used for the two gesture-gated buttons (Test Live URL, Request
// Indexing); incidental popup-dismiss clicks stay synthetic.
async function trustedClick(el) {
  try {
    el.scrollIntoView({ block: "center", inline: "center" });
    await sleep(rand(500, 1400));   // random human-like pause before each click
    const r = el.getBoundingClientRect();
    if (r.width > 0 && r.height > 0) {
      const x = r.left + r.width / 2;
      const y = r.top + r.height / 2;
      const resp = await bgRequest({ type: "trusted_click", x, y });
      if (resp && resp.ok) { console.log("[gsc-bridge] trusted click OK"); recordClick("trusted"); return true; }
      console.warn("[gsc-bridge] trusted click unavailable ("
        + ((resp && resp.error) || "?") + ") — synthetic fallback");
    }
  } catch (e) {
    console.warn("[gsc-bridge] trusted click error — synthetic fallback:", e);
  }
  recordClick("synthetic");
  el.click();   // synthetic fallback (isTrusted:false)
  return false;
}

// Trusted typing/keys via the worker (CDP). Return true if the trusted path ran.
async function trustedType(text) {
  const r = await bgRequest({ type: "trusted_type", text });
  return !!(r && r.ok);
}
async function trustedKey(key, modifiers) {
  const r = await bgRequest({ type: "trusted_key", key, modifiers: modifiers || 0 });
  return !!(r && r.ok);
}

// find a clickable by its visible text — GSC renders buttons as <button> and
// <div role="button">
function findClickable(texts) {
  const lows = texts.map((t) => t.toLowerCase());
  const nodes = document.querySelectorAll("button, [role='button']");
  for (const n of nodes) {
    const t = (n.innerText || "").trim().toLowerCase();
    if (t && lows.some((l) => t === l || t.includes(l))) {
      if (n.offsetParent !== null) return n;   // visible only
    }
  }
  return null;
}

// True ONLY for a real, interactive reCAPTCHA challenge (the image-grid
// "bframe"). GSC embeds an invisible background reCAPTCHA on EVERY page —
// its mere presence is NOT a challenge (matching it made every submission
// look like a captcha). Mirror _captcha_visible() in indexer_core.py.
function captchaVisible() {
  const frames = document.querySelectorAll(
    "iframe[src*='recaptcha'][src*='bframe'], iframe[title*='recaptcha challenge' i]");
  for (const f of frames) {
    const r = f.getBoundingClientRect();
    const st = getComputedStyle(f);
    if (r.width > 0 && r.height > 0
        && st.display !== "none" && st.visibility !== "hidden") return true;
  }
  return pageText().includes("unusual traffic");
}

function accountMismatch(expected) {
  if (!expected || !expected.includes("@")) return false;
  for (const sel of ['a[aria-label*="Google Account"]',
                     'img[aria-label*="Google Account"]',
                     '[aria-label*="Google Account"]']) {
    const el = document.querySelector(sel);
    if (!el) continue;
    const m = (el.getAttribute("aria-label") || "").match(/[\w.+-]+@[\w.-]+\.\w+/);
    if (m) return m[0].toLowerCase() !== expected.trim().toLowerCase();
  }
  return false;   // positive-mismatch only — unreadable chip never blocks
}

async function waitFor(pred, timeoutMs, intervalMs = 1000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (aborted) return null;
    const v = pred();
    if (v) return v;
    await sleep(intervalMs);
  }
  return null;
}

// pause-on-captcha: poll up to 10 minutes for the user to solve it manually
async function captchaPause(id) {
  report(id, "captcha_visible — solve it in the browser");
  const cleared = await waitFor(() => !captchaVisible(), 10 * 60 * 1000, 3000);
  return cleared !== null;
}

const VERDICT_SIGNALS = ["url is on google", "url is not on google",
                         "url is unknown to google"];

function urlPath(u) {
  try { return new URL(u).pathname.toLowerCase(); } catch { return ""; }
}

async function inspect(id, url) {
  report(id, "inspecting");
  // Dismiss any lingering result / "Indexing requested" popup so the inspect
  // bar is interactable, then type the next URL right here — no dashboard
  // round-trip between URLs (human-style in-place reuse).
  const popup = findClickable(["got it", "dismiss", "close"]);
  if (popup) { popup.click(); await sleep(rand(600, 1200)); }

  const bar = await waitFor(() => {
    for (const sel of ["input[aria-label*='nspect']", "input[placeholder*='nspect']",
                       "input[aria-label*='URL']", "input[type='url']",
                       "form[role='search'] input", "input[jsname]"]) {
      const el = document.querySelector(sel);
      if (el && el.offsetParent !== null) return el;
    }
    return null;
  }, 30000);
  if (!bar) return "error";
  // Human-like, TRUSTED entry: move the cursor to the inspect bar and click it
  // (focus), select any existing text (Ctrl+A), paste the URL via trusted
  // insertText, then press Enter. Falls back to synthetic value-set + synthetic
  // Enter if the debugger isn't attached, so the run still works.
  const focused = await trustedClick(bar);
  let typed = false;
  if (focused) {
    await trustedKey("a", 2);                 // Ctrl+A — select any existing text
    await sleep(rand(120, 280));
    typed = await trustedType(url);           // paste-like trusted input
  }
  if (!typed) setNativeValue(bar, url);       // fallback
  await sleep(rand(400, 900));
  const sentEnter = typed && await trustedKey("Enter");
  if (!sentEnter) {
    for (const type of ["keydown", "keypress", "keyup"]) {
      bar.dispatchEvent(new KeyboardEvent(type, { key: "Enter", code: "Enter",
        keyCode: 13, which: 13, bubbles: true, cancelable: true }));
    }
  }
  // Wait for the NEW verdict. Because we reuse the page in place, the PREVIOUS
  // URL's verdict may still be in the DOM — so require the freshly-inspected
  // URL (full or its path) to be on the page TOGETHER with a verdict phrase
  // before trusting it. Input values aren't part of innerText, so the bar's
  // typed text can't produce a false match. The result header renders the URL.
  const needle = url.toLowerCase();
  const path = urlPath(url);
  const verdict = await waitFor(() => {
    const t = pageText();
    const hasVerdict = VERDICT_SIGNALS.some((s) => t.includes(s));
    const showsThisUrl = t.includes(needle) || (path && t.includes(path));
    if (hasVerdict && showsThisUrl) return t;
    if (captchaVisible()) return "captcha";
    return null;
  }, 90000, 1500);
  if (verdict === "captcha") return "captcha";
  if (!verdict) return "error";
  return "ok";
}

async function awaitLiveTest(id) {
  await sleep(1500);
  let sawModal = false;
  const deadline = Date.now() + 150000;
  while (Date.now() < deadline) {
    if (aborted) return "aborted";
    if (hasAny(QUOTA_SIGNALS)) return "quota";
    if (captchaVisible()) {
      const solved = await captchaPause(id);
      if (!solved) return "captcha";
      continue;
    }
    const t = pageText();
    if (t.includes("testing live url") || t.includes("running test")) {
      sawModal = true; await sleep(1000); continue;
    }
    if (t.includes("url is not available to google")
        || t.includes("page cannot be indexed")
        || t.includes("url is unavailable to google")) return "cannot_index";
    if (t.includes("url is available to google")
        || findClickable(["request indexing"])) {
      await sleep(2000);
      if (findClickable(["request indexing"])) return "ready";
    }
    if (sawModal && hasAny(TRANSIENT_SIGNALS)) return "rate_limited";
    await sleep(1000);
  }
  return "timeout";
}

async function awaitSubmission(id) {
  const deadline = Date.now() + 180000;
  while (Date.now() < deadline) {
    if (aborted) return "aborted";
    const t = pageText();
    if (t.includes("indexing requested")) return "submitted";
    if (hasAny(QUOTA_SIGNALS)) return "quota_exceeded";
    if (hasAny(TRANSIENT_SIGNALS)) return "rate_limited";   // server rate throttle — do NOT retry
    if (t.includes("url is not available to google")
        || t.includes("page cannot be indexed")
        || t.includes("url is unavailable to google")) return "error";
    if (captchaVisible()) return "captcha_after_click";
    await sleep(1000);
  }
  return "timeout";
}

// ---------------------------------------------------------------------------
// RPC path (see rpc-main.js). Talks to the MAIN-world module over postMessage.
// Modes, stored in chrome.storage.local as `rpcMode`. ALL of them submit by
// clicking — these only control observation, never the submission itself:
//   "off"   (default) — click path only, nothing recorded
//   "learn" — click path plus the recorder, to study GSC's rpc traffic
//   "probe" — inspect and record, then STOP before the click (costs no quota)
// There is no replay mode; see RPC-HANDOFF.md §4d for why it was removed.
// ---------------------------------------------------------------------------
const RPC_FLAG = "__gscrpc__";

function rpcDown(type, payload) {
  window.postMessage({ [RPC_FLAG]: true, dir: "down", type, ...payload }, "*");
}

// Wait for one specific message coming up from the MAIN world.
function rpcUp(type, timeoutMs, match) {
  return new Promise((resolve) => {
    const timer = setTimeout(() => { window.removeEventListener("message", h); resolve(null); }, timeoutMs);
    function h(ev) {
      if (ev.source !== window) return;
      const d = ev.data;
      if (!d || d[RPC_FLAG] !== true || d.dir !== "up" || d.type !== type) return;
      if (match && !match(d)) return;
      clearTimeout(timer); window.removeEventListener("message", h); resolve(d);
    }
    window.addEventListener("message", h);
  });
}

// Persist a recipe the moment the recorder captures one. Runs for the life of
// the content script, so a learn-mode job stores its recipe without any extra
// round trip.
// Learning result. NOT auto-enabled: on 2026-07-25 auto-flipping to "on" put a
// wrong recipe straight into production and it burned a slot reporting success
// for a request that queued nothing. A recipe now has to be reviewed in Options
// and switched on deliberately.
window.addEventListener("message", (ev) => {
  if (ev.source !== window) return;
  const d = ev.data;
  if (!d || d[RPC_FLAG] !== true || d.dir !== "up" || d.type !== "harvested") return;
  if (d.recipe) {
    chrome.storage.local.set({ rpcRecipe: d.recipe, rpcCaptures: d.captures });
    console.log("[gsc-rpc] candidate submission request:", d.recipe.rpcids,
                d.recipe.endpoint, "— review in Options, then set mode to 'on'");
  } else {
    chrome.storage.local.set({ rpcCaptures: d.captures });
    console.log("[gsc-rpc] no submission request identified;", d.captures.length,
                "candidate(s) recorded:", d.captures);
  }
});

// Keep the last raw RPC response so it can be read from the service-worker
// console after the tab has closed. Survives the run; overwritten each time.
window.addEventListener("message", (ev) => {
  if (ev.source !== window) return;
  const d = ev.data;
  if (!d || d[RPC_FLAG] !== true || d.dir !== "up" || d.type !== "response") return;
  chrome.storage.local.set({
    rpcLastResponse: { at: new Date().toISOString(), status: d.status, ok: d.ok,
                       verdict: d.verdict, url: d.url, body: d.body },
  });
});

const rpcConfig = () =>
  new Promise((r) => chrome.storage.local.get(["rpcMode", "rpcRecipe"], (v) => r(v || {})));

// Ask the page world for everything it recorded and let it pick the submission.
// Waits briefly: the submission request is in flight around the same moment the
// UI confirms, so harvesting the instant the toast appears can miss it.
async function rpcHarvest() {
  await new Promise((r) => setTimeout(r, 1500));
  rpcDown("harvest", {});
  return rpcUp("harvested", 5000);
}

async function runJob(id, url, authuser) {
  try {
    if (aborted) return finish(id, "skipped");
    if (accountMismatch(authuser)) return finish(id, "account_mismatch");
    if (hasAny(QUOTA_SIGNALS)) return finish(id, "quota_exceeded");

    // Arm BEFORE the inspection. Arming after it (as 1.6.0/1.6.1 did) left the
    // pre-click window about a second wide and empty, so the inspection's own
    // rpc was never baselined and anything post-click was accepted by default.
    // The inspection is exactly what has to be excluded, so it must be seen.
    const rpcPre = await rpcConfig();
    const probing = rpcPre.rpcMode === "probe";
    const learning = probing || (rpcPre.rpcMode === "learn" && !rpcPre.rpcRecipe);
    if (learning) {
      rpcDown("arm", { url });
      await rpcUp("armed", 3000);
      report(id, "rpc recorder armed (pre-inspection)");
    }

    const ins = await inspect(id, url);
    if (ins === "captcha") {
      const solved = await captchaPause(id);
      if (!solved) return finish(id, "captcha");
    } else if (ins !== "ok") {
      return finish(id, "error", "inspection failed");
    }

    // already indexed -> skip without submitting. Only the verdict variant
    // "URL is on Google, but has issues" justifies a (re)submit — benign
    // mentions like "Non-critical issues detected" in the Enhancements
    // section must NOT block the skip (live bug: indexed pages were
    // re-submitted and burned slots).
    const t = pageText();
    if (t.includes("url is on google") && !t.includes("but has issues"))
      return finish(id, "already_indexed");

    // PROBE: inspect, record, and stop before the click. Costs no quota — only
    // the submission does — so the inspection response can be studied for the
    // tokens Fj3Owf needs without spending slots. Finishes "skipped" because
    // indexer_core consumes a slot on exactly "submitted" and nothing else.
    if (probing) {
      const h = await rpcHarvest();
      for (const r of (h && h.responses) || []) {
        report(id, `probe response rpc=${r.rpcids || "?"} phase=${r.phase} `
          + `len=${r.len} tokens=${(r.tokens || []).map((x) => x.slice(0, 24) + "…").join(" ")}`);
      }
      return finish(id, "skipped", "probe only — no submission");
    }

    // THERE IS NO REPLAY PATH. Removed 2026-07-25 after it was proven
    // unbuildable: the submission rpc (Fj3Owf) takes a ~1,100-char argument the
    // page serializes client-side, which appears in no response and therefore
    // cannot be learned by watching traffic. See RPC-HANDOFF.md §4d.
    //
    // The click below is the only submission mechanism. `learn` and `probe`
    // remain as read-only diagnostics; neither changes how a URL is submitted.

    // Change B: SKIP the heavy "Test Live URL" — request indexing DIRECTLY off
    // the cached inspection (lighter load = less rate-throttling; Request
    // Indexing runs its own quick test internally). Only fall back to an
    // explicit live test if the Request Indexing control isn't present.
    let btn = findClickable(["request indexing"]);
    if (!btn) {
      const live = findClickable(["test live url"]);
      if (live) {
        report(id, "live_test (fallback)");
        await trustedClick(live);
        const out = await awaitLiveTest(id);
        if (out === "aborted") return finish(id, "skipped");
        if (out === "rate_limited") return finish(id, "rate_limited");
        if (out === "quota") return finish(id, "quota_exceeded");
        if (out === "captcha") return finish(id, "captcha");
        if (out === "cannot_index") return finish(id, "error", "cannot be indexed");
      }
      btn = findClickable(["request indexing"]);
    }
    if (!btn) return finish(id, "error", "no Request Indexing control");

    // brief human-like pause, then ONE trusted click — no retries (retrying a
    // throttle only burns the rate/profile budget further).
    await sleep(rand(800, 1800));
    report(id, "clicking request indexing");
    // Everything the recorder sees from here on is a submission candidate.
    // Anything before it — including GSC's own inspection of this same URL —
    // is not, which is the distinction the first recipe failed to make.
    rpcDown("phase", { phase: "post-click" });
    await trustedClick(btn);
    const res = await awaitSubmission(id);
    if (res === "aborted") return finish(id, "skipped");
    if (res === "rate_limited") return finish(id, "rate_limited");
    if (res === "captcha_after_click") {
      const solved = await captchaPause(id);
      // outcome unknown even if solved — conservative vocabulary so python
      // consumes a slot exactly like the playwright path
      return finish(id, "captcha_after_click");
    }
    // Only a real submission teaches anything; a throttled or errored click
    // would record the wrong traffic. Gated on `learning` too: with the recorder
    // disarmed this harvested nothing, logged a misleading "learned NOTHING"
    // line, and cost 1.5s per URL — visible across the 2026-07-25 batch run.
    if (learning && res === "submitted") {
      const h = await rpcHarvest();
      // Report it up the bridge so the learn outcome lands in the run log.
      // Otherwise the only copy is in this browser's extension storage, which
      // means a slot gets spent and nobody finds out what it bought.
      if (h) {
        // The rpc unique to the post-click window is the submission, whether or
        // not it carries the URL. Report it and its payload: a submission that
        // names its target some other way is invisible to the URL-matching
        // recorder, so this line is the only place its shape shows up.
        if (h.identified) {
          report(id, `rpc submission looks like ${h.identified.rpcids} `
            + `(hasUrl=${!!h.identified.hasUrl}) body=${(h.identified.body || "").slice(0, 420)}`);
        }
        // The whole question in one line: are the submission's arguments
        // obtainable before the click, or minted by it?
        for (const t of h.trace || []) {
          report(id, `arg ${t.arg} (${t.len}c) -> `
            + (t.foundIn ? `found in ${t.foundIn}` : "NOT FOUND in any earlier response"));
        }
        report(id, h.recipe
          ? `rpc learned ${h.recipe.rpcids}`
          : `rpc learned NOTHING replayable (submission carries no URL)`);
      }
    }
    finish(id, res);
  } catch (e) {
    finish(id, "error", String(e && e.message || e));
  }
}

chrome.runtime.onMessage.addListener((m, _sender, sendResponse) => {
  if (m.type === "job") {
    aborted = false;                    // fresh job — clear any prior abort
    sendResponse({ accepted: true });   // ack immediately; result comes async
    runJob(m.id, m.url, m.authuser);
  }
  if (m.type === "abort") { aborted = true; }   // popup "Stop" — bail out of waits
  return false;
});

// Keep the MV3 service worker alive for the whole run. Jobs have long waits
// (≤90s for a verdict, plus 30–90s pacing between URLs) during which Chrome
// would otherwise terminate the idle worker mid-job and drop its WS to the
// Python bridge. An open Port keeps the worker alive; we also ping it and
// re-open if the worker is ever recycled (reviving it). The content script
// persists across in-place jobs, so this port spans the entire batch.
let keepAlivePort = null;
function openKeepAlive() {
  try {
    keepAlivePort = chrome.runtime.connect({ name: "keepalive" });
    keepAlivePort.onDisconnect.addListener(() => {
      keepAlivePort = null;
      setTimeout(openKeepAlive, 1000);   // worker recycled — revive it
    });
  } catch {
    setTimeout(openKeepAlive, 2000);
  }
}
openKeepAlive();
setInterval(() => {
  try { if (keepAlivePort) keepAlivePort.postMessage({ t: "ping" }); }
  catch { /* port died; onDisconnect will reopen */ }
}, 20000);
