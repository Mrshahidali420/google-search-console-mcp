// The tool opens this page when the extension has not shown up within a few
// seconds of a run starting. Loading any extension page starts an evicted MV3
// service worker immediately — that is the whole purpose. The worker connects
// (pairing itself first if it has no token), and this tab removes itself.
//
// Without this the only legal wake-up is chrome.alarms, whose minimum period is
// 30 seconds, so every run began with up to half a minute of apparent silence.

const CLOSE_AFTER_CONNECTED_MS = 1200;   // let the user see it worked
const GIVE_UP_MS = 25000;                // then leave the tab up with the reason

let done = false;

function say(text) { document.getElementById("msg").textContent = text; }

function closeSelf() {
  if (done) return;
  done = true;
  chrome.runtime.sendMessage({ type: "close_me" });
}

function onState(state) {
  if (done) return;
  if (state === "connected") {
    say("Connected — closing…");
    setTimeout(closeSelf, CLOSE_AFTER_CONNECTED_MS);
  } else if (state && state.startsWith("not paired")) {
    say(state);           // a refusal is worth reading, so stop here
    done = true;
  }
}

chrome.storage.onChanged.addListener((ch, area) => {
  if (area === "local" && ch.bridgeState) onState(ch.bridgeState.newValue);
});
chrome.storage.local.get(["bridgeState"], (v) => onState(v.bridgeState));

// Nudge the worker rather than waiting for its next alarm tick.
chrome.runtime.sendMessage({ type: "reconnect" });

setTimeout(() => {
  if (done) return;
  done = true;
  chrome.storage.local.get(["lastAttempt"], (v) => {
    say("Could not reach the tool. " + ((v.lastAttempt && v.lastAttempt.detail) || ""));
  });
}, GIVE_UP_MS);
