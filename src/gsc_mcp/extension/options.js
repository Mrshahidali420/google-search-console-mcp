const $ = (id) => document.getElementById(id);

// --- pairing by URL fragment (fallback) --------------------------------------
// The extension now pairs itself over the socket, so this path only runs when
// a token is pasted in deliberately — e.g. to hand one to an extension
// loaded from a folder the server cannot verify. A fragment is never
// sent to a server and this is a chrome-extension:// page, so the token stays
// local; it is stripped from history the moment it is read.
function applyPairingFragment() {
  const hash = location.hash.startsWith("#") ? location.hash.slice(1) : "";
  if (!hash) return false;
  const q = new URLSearchParams(hash);
  const token = q.get("t");
  if (!token) return false;
  const port = Number(q.get("p")) || 8765;

  history.replaceState(null, "", location.pathname);   // drop it from history
  chrome.storage.local.set({ token, port, paused: false }, () => {
    $("token").value = token;
    $("port").value = port;
    $("msg").textContent = "paired — connecting…";
    chrome.runtime.sendMessage({ type: "reconnect" });
  });
  return true;
}

if (!applyPairingFragment()) {
  chrome.storage.local.get(["token", "port"], (v) => {
    if (v.token) $("token").value = v.token;
    if (v.port) $("port").value = v.port;
  });
}

$("save").addEventListener("click", () => {
  chrome.storage.local.set(
    { token: $("token").value.trim(), port: Number($("port").value) || 8765,
      paused: false },
    () => { $("msg").textContent = "saved — reconnecting";
            chrome.runtime.sendMessage({ type: "reconnect" }); });
});

// --- live connection status -------------------------------------------------
// background.js already writes `bridgeState`; surfacing it here means "did it
// work?" is answered on the page you are already looking at, instead of by
// opening the popup or reading a log.
const STATES = {
  "connected":            ["ok",   "● Connected to the tool"],
  "waiting for the tool": ["warn", "○ Waiting for the tool — normal when no job "
                                 + "is running; it connects by itself"],
  "pairing…":             ["warn", "◐ Pairing…"],
  "paused":               ["warn", "‖ Stopped — press Connect now to resume"],
  "token stale — re-pairing": ["warn", "◐ Token was refused — pairing again"],
};
function paintState(s) {
  const el = $("bridgeState");
  if (!el) return;
  const [cls, text] = STATES[s]
    || (s && s.startsWith("not paired") ? ["bad", "✕ " + s] : null)
    || (s ? ["warn", "• " + s] : ["warn", "○ unknown — open the popup once"]);
  el.className = "state " + cls;
  el.textContent = text;
}

const ago = (t) => {
  const s = Math.round((Date.now() - t) / 1000);
  return s < 60 ? `${s}s ago` : `${Math.round(s / 60)}m ago`;
};
function paintDiag(v) {
  const bits = [];
  if (v.lastPair) {
    bits.push(`${v.lastPair.ok ? "Paired" : "Pairing failed"}: `
      + `${v.lastPair.detail} (${ago(v.lastPair.at)})`);
  }
  if (v.lastAttempt && (!v.lastPair || v.lastAttempt.at > v.lastPair.at)) {
    bits.push(`Last attempt ${ago(v.lastAttempt.at)}: ${v.lastAttempt.detail}`);
  }
  $("diag").textContent = bits.join(" · ");
}

const WATCHED = ["bridgeState", "lastPair", "lastAttempt"];
chrome.storage.local.get(WATCHED, (v) => { paintState(v.bridgeState); paintDiag(v); });
chrome.storage.onChanged.addListener((ch, area) => {
  if (area !== "local") return;
  if (WATCHED.some((k) => ch[k])) {
    chrome.storage.local.get(WATCHED, (v) => { paintState(v.bridgeState); paintDiag(v); });
  }
});

// --- one-button setup --------------------------------------------------------
// The extension asks the tool for its own token (the tool checks the ID below
// against the extension it finds in the browser profile before answering), so
// nothing here needs a file to be opened or a string to be copied.
$("extId").textContent = chrome.runtime.id;
$("copyId").addEventListener("click", () => {
  navigator.clipboard.writeText(chrome.runtime.id)
    .then(() => { $("copyId").textContent = "Copied"; })
    .catch(() => { $("copyId").textContent = "Copy failed"; });
});

$("pair").addEventListener("click", () => {
  const btn = $("pair");
  btn.disabled = true;
  $("pairMsg").textContent = " pairing…";
  chrome.runtime.sendMessage(
    { type: "pair_now", port: Number($("port").value) || 8765 },
    (res) => {
      btn.disabled = false;
      $("pairMsg").textContent = res
        ? (res.ok ? " ✓ paired" : " ✕ " + res.detail)
        : " ✕ the extension's worker did not answer — reload the extension";
      if (res && res.ok) chrome.storage.local.get(["token"], (v) => {
        if (v.token) $("token").value = v.token;
      });
    });
});

// --- submission method (click vs recorded RPC) ------------------------------
// The rpcid matters more than the endpoint: the first recorded recipe pointed
// at the right endpoint but the WRONG rpc (GSC's inspection call, not the
// submission), which replayed cleanly and indexed nothing. Show it, and show
// what else was seen, so a recipe can be judged before it is switched on.
function paintRecipe(r, captures) {
  const lines = [];
  lines.push(r
    ? `Recipe: rpc ${r.rpcids || "(none — pre-1.6.1, re-learn)"} recorded `
      + `${new Date(r.learnedAt).toLocaleString()}, ${r.phase || "?"}, `
      + `${r.endpoint.slice(0, 60)}…`
    : "Recipe: none recorded yet. Choose Learn, then run one job; the "
      + "submission fired by that click is recorded.");
  if (captures && captures.length) {
    lines.push(`Seen during learning (${captures.length}):`);
    for (const c of captures) {
      lines.push(`  · ${c.phase}  rpc ${c.rpcids || "?"}  ${c.bodyLen}b`);
    }
    lines.push("Only a post-click rpcid unseen before the click is treated as "
      + "the submission.");
  }
  $("recipe").textContent = lines.join("\n");
}
chrome.storage.local.get(["rpcMode", "rpcRecipe", "rpcCaptures"], (v) => {
  $("rpcMode").value = v.rpcMode || "off";
  paintRecipe(v.rpcRecipe, v.rpcCaptures);
});
$("saveRpc").addEventListener("click", () => {
  chrome.storage.local.set({ rpcMode: $("rpcMode").value },
    () => { $("rpcMsg").textContent = "saved"; });
});
$("forget").addEventListener("click", () => {
  chrome.storage.local.remove(["rpcRecipe", "rpcCaptures"], () => {
    paintRecipe(null, null);
    $("rpcMsg").textContent = "recipe cleared";
  });
});
// Learning happens in the content script; reflect it live if this page is open.
chrome.storage.onChanged.addListener((ch, area) => {
  if (area !== "local") return;
  if (ch.rpcRecipe || ch.rpcCaptures) {
    chrome.storage.local.get(["rpcRecipe", "rpcCaptures"],
      (v) => paintRecipe(v.rpcRecipe, v.rpcCaptures));
  }
  if (ch.rpcMode) $("rpcMode").value = ch.rpcMode.newValue || "off";
});
