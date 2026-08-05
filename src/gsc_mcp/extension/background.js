// GSC MCP Bridge — service worker.
// Holds the WS connection to the local tool, owns ONE dedicated GSC tab,
// relays submit jobs to the content script and results back to the bridge.

let ws = null;
let gscTabId = null;
let currentJobId = null;
let lastProperty = null;   // property+account of the last job — lets the next
let lastAuthuser = null;   // job reuse the inspect page in place when unchanged
let attachedTabId = null;  // tab we currently hold a chrome.debugger session on
let clearing = false;      // true while clearing pending jobs — ignore replays

const setState = (bridgeState) => chrome.storage.local.set({ bridgeState });

function pushRecent(url, outcome) {
  chrome.storage.local.get(["recent"], (v) => {
    const recent = [{ url, outcome, at: Date.now() }, ...(v.recent || [])].slice(0, 25);
    chrome.storage.local.set({ recent, currentJob: "" });
  });
}

// ---- WS lifecycle ---------------------------------------------------------
// Auto-connect means the extension does the whole job itself: it fetches its own
// token (pair_request), retries on a backoff, and re-pairs if the token it holds
// is refused. Nothing here needs a human. The tool only listens while a run is
// active, so most attempts fail by design — the point is to already be knocking
// when one starts.
const VERSION = "1.12.1";
const RETRY_MIN_MS = 1000;
const RETRY_MAX_MS = 15000;
let retryMs = RETRY_MIN_MS;
let retryTimer = null;
let connecting = false;
let pairing = false;

function scheduleRetry() {
  if (retryTimer || (ws && ws.readyState !== WebSocket.CLOSED)) return;
  retryTimer = setTimeout(() => { retryTimer = null; connect(); }, retryMs);
  retryMs = Math.min(retryMs * 2, RETRY_MAX_MS);
}

// One place records why we are not connected, so the Options page and the popup
// can say something truer than "disconnected".
function noteAttempt(detail) {
  chrome.storage.local.set({ lastAttempt: { at: Date.now(), detail } });
}

function connect() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING))
    return;
  if (connecting || pairing) return;
  chrome.storage.local.get(["token", "port", "paused"], ({ token, port, paused }) => {
    if (paused) { setState("paused"); return; }   // user hit Stop — stay offline
    port = port || 8765;
    if (!token) { pairSelf(port); return; }       // no token yet — go get one
    connecting = true;
    let sock;
    try { sock = new WebSocket(`ws://127.0.0.1:${port}`); }
    catch (e) {
      connecting = false; noteAttempt(String(e && e.message || e));
      setState("waiting for the tool"); scheduleRetry(); return;
    }
    ws = sock;
    sock.onopen = () => {
      connecting = false;
      sock.send(JSON.stringify({ type: "hello", token, version: VERSION }));
    };
    sock.onmessage = (ev) => { try { onBridgeMessage(JSON.parse(ev.data)); } catch {} };
    sock.onclose = () => {
      connecting = false;
      if (ws === sock) ws = null;
      setState("waiting for the tool");
      detachDebugger();
      scheduleRetry();
    };
    sock.onerror = () => { noteAttempt(`nothing listening on port ${port}`); };
  });
}

// Ask the bridge for the token. It answers only after checking that this
// extension ID is the one the browser profile loaded from the tool's own
// extension directory — so this cannot be used by anything else on the machine
// to talk the extension into working for it.
function pairSelf(port, done) {
  if (pairing) { done && done(false, "already pairing"); return; }
  pairing = true;
  setState("pairing…");
  let settled = false;
  let giveUp = null;
  // `denied` separates "the tool said no" (a real fault worth showing in red)
  // from "nothing is listening yet" (the resting state between runs). Painting
  // both as broken is how a working extension ends up looking broken.
  const finish = (ok, detail, denied) => {
    if (settled) return;
    settled = true;
    pairing = false;
    clearTimeout(giveUp);
    chrome.storage.local.set({ lastPair: { ok, detail, at: Date.now() } });
    noteAttempt(detail);
    done && done(ok, detail);
    if (ok) { retryMs = RETRY_MIN_MS; connect(); return; }
    setState(denied ? "not paired — " + detail : "waiting for the tool");
    scheduleRetry();
  };
  let sock;
  try { sock = new WebSocket(`ws://127.0.0.1:${port}`); }
  catch (e) { finish(false, String(e && e.message || e)); return; }
  giveUp = setTimeout(() => {
    try { sock.close(); } catch {}
    finish(false, `nothing listening on port ${port} — start a job, or run \`gsc_setup\` again`);
  }, 6000);
  sock.onopen = () => sock.send(JSON.stringify(
    { type: "pair_request", extension_id: chrome.runtime.id, version: VERSION }));
  sock.onmessage = (ev) => {
    let m; try { m = JSON.parse(ev.data); } catch { return; }
    if (m.type === "pair_ok" && m.token) {
      chrome.storage.local.set({ token: m.token, port, paused: false },
        () => finish(true, "paired automatically"));
    } else if (m.type === "pair_denied") {
      finish(false, m.reason || "the tool refused to pair", true);
    } else if (m.type === "hello_busy") {
      // Not a refusal to pair — the tool is driving another browser, or is
      // busy with one. Retry on backoff; do not mark this copy unpaired.
      finish(false, m.reason || "the tool is busy with another browser");
    }
  };
  sock.onerror = () => finish(false, `no tool listening on port ${port}`);
  sock.onclose = () => finish(false, `no tool listening on port ${port}`);
}

function onBridgeMessage(msg) {
  if (msg.type === "hello_ok") { retryMs = RETRY_MIN_MS; setState("connected"); return; }
  if (msg.type === "hello_denied") {
    // The token file was regenerated (deleted, or a fresh checkout). Throw ours
    // away and pair again rather than sitting there telling the user to go and
    // copy a file — that state used to require a manual fix every time.
    setState("token stale — re-pairing");
    chrome.storage.local.remove("token", () => { try { ws && ws.close(); } catch {} });
    return;
  }
  if (msg.type === "hello_busy") {
    // A run belongs to ONE browser. Loading this extension into a second one
    // gives both copies the same ID (it is a hash of the load path), so the
    // tool cannot tell them apart at the handshake and refuses whichever
    // arrives second. Keep the token — this is not "you are unpaired" — and
    // let the reconnect backoff try again.
    setState(msg.reason ? "standing by — " + msg.reason : "standing by");
    return;
  }
  if (msg.type === "pong") return;
  // "are you actually running?" — asked by a newcomer socket before it takes
  // the run off us. Only a live worker can reply, which is the point: Brave
  // kills this worker during session restore without closing the socket, and
  // the browser goes on answering protocol pings for the corpse. Answering
  // costs nothing; not answering is how a dead worker lets go promptly.
  if (msg.type === "probe") { send({ type: "probe_ack" }); return; }
  if (msg.type === "submit") { if (clearing) return; runJob(msg); }   // drop replays while clearing
}

function send(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}

// Backstop for an evicted worker. 0.5 min is Chrome's floor, so this alone
// would mean up to 30s of "why isn't it connected" — the tool closes that gap
// from its side by opening connect.html (see pairing.wake). Reset the backoff
// first: a worker that just woke should try immediately, not at 15s.
chrome.alarms.create("keepalive", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(() => {
  retryMs = RETRY_MIN_MS;
  connect();
  send({ type: "ping" });
});
chrome.runtime.onStartup.addListener(connect);
// On install/reload, clear any stale "Working now" left by a hung/aborted run.
chrome.runtime.onInstalled.addListener(() => { chrome.storage.local.set({ currentJob: "" }); connect(); });
chrome.runtime.onMessage.addListener((m) => {
  // Reconnect also RESUMES (clears the paused flag) so one button re-arms it.
  if (m.type === "reconnect" || m.type === "resume") {
    chrome.storage.local.set({ paused: false }, () => {
      try { ws && ws.close(); } catch {} ws = null; connect();
    });
  }
  // Stop: go offline, suppress auto-reconnect, abort the in-flight job, and
  // clear the "Working now" display. Stops the bridge re-sending on reload.
  if (m.type === "pause") {
    chrome.storage.local.set({ paused: true, currentJob: "" });
    try { if (gscTabId !== null) chrome.tabs.sendMessage(gscTabId, { type: "abort" }); } catch {}
    try { ws && ws.close(); } catch {}
    ws = null;
    setState("paused");
  }
  if (m.type === "clear") clearPending();
});

// Options-page "Connect now", and the wake page asking to be tidied away.
// Pairing is re-run rather than skipped when a token already exists: this is
// the button people press when something is wrong, and a stale token is the
// most common thing that is wrong.
chrome.runtime.onMessage.addListener((m, sender, sendResponse) => {
  if (m.type === "pair_now") {
    chrome.storage.local.get(["port"], ({ port }) => {
      chrome.storage.local.set({ paused: false }, () => {
        retryMs = RETRY_MIN_MS;
        pairSelf(Number(m.port) || port || 8765,
                 (ok, detail) => sendResponse({ ok, detail }));
      });
    });
    return true;                                    // async sendResponse
  }
  if (m.type === "close_me") {
    const id = sender.tab && sender.tab.id;
    if (id != null) chrome.tabs.remove(id).catch(() => {});
  }
});

// Clear the bridge's stuck/queued submission so a fresh run can start. Opens a
// short-lived socket, says hello, sends {type:"cancel"} (which makes the Python
// run abort and free port 8765), and IGNORES any job the dying run replays.
function clearPending() {
  clearing = true;
  setState("clearing pending…");
  try { if (gscTabId !== null) chrome.tabs.sendMessage(gscTabId, { type: "abort" }); } catch {}
  chrome.storage.local.get(["token", "port"], ({ token, port }) => {
    if (!token) { clearing = false; setState("no token — open Options"); return; }
    let sock;
    try { sock = new WebSocket(`ws://127.0.0.1:${port || 8765}`); }
    catch { clearing = false; setState("clear failed"); return; }
    const giveUp = setTimeout(() => {
      clearing = false; try { sock.close(); } catch {}
      setState("nothing to clear (no bridge)");
    }, 6000);
    sock.onopen = () => sock.send(JSON.stringify({ type: "hello", token, version: "1.0.0" }));
    sock.onmessage = (ev) => {
      let m; try { m = JSON.parse(ev.data); } catch { return; }
      if (m.type === "hello_ok") {
        sock.send(JSON.stringify({ type: "cancel" }));
        clearTimeout(giveUp);
        setTimeout(() => { try { sock.close(); } catch {} finishClear(); }, 700);
      } else if (m.type === "hello_denied") {
        clearTimeout(giveUp); clearing = false; try { sock.close(); } catch {}
        setState("token rejected — check Options");
      }
      // any "submit" replayed by the dying run is intentionally ignored
    };
    sock.onerror = () => {
      clearTimeout(giveUp); clearing = false;
      setState("nothing to clear (no bridge)");
    };
  });
}

// After clearing, leave the extension READY (not paused) and reconnecting, so
// the next run's fresh bridge can pick it up.
function finishClear() {
  clearing = false;
  chrome.storage.local.set({ paused: false, currentJob: "" }, () => {
    try { ws && ws.close(); } catch {} ws = null;
    setState("cleared — ready");
    connect();
  });
}
connect();

// ---- job execution --------------------------------------------------------
async function ensureGscTab(url) {
  if (gscTabId !== null) {
    try { await chrome.tabs.update(gscTabId, { url, active: false }); return gscTabId; }
    catch { gscTabId = null; }   // user closed it
  }
  const tab = await chrome.tabs.create({ url, active: true });
  gscTabId = tab.id;
  return gscTabId;
}

function inspectionUrl(property, authuser) {
  return "https://search.google.com/search-console"
    + "?resource_id=" + encodeURIComponent(property)
    + "&authuser=" + encodeURIComponent(authuser) + "&hl=en";
}

// Try to hand a job to the content script already living on the tab. Returns
// true once the content script acks (the real outcome arrives later as a
// separate runtime message). Returns false if it never answers (e.g. the SPA
// hard-navigated and the script is gone) so the caller can fall back to a
// full navigation.
async function dispatchJob(tabId, cmd, attempts) {
  // Attach the debugger up front so the "is debugging this browser" infobar
  // (which shifts page layout down) is already present BEFORE the content
  // script measures click coordinates — keeps trusted-click coords aligned
  // with the live layout. Failure is non-fatal: clicks fall back to synthetic.
  await ensureDebugger(tabId);
  for (let i = 0; i < attempts; i++) {
    try {
      const reply = await chrome.tabs.sendMessage(tabId, { type: "job",
        id: cmd.id, url: cmd.url, authuser: cmd.authuser });
      if (reply) return true;
    } catch { await sleep(1000); }   // content script not ready yet
  }
  return false;
}

async function runJob(cmd) {
  currentJobId = cmd.id;
  chrome.storage.local.set({ currentJob: cmd.url });

  // In-place reuse: when we're still on the same property+account, skip the
  // dashboard reload — the content script dismisses the popup and types the
  // next URL into the existing inspect bar. Only the first job of a property
  // (or a property/account switch, or a closed/logged-out tab) navigates.
  if (gscTabId !== null && cmd.property === lastProperty
      && cmd.authuser === lastAuthuser) {
    const tab = await chrome.tabs.get(gscTabId).catch(() => null);
    if (tab && !(tab.url || "").includes("accounts.google.com")) {
      send({ type: "progress", id: cmd.id, stage: "in_place" });
      if (await dispatchJob(gscTabId, cmd, 3)) {
        lastProperty = cmd.property; lastAuthuser = cmd.authuser;
        return;
      }
      // content script unreachable in place — fall through to a full reload
    }
  }

  send({ type: "progress", id: cmd.id, stage: "navigating" });
  let tabId;
  try {
    tabId = await ensureGscTab(inspectionUrl(cmd.property, cmd.authuser));
  } catch (e) {
    send({ type: "result", id: cmd.id, outcome: "error", detail: "tab: " + e.message });
    return;
  }
  await waitTabComplete(tabId, 45000);

  // logged out? GSC bounces to accounts.google.com where our content script
  // cannot run — detect via tab URL.
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  if (!tab) { send({ type: "result", id: cmd.id, outcome: "error", detail: "tab gone" }); return; }
  if (tab.url && tab.url.includes("accounts.google.com")) {
    send({ type: "result", id: cmd.id, outcome: "auth_required" });
    pushRecent(cmd.url, "auth_required");
    return;
  }

  // hand the page-level flow to the freshly-loaded content script
  if (!await dispatchJob(tabId, cmd, 10)) {
    send({ type: "result", id: cmd.id, outcome: "error", detail: "content script unreachable" });
    pushRecent(cmd.url, "error");
    return;
  }
  lastProperty = cmd.property; lastAuthuser = cmd.authuser;
  // content script reports the real outcome itself via runtime message below
}

// content script holds an open keepalive port for the run's duration — an open
// port stops Chrome terminating this worker mid-job. Re-arm the WS each time a
// port (re)connects so the worker that just woke is talking to the bridge.
chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== "keepalive") return;
  connect();
  port.onMessage.addListener(() => { connect(); });   // ping → ensure WS up
  port.onDisconnect.addListener(() => {});
});

// content script reports progress + final outcome through runtime messages
chrome.runtime.onMessage.addListener((m) => {
  if (m.type === "progress") send({ type: "progress", id: m.id, stage: m.stage });
  if (m.type === "result") {
    send({ type: "result", id: m.id, outcome: m.outcome, detail: m.detail,
           click_mode: m.click_mode });
    chrome.storage.local.get(["currentJob"], (v) => pushRecent(v.currentJob || "", m.outcome));
  }
});

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function waitTabComplete(tabId, timeoutMs) {
  return new Promise((resolve) => {
    const timer = setTimeout(() => { cleanup(); resolve(); }, timeoutMs);
    function listener(updatedTabId, info) {
      if (updatedTabId === tabId && info.status === "complete") { cleanup(); resolve(); }
    }
    function cleanup() { clearTimeout(timer); chrome.tabs.onUpdated.removeListener(listener); }
    chrome.tabs.onUpdated.addListener(listener);
  });
}

// ---- trusted input via chrome.debugger (CDP) ------------------------------
// GSC's Request-Indexing flow is guarded by an in-page bot-guard that reads
// interaction "trust" signals. A content script's element.click() /
// dispatchEvent() are isTrusted:false with no cursor path — detectably
// non-human, which trips the "try again later" soft-throttle even on the real
// logged-in profile. chrome.debugger lets us dispatch REAL CDP input
// (Input.dispatchMouseEvent) that the page sees as isTrusted:true with a
// genuine cursor movement, matching a human click. We attach to the GSC tab
// only; the page cannot observe the attachment from JS.
const DEBUGGER_VERSION = "1.3";

function dbg(tabId, method, params) {
  return new Promise((resolve, reject) => {
    chrome.debugger.sendCommand({ tabId }, method, params || {}, (res) => {
      const err = chrome.runtime.lastError;
      if (err) reject(new Error(err.message)); else resolve(res);
    });
  });
}

async function ensureDebugger(tabId) {
  if (attachedTabId === tabId) return true;
  if (attachedTabId !== null && attachedTabId !== tabId) {
    try { await chrome.debugger.detach({ tabId: attachedTabId }); } catch {}
    attachedTabId = null;
  }
  try {
    await chrome.debugger.attach({ tabId }, DEBUGGER_VERSION);
    attachedTabId = tabId;
    return true;
  } catch (e) {
    const msg = String((e && e.message) || e).toLowerCase();
    // MV3 service workers are ephemeral: after a recycle (e.g. during the
    // 30-90s pacing gap between submissions) our in-memory `attachedTabId`
    // resets to null even though Chrome STILL holds the attachment from the
    // previous worker instance. A fresh attach then fails with "Already
    // attached" — but the extension's attachment is still live, so sendCommand
    // works. RECLAIM it; do NOT fall back to a synthetic (isTrusted:false)
    // click, which is exactly what trips GSC's "try again later" throttle.
    if (msg.includes("already attached") || msg.includes("another debugger")) {
      attachedTabId = tabId;
      console.log("[gsc-bridge] reclaimed existing debugger attachment on tab", tabId);
      return true;
    }
    console.warn("[gsc-bridge] debugger attach FAILED:", msg);
    return false;
  }
}

function detachDebugger() {
  if (attachedTabId === null) return;
  const t = attachedTabId; attachedTabId = null;
  try { chrome.debugger.detach({ tabId: t }); } catch {}
}

// keep our handle honest if Chrome detaches us (tab closed, DevTools opened…)
chrome.debugger.onDetach.addListener((src) => {
  if (src.tabId === attachedTabId) attachedTabId = null;
});
chrome.tabs.onRemoved.addListener((tabId) => {
  if (tabId === attachedTabId) attachedTabId = null;
});

// Dispatch a human-like trusted left click at viewport CSS coords (x, y): a
// short multi-step cursor move into the target, then press/release with small
// randomized dwell times. Coords come from the content script's
// getBoundingClientRect — same CSS-pixel viewport origin CDP's Input uses.
async function trustedClick(tabId, x, y) {
  const steps = 3 + Math.floor(Math.random() * 3);
  const sx = x - (20 + Math.random() * 60);
  const sy = y - (15 + Math.random() * 40);
  for (let i = 1; i <= steps; i++) {
    await dbg(tabId, "Input.dispatchMouseEvent", {
      type: "mouseMoved",
      x: sx + (x - sx) * (i / steps),
      y: sy + (y - sy) * (i / steps),
    });
    await sleep(20 + Math.random() * 40);
  }
  await dbg(tabId, "Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
  await sleep(30 + Math.random() * 50);
  await dbg(tabId, "Input.dispatchMouseEvent", {
    type: "mousePressed", x, y, button: "left", buttons: 1, clickCount: 1 });
  await sleep(40 + Math.random() * 60);
  await dbg(tabId, "Input.dispatchMouseEvent", {
    type: "mouseReleased", x, y, button: "left", buttons: 0, clickCount: 1 });
}

// Trusted TYPING — CDP insertText looks to the page like a real paste/keystroke
// (isTrusted). Used to fill the inspect bar instead of synthetic value-setting.
async function trustedType(tabId, text) {
  await dbg(tabId, "Input.insertText", { text: String(text || "") });
}

// Trusted KEY — dispatch a real keyDown/keyUp (e.g. Enter, or Ctrl+A with
// modifiers=2). CDP modifiers: Alt=1, Ctrl=2, Meta=4, Shift=8.
async function trustedKey(tabId, key, modifiers) {
  const KEYS = {
    Enter:     { key: "Enter", code: "Enter", vk: 13, text: "\r" },
    a:         { key: "a", code: "KeyA", vk: 65 },
    Backspace: { key: "Backspace", code: "Backspace", vk: 8 },
  };
  const k = KEYS[key] || { key, code: key, vk: 0 };
  const base = { modifiers: modifiers || 0, key: k.key, code: k.code,
                 windowsVirtualKeyCode: k.vk, nativeVirtualKeyCode: k.vk };
  // a text key (Enter) sends text on keyDown; a shortcut (Ctrl+A) does not
  const down = { type: "keyDown", ...base };
  if (k.text && !modifiers) down.text = k.text;
  await dbg(tabId, "Input.dispatchKeyEvent", down);
  await sleep(20 + Math.random() * 40);
  await dbg(tabId, "Input.dispatchKeyEvent", { type: "keyUp", ...base });
}

// content script asks the worker for trusted input (it can't produce isTrusted
// events itself). One handler for click / type / key.
chrome.runtime.onMessage.addListener((m, sender, sendResponse) => {
  if (m.type !== "trusted_click" && m.type !== "trusted_type" && m.type !== "trusted_key") return;
  const tabId = sender.tab && sender.tab.id;
  if (tabId == null) { sendResponse({ ok: false, error: "no tab" }); return; }
  (async () => {
    if (!await ensureDebugger(tabId)) { sendResponse({ ok: false, error: "no debugger" }); return; }
    try {
      if (m.type === "trusted_click") {
        await trustedClick(tabId, m.x, m.y);
        console.log("[gsc-bridge] TRUSTED click on tab", tabId, "@", Math.round(m.x) + "," + Math.round(m.y));
      } else if (m.type === "trusted_type") {
        await trustedType(tabId, m.text);
        console.log("[gsc-bridge] TRUSTED type on tab", tabId);
      } else {
        await trustedKey(tabId, m.key, m.modifiers || 0);
        console.log("[gsc-bridge] TRUSTED key on tab", tabId, m.key);
      }
      sendResponse({ ok: true });
    } catch (e) {
      console.warn("[gsc-bridge]", m.type, "FAILED:", String((e && e.message) || e));
      sendResponse({ ok: false, error: String((e && e.message) || e) });
    }
  })();
  return true;                                    // async sendResponse
});
