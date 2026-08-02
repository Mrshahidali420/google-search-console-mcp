// rpc-main.js — runs in the PAGE'S MAIN WORLD (see manifest "world": "MAIN").
//
// WHY THIS EXISTS
// The click path (content.js + chrome.debugger) imitates a human pressing
// "Request Indexing". Search Console's front end does not actually index
// anything on click; it fires an authenticated request to a Google endpoint.
// This module skips the imitation and issues that same request directly, from
// inside the page that is already logged in. There is no synthetic event, no
// isTrusted problem, no cursor path to fake, and no debugger attachment to
// keep alive across MV3 service-worker evictions.
//
// WHY IT LEARNS INSTEAD OF HARDCODING
// The endpoint, its RPC id and its payload shape are obfuscated and change
// without notice. Hardcoding today's shape produces a tool that silently
// breaks later. So the extension watches ONE real submission, records the
// exact request that carried the URL, and replays that recipe for subsequent
// URLs. When Google changes the shape, you re-learn in one click instead of
// reverse-engineering again.
//
// WHAT THIS DOES NOT DO
// It does not raise the quota. Request Indexing is capped per Google account
// (~11 on a rolling 24h window, per quota.py); that cap is enforced server
// side and applies identically to a click and to this request. This removes
// the fragility, not the ceiling. Keep using the quota ledger.
//
// TRUST NOTE: this world is shared with the page, so a hostile page could post
// these messages too. Acceptable for a private tool driving google.com; do not
// widen the host matches without revisiting that.
(() => {
  "use strict";

  const FLAG = "__gscrpc__";
  const post = (type, payload) =>
    window.postMessage({ [FLAG]: true, dir: "up", type, ...payload }, "*");

  // Headers the browser must own. Setting these on a replay either throws or
  // makes the request look wrong; the browser fills them in correctly anyway
  // because we are same-origin inside the real page.
  const DENY = new Set([
    "cookie", "content-length", "host", "origin", "referer", "user-agent",
    "connection", "accept-encoding", "sec-fetch-site", "sec-fetch-mode",
    "sec-fetch-dest", "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
  ]);

  let armed = null; // { targetUrl, phase, captures: [] } while learning

  // WHY THIS RECORDS EVERY CANDIDATE INSTEAD OF THE FIRST ONE
  // The original rule — "the submission is the request whose body carries the
  // URL" — is not selective enough, and that cost a real slot on 2026-07-25.
  // Opening an inspection and pressing Request Indexing both send the URL:
  // GSC runs its own quick test as part of the request flow. The recorder
  // stopped at the FIRST match, so it learned the INSPECTION call, and every
  // later "replay" re-ran an inspection. That returns a clean HTTP 200 and
  // queues nothing — which is exactly what was observed.
  // So: capture all of them, tag which side of the click each arrived on, and
  // let the picker choose. A candidate is only the submission if it appeared
  // AFTER the click and its rpcid was never seen before it.
  const rpcidsOf = (endpoint) => {
    try {
      const v = new URL(endpoint, location.href).searchParams.get("rpcids");
      return v || "";
    } catch { return ""; }
  };

  // A URL can appear in a payload raw, percent-encoded, or double-encoded.
  // Record which form matched so the replay substitutes the same form.
  const forms = (u) => [u, encodeURIComponent(u), encodeURIComponent(encodeURIComponent(u))];

  function headersToObject(h) {
    const out = {};
    try {
      if (!h) return out;
      if (typeof Headers !== "undefined" && h instanceof Headers) {
        h.forEach((v, k) => { if (!DENY.has(k.toLowerCase())) out[k] = v; });
      } else if (Array.isArray(h)) {
        for (const [k, v] of h) if (!DENY.has(String(k).toLowerCase())) out[k] = v;
      } else if (typeof h === "object") {
        for (const k of Object.keys(h)) if (!DENY.has(k.toLowerCase())) out[k] = h[k];
      }
    } catch {}
    return out;
  }

  function bodyToString(body) {
    if (body == null) return null;
    if (typeof body === "string") return body;
    try {
      if (body instanceof URLSearchParams) return body.toString();
    } catch {}
    return null; // FormData / Blob / ArrayBuffer are not replayable as text
  }

  // Only Google's RPC transport is interesting; everything else on the page is
  // noise. Having an rpcids param is what makes a request one of these.
  const isRpcCall = (endpoint) =>
    /batchexecute/.test(endpoint) || /[?&]rpcids=/.test(endpoint);

  // Called for every outgoing request while learning.
  //
  // Records EVERY rpc call, not just the ones carrying the URL. The 2026-07-25
  // learn run recorded exactly one URL-bearing call (RVtklb) and replaying it
  // indexed nothing — which means either that call is the inspection and the
  // real submission identifies its URL some other way (server-side context, an
  // internal id), or it is the submission and the replay fails for an unrelated
  // reason. A URL-only recorder cannot tell those apart, because in the first
  // case the request we need is never a candidate. So: observe everything,
  // replay only what carries the URL.
  function observe({ endpoint, method, headers, body }) {
    if (!armed || !body || !isRpcCall(endpoint)) return;
    const candidates = forms(armed.targetUrl);
    const level = candidates.findIndex((c) => body.includes(c));

    if (level < 0) {
      armed.observed.push({
        endpoint, rpcids: rpcidsOf(endpoint), phase: armed.phase,
        bodyLen: body.length, hasUrl: false,
        // Keep the payload: the submission (Fj3Owf, found 2026-07-25) does not
        // carry the URL, so its shape is the only way to learn how it names its
        // target. `at` is an XSRF token and is refreshed per session anyway —
        // strip it rather than write it into a log file.
        body: body.replace(/((^|&)at=)[^&]*/, "$1<redacted>").slice(0, 1200),
        method: (method || "POST").toUpperCase(),
        headers,
      });
      return;
    }

    armed.captures.push({
      endpoint,
      method: (method || "POST").toUpperCase(),
      headers,
      bodyTemplate: body,
      marker: candidates[level],
      markerLevel: level,
      rpcids: rpcidsOf(endpoint),
      phase: armed.phase,               // "pre-click" | "post-click"
      learnedFrom: armed.targetUrl,
      learnedAt: new Date().toISOString(),
    });
  }

  // Pick the submission out of everything seen. Deliberately conservative:
  // when nothing satisfies the rule it returns null and the caller keeps
  // clicking, rather than handing back a plausible wrong recipe.
  function pickSubmission(captures) {
    const before = new Set(
      captures.filter((c) => c.phase === "pre-click" && c.rpcids)
              .map((c) => c.rpcids));
    const after = captures.filter(
      (c) => c.phase === "post-click" && c.rpcids && !before.has(c.rpcids));
    // Last one wins: GSC fires its internal test first, the submission last.
    return after.length ? after[after.length - 1] : null;
  }

  // The rpc that only ever appears after the click, whether or not it carries
  // the URL. This is the diagnostic answer to "which call is the submission",
  // and it is NOT automatically replayable: if it has no URL in its body it
  // names its target some other way and a naive marker swap cannot retarget it.
  function identifySubmission(all) {
    const before = new Set(
      all.filter((c) => c.phase === "pre-click" && c.rpcids).map((c) => c.rpcids));
    const after = all.filter(
      (c) => c.phase === "post-click" && c.rpcids && !before.has(c.rpcids));
    return after.length ? after[after.length - 1] : null;
  }

  // Opaque continuation tokens: the submission (Fj3Owf) takes two of these and
  // no URL, so they must come out of the inspection's RESPONSE. Recording the
  // response is what makes the extraction path findable — and it is free,
  // because inspecting costs no quota. Only submitting does.
  // 40, not 80: the first Fj3Owf argument was only 22 chars, so the handles are
  // not all long. The 80-char probe on 2026-07-25 found nothing and that null
  // result may have been the threshold, not the absence of the handles.
  const TOKENISH = /[A-Za-z0-9_-]{40,}/g;
  // Static assets can't carry a per-inspection handle and would drown the log.
  const IS_ASSET = /\.(js|css|png|jpg|jpeg|gif|svg|woff2?|ico)(\?|$)/i;
  function recordResponse(endpoint, text) {
    if (!armed || !text) return;
    armed.responses.push({
      endpoint, rpcids: rpcidsOf(endpoint), phase: armed.phase,
      len: text.length,
      tokens: Array.from(new Set(text.match(TOKENISH) || [])).slice(0, 12),
      body: text.slice(0, 4000),
      // kept only for matching at harvest, stripped before it leaves this world
      _full: text.slice(0, 200000),
    });
  }

  // Where did the submission's arguments come from?
  //
  // Fj3Owf takes two opaque handles and no URL. If those exact strings appear
  // in a response recorded BEFORE the click, the submission can be rebuilt:
  // inspect, read the handles out of that response, fire the rpc. If they
  // appear nowhere, they are minted per click and the RPC path is dead — that
  // is a real answer too, and worth one slot to get.
  function traceArgs(identified, responses) {
    if (!identified || !identified.body) return [];
    const args = Array.from(new Set(
      (decodeURIComponent(identified.body).match(/[A-Za-z0-9_-]{20,}/g) || [])));
    return args.slice(0, 10).map((a) => {
      const hit = responses.find((r) => r._full && r._full.includes(a));
      return {
        arg: a.slice(0, 28) + (a.length > 28 ? "…" : ""),
        len: a.length,
        foundIn: hit ? (hit.rpcids || hit.endpoint.slice(0, 60)) : null,
      };
    });
  }

  // Everything Google sends this page, not just batchexecute. The narrow probe
  // could only prove the handles were absent from the calls it already knew
  // about, which is not the same as absent.
  const isWatchable = (endpoint) => {
    try {
      const u = new URL(endpoint, location.href);
      return /google\.com$/.test(u.hostname.replace(/^www\./, ""))
        && !IS_ASSET.test(u.pathname);
    } catch { return false; }
  };

  // ---- interceptors -------------------------------------------------------
  const origFetch = window.fetch;
  window.fetch = function (input, init) {
    let watch = null;
    try {
      if (armed) {
        const req = input instanceof Request ? input : null;
        const endpoint = req ? req.url : String(input);
        const method = (init && init.method) || (req && req.method) || "GET";
        if (method.toUpperCase() === "POST") {
          const body = bodyToString(init && init.body);
          if (body) observe({ endpoint, method, headers: headersToObject((init && init.headers) || (req && req.headers)), body });
        }
        if (isWatchable(endpoint)) watch = endpoint;
      }
    } catch {}
    const p = origFetch.apply(this, arguments);
    if (watch) {
      // clone() so the app still gets an unread body — never consume the
      // original stream or Search Console breaks under us.
      try {
        p.then((res) => {
          try { res.clone().text().then((t) => recordResponse(watch, t), () => {}); } catch {}
          return res;
        }, () => {});
      } catch {}
    }
    return p;
  };

  const origOpen = XMLHttpRequest.prototype.open;
  const origSetHeader = XMLHttpRequest.prototype.setRequestHeader;
  const origSend = XMLHttpRequest.prototype.send;

  XMLHttpRequest.prototype.open = function (method, url) {
    try { this.__gsc = { method, url, headers: {} }; } catch {}
    return origOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.setRequestHeader = function (k, v) {
    try { if (this.__gsc && !DENY.has(String(k).toLowerCase())) this.__gsc.headers[k] = v; } catch {}
    return origSetHeader.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function (body) {
    try {
      if (armed && this.__gsc && String(this.__gsc.method).toUpperCase() === "POST") {
        const s = bodyToString(body);
        if (s) observe({ endpoint: this.__gsc.url, method: this.__gsc.method, headers: this.__gsc.headers, body: s });
        if (isWatchable(this.__gsc.url)) {
          const url = this.__gsc.url;
          this.addEventListener("load", () => {
            try { recordResponse(url, this.responseText); } catch {}
          });
        }
      }
    } catch {}
    return origSend.apply(this, arguments);
  };

  // ---- message bridge (isolated world -> here) ----------------------------
  window.addEventListener("message", async (ev) => {
    if (ev.source !== window) return;
    const d = ev.data;
    if (!d || d[FLAG] !== true || d.dir !== "down") return;

    if (d.type === "arm") {
      armed = { targetUrl: d.url, phase: "pre-click", captures: [], observed: [],
                responses: [] };
      post("armed", { url: d.url });
      return;
    }
    // content.js flips the phase the moment it clicks Request Indexing; that
    // boundary is the only thing separating the submission from the inspection.
    if (d.type === "phase") {
      if (armed) armed.phase = d.phase;
      return;
    }
    if (d.type === "harvest") {
      const captures = armed ? armed.captures : [];
      const observed = armed ? armed.observed : [];
      const recipe = pickSubmission(captures);
      const all = captures
        .map((c) => ({ rpcids: c.rpcids, phase: c.phase, endpoint: c.endpoint,
                       body: c.bodyTemplate, hasUrl: true }))
        .concat(observed);
      const identified = identifySubmission(all);
      const responses = armed ? armed.responses : [];
      const trace = traceArgs(identified, responses);
      armed = null;
      post("harvested", {
        recipe,
        identified,
        trace,
        responses: responses.map(({ _full, ...r }) => r),
        // Everything seen, replayable or not, kept whole so a failed pick can
        // be diagnosed from stored state instead of by spending another slot.
        captures: captures
          .map((c) => ({
            endpoint: c.endpoint, rpcids: c.rpcids, phase: c.phase,
            bodyLen: c.bodyTemplate.length, markerLevel: c.markerLevel,
            hasUrl: true,
          }))
          .concat(observed),
      });
      return;
    }
    if (d.type === "disarm") { armed = null; return; }
  });

  post("ready", {});
})();
