// ==UserScript==
// @name         Google Meet PTT
// @namespace    roman.googlemeet.ptt
// @version      5.0.0
// @description  Hard push-to-talk for Google Meet. The key is read system-wide by the Google Meet PTT app (127.0.0.1:8875); this script only drives Meet's mic button.
// @match        https://meet.google.com/*
// @noframes
// @run-at       document-idle
// @grant        GM_xmlhttpRequest
// @grant        unsafeWindow
// @connect      127.0.0.1
// ==/UserScript==

(function () {
  'use strict';

  const BASE = 'http://127.0.0.1:8875';
  const PROTO = 2; // must match PROTO in Google_Meet_PTT.py
  const HEADERS = { 'X-GMeet-PTT': String(PROTO), 'Content-Type': 'application/json' };

  const POLL_TIMEOUT_MS = 12000; // the app holds each poll for up to 8 s
  const RETRY_MS = 500;          // reconnect interval while the app is unreachable
  const CONFIRM_MS = 2000;       // one click in flight at a time; retried if Meet hasn't flipped by then
  const RELEASE_MS = 3000;       // keep forcing mute this long after losing the app mid-transmit
  const TICK_MS = 500;           // safety-net re-check (Chrome stretches it to ~1 s in background tabs)

  // One copy per page, even if the script got installed twice under different names.
  const root = document.documentElement;
  if (root.hasAttribute('data-gmeet-ptt')) return;
  root.setAttribute('data-gmeet-ptt', '');

  const ID = Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
  const now = () => performance.now();
  const log = (...a) => console.log('[Meet PTT]', ...a);
  const warn = (...a) => console.warn('[Meet PTT]', ...a);

  const S = {
    connected: false,
    cmd: { enabled: false, open: false, target: false },
    v: -1,
    releaseUntil: 0,
    pending: null,          // { at, from }: click sent, waiting for Meet to flip
    fails: 0,               // clicks in a row that Meet ignored
    mic: null,              // last findMic() result
    lastSent: '',
  };

  // ---------- Meet mic button ----------
  // Primary signal is [data-is-muted]; the Ctrl+D shortcut in the label identifies the mic in any UI language.
  const LABEL_ATTRS = ['aria-label', 'data-tooltip', 'title', 'aria-description'];
  const SHORTCUT_MIC = /(ctrl|cmd|⌘)\s*\+\s*d\b/i;
  const SHORTCUT_CAM = /(ctrl|cmd|⌘)\s*\+\s*e\b/i;
  const MIC_WORD = /мікрофон|microphone|\bmicro\b|\bmic\b/i;
  const CAM_WORD = /камер|camera|caméra/i;
  const SAYS_MUTED = /увімкнути мікрофон|turn on microphone|\bactiver le micro|\bunmute\b/i;
  const SAYS_LIVE = /вимкнути мікрофон|turn off microphone|désactiver le micro|couper le micro|\bmute\b/i;

  function labelOf(el) {
    let s = '';
    for (const a of LABEL_ATTRS) s += ' ' + (el.getAttribute(a) || '');
    return s;
  }

  function findMic() {
    let best = null;
    const consider = (el, hasFlag) => {
      const label = labelOf(el);
      if (CAM_WORD.test(label) || SHORTCUT_CAM.test(label)) return;
      const byShortcut = SHORTCUT_MIC.test(label);
      const byWord = MIC_WORD.test(label);
      if (!hasFlag && !((byShortcut || byWord) && (SAYS_MUTED.test(label) || SAYS_LIVE.test(label)))) return;
      const r = el.getBoundingClientRect();
      const score = (byShortcut ? 4 : 0) + (byWord ? 2 : 0) + (r.width > 4 && r.height > 4 ? 1 : 0);
      if (!best || score > best.score || (score === best.score && r.bottom > best.bottom)) {
        best = { el, label, score, bottom: r.bottom };
      }
    };
    for (const el of document.querySelectorAll('[data-is-muted]')) consider(el, true);
    if (!best) for (const el of document.querySelectorAll('button, [role="button"]')) consider(el, false);
    if (!best) return null;

    const flag = best.el.getAttribute('data-is-muted');
    let muted = flag === 'true' ? true : flag === 'false' ? false : null;
    if (muted === null) muted = SAYS_MUTED.test(best.label) ? true : SAYS_LIVE.test(best.label) ? false : null;
    return { el: best.el, muted };
  }

  // ---------- enforcement: mic open only while the app says so ----------
  function desiredMuted() {
    if (!S.connected) return now() < S.releaseUntil ? true : null; // null = hands off
    if (!S.cmd.enabled) return null;
    return !(S.cmd.open && S.cmd.target);
  }

  let busy = false;
  function reconcile() {
    if (busy) return;
    busy = true;
    try {
      const t = now();
      const mic = findMic();
      if (!!mic !== !!S.mic) log(mic ? 'mic button found' : 'mic button gone');
      S.mic = mic;
      const muted = mic ? mic.muted : null;

      if (S.pending) {
        if (muted !== null && muted !== S.pending.from) {
          S.pending = null;
          S.fails = 0;
        } else if (t - S.pending.at >= CONFIRM_MS) {
          S.pending = null;
          S.fails++;
          warn('Meet ignored the mic click', S.fails);
        }
      }

      const want = desiredMuted();
      if (mic && muted !== null && want !== null && muted !== want && !S.pending) {
        S.pending = { at: t, from: muted };
        mic.el.click();
        setTimeout(reconcile, CONFIRM_MS + 50);
      }
    } catch (e) {
      warn('reconcile failed', e);
    } finally {
      busy = false;
    }
    report();
  }

  // ---------- link to the app ----------
  function status() {
    const m = S.mic;
    let error = '';
    if (m && m.muted === null) error = 'mic-state-unknown';
    else if (m && S.fails >= 2) error = 'mic-unresponsive';
    return {
      hasMic: !!m,
      micOn: !!m && m.muted === false,
      error,
      focused: document.visibilityState === 'visible' && document.hasFocus(),
      url: location.host + location.pathname,
    };
  }

  function send(path, body, timeout) {
    try {
      GM_xmlhttpRequest({
        method: 'POST',
        url: BASE + path,
        headers: HEADERS,
        timeout,
        data: JSON.stringify({ id: ID, ...body }),
      });
    } catch (e) {
      warn('request failed', e);
    }
  }

  function report() {
    const st = status();
    const key = JSON.stringify(st);
    if (key === S.lastSent) return;
    S.lastSent = key;
    send('/status', st, 3000);
  }

  function lost() {
    if (S.connected) {
      S.connected = false;
      // App vanished while we were transmitting: close the mic, then stop touching it.
      if (S.cmd.enabled && S.cmd.open && S.cmd.target) S.releaseUntil = now() + RELEASE_MS;
      warn('app unreachable — PTT inactive until it is back');
    }
    reconcile();
  }

  // Long-poll: the app answers the moment the key state changes, so there is no timer on the
  // key path and Chrome's background-tab timer throttling can't delay it.
  let gen = 0;
  let req = null;
  let pollStartedAt = 0;

  function poll() {
    const my = ++gen;
    if (req) { try { req.abort(); } catch (_) {} }
    req = null;
    pollStartedAt = now();

    const fail = () => {
      if (my !== gen) return;
      req = null;
      lost();
      setTimeout(() => { if (my === gen) poll(); }, RETRY_MS);
    };

    try {
      req = GM_xmlhttpRequest({
        method: 'POST',
        url: BASE + '/poll',
        headers: HEADERS,
        timeout: POLL_TIMEOUT_MS,
        data: JSON.stringify({ id: ID, v: S.v, ...status() }),
        onload: r => {
          if (my !== gen) return;
          let d = null;
          if (r.status === 200) { try { d = JSON.parse(r.responseText); } catch (_) {} }
          if (!d || typeof d.v !== 'number') {
            if (r.status === 409) warn('script and app versions differ — update both');
            return fail();
          }
          req = null;
          if (!S.connected) log('connected to app');
          S.connected = true;
          S.releaseUntil = 0;
          S.v = d.v;
          S.cmd = { enabled: !!d.enabled, open: !!d.open, target: !!d.target };
          reconcile();
          poll();
        },
        onerror: fail,
        ontimeout: fail,
        onabort: fail,
      });
    } catch (e) {
      warn('poll failed', e);
      fail();
    }
  }

  // ---------- wiring ----------
  new MutationObserver(reconcile).observe(root, {
    subtree: true,
    attributes: true,
    attributeFilter: ['data-is-muted'],
  });

  window.addEventListener('focus', report);
  window.addEventListener('blur', report);
  document.addEventListener('visibilitychange', report);
  window.addEventListener('pagehide', () => send('/status', { closing: true }, 1000));
  window.addEventListener('pageshow', e => { if (e.persisted) poll(); });

  setInterval(() => {
    reconcile();
    // A GM request that never calls back would otherwise stall the loop for good.
    if (now() - pollStartedAt > POLL_TIMEOUT_MS + 3000) {
      warn('poll stuck — restarting');
      poll();
    }
  }, TICK_MS);

  try { unsafeWindow.meetPtt = { state: S, findMic, reconcile }; } catch (_) {}

  log('loaded', ID);
  reconcile();
  poll();
})();
