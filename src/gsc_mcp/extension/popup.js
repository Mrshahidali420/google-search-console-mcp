// GSC MCP Bridge — popup controller.
// Live status panel + recent-activity feed + inline bridge settings.
const $ = (id) => document.getElementById(id);

// outcome -> { label, cls (badge color), bucket (summary tally) }
const OUTCOMES = {
  submitted:             { label: "Submitted",  cls: "ok",   bucket: "ok" },
  submitted_unconfirmed: { label: "Submitted?", cls: "ok",   bucket: "ok" },
  already_indexed:       { label: "Indexed",    cls: "ok",   bucket: "ok" },
  quota_exceeded:        { label: "Quota",      cls: "warn", bucket: "quota" },
  no_slot:               { label: "No slot",    cls: "warn", bucket: "quota" },
  timeout:               { label: "Timeout",    cls: "warn", bucket: "" },
  captcha:               { label: "Captcha",    cls: "info", bucket: "" },
  captcha_after_click:   { label: "Captcha",    cls: "info", bucket: "" },
  account_mismatch:      { label: "Wrong acct", cls: "info", bucket: "" },
  auth_required:         { label: "Sign in",    cls: "info", bucket: "" },
  skipped:               { label: "Skipped",    cls: "info", bucket: "" },
  error:                 { label: "Error",      cls: "bad",  bucket: "err" },
};
const meta = (o) => OUTCOMES[o] || { label: o || "?", cls: "bad", bucket: "err" };

function classifyState(s) {
  s = (s || "waiting for the tool").toLowerCase();
  if (s === "connected") return "ok";
  // "waiting" and "pairing" are the resting states of a working extension —
  // the tool only listens while a job runs. Red would cry wolf.
  if (s === "paused" || s.includes("token") || s.startsWith("waiting")
      || s.startsWith("pairing")) return "warn";
  return "bad";
}

function splitUrl(u) {
  try { const x = new URL(u); return { host: x.host, path: (x.pathname + x.search) || "/" }; }
  catch { return { host: "", path: u || "" }; }
}

function openTab(url) { if (url) chrome.tabs.create({ url }); }

// ---- render (re-runnable on every storage change) -------------------------
function renderState() {
  chrome.storage.local.get(["bridgeState", "currentJob", "recent", "port", "lastClick"], (v) => {
    const state = v.bridgeState || "disconnected";
    const cls = classifyState(state);

    const pill = $("pill");
    pill.className = "pill " + cls;
    $("state").textContent = state.charAt(0).toUpperCase() + state.slice(1);
    $("state-sub").textContent = cls === "ok" ? "live" : "";
    // when stopped, the primary button becomes "Resume"
    $("reconnect-lbl").textContent = state.toLowerCase() === "paused" ? "Resume" : "Reconnect";

    // current job
    const now = $("now");
    if (v.currentJob) { now.classList.add("show"); $("now-url").textContent = v.currentJob; }
    else now.classList.remove("show");

    // summary tallies + feed
    const recent = v.recent || [];
    let ok = 0, quota = 0, err = 0;
    const feed = $("feed");
    feed.textContent = "";
    for (const r of recent) {
      const m = meta(r.outcome);
      if (m.bucket === "ok") ok++; else if (m.bucket === "quota") quota++; else if (m.bucket === "err") err++;

      const { host, path } = splitUrl(r.url);
      const li = document.createElement("li");
      li.className = "item";
      li.title = r.url;
      li.addEventListener("click", () => openTab(r.url));

      const badge = document.createElement("span");
      badge.className = "badge " + m.cls;
      const d = document.createElement("span"); d.className = "d";
      badge.appendChild(d);
      badge.appendChild(document.createTextNode(m.label));

      const wrap = document.createElement("div"); wrap.className = "meta";
      const p = document.createElement("div"); p.className = "path"; p.textContent = path;
      const h = document.createElement("div"); h.className = "host"; h.textContent = host;
      wrap.appendChild(p); wrap.appendChild(h);

      li.appendChild(badge); li.appendChild(wrap);
      feed.appendChild(li);
    }
    $("c-ok").textContent = ok;
    $("c-quota").textContent = quota;
    $("c-err").textContent = err;

    const hasItems = recent.length > 0;
    feed.style.display = hasItems ? "" : "none";
    $("empty").style.display = hasItems ? "none" : "";
    $("summary").style.display = hasItems ? "" : "none";

    $("f-port").textContent = v.port || 8765;

    // live trusted-vs-synthetic click indicator
    const fc = $("f-click");
    const lc = v.lastClick;
    if (!lc) { fc.className = "click-stat"; fc.textContent = "no clicks yet"; }
    else if (lc.mode === "trusted") { fc.className = "click-stat trusted"; fc.textContent = "Trusted clicks"; }
    else { fc.className = "click-stat synthetic"; fc.textContent = "Synthetic — throttle risk"; }
  });
}

// ---- settings (populated once so live updates never clobber typing) -------
function populateSettings() {
  chrome.storage.local.get(["token", "port"], (v) => {
    if (v.token) $("token").value = v.token;
    if (v.port) $("port").value = v.port;
  });
}

// ---- wiring ---------------------------------------------------------------
$("gear").addEventListener("click", () => {
  $("settings").classList.toggle("show");
  $("gear").classList.toggle("on");
});

$("reconnect").addEventListener("click", () => {
  chrome.runtime.sendMessage({ type: "reconnect" });   // also clears paused
  $("state").textContent = "Reconnecting…";
  $("pill").className = "pill warn";
});

$("stop").addEventListener("click", () => {
  chrome.runtime.sendMessage({ type: "pause" });        // disconnect + abort + suppress reconnect
  $("state").textContent = "Paused";
  $("state-sub").textContent = "";
  $("pill").className = "pill warn";
  $("reconnect-lbl").textContent = "Resume";
});

$("open-sc").addEventListener("click", () =>
  openTab("https://search.google.com/search-console"));

$("clear").addEventListener("click", () =>
  chrome.storage.local.set({ recent: [] }, renderState));

$("clear-pending").addEventListener("click", () => {
  chrome.runtime.sendMessage({ type: "clear" });
  $("clear-msg").textContent = "clearing…";
  setTimeout(() => { $("clear-msg").textContent = ""; }, 3500);
});

$("save").addEventListener("click", () => {
  const token = $("token").value.trim();
  const port = Number($("port").value) || 8765;
  chrome.storage.local.set({ token, port }, () => {
    $("save-msg").textContent = "saved — reconnecting";
    chrome.runtime.sendMessage({ type: "reconnect" });
    setTimeout(() => { $("save-msg").textContent = ""; }, 2500);
  });
});

// re-render live while the popup is open (connection flips, jobs land)
chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local") return;
  if (changes.bridgeState || changes.currentJob || changes.recent || changes.port || changes.lastClick)
    renderState();
});

renderState();
populateSettings();
