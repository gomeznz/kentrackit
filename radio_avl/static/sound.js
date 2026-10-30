"use strict";
// Alert sounds generated with Web Audio: no sound files to ship or cache.
window.KTSound = (function () {
  let ctx = null, alarmTimer = null;
  const ensure = () => {
    if (!ctx) { try { ctx = new (window.AudioContext || window.webkitAudioContext)(); } catch (e) { return null; } }
    if (ctx.state === "suspended") ctx.resume();
    return ctx;
  };
  function tone(freq, start, dur, type = "sine", vol = 0.25) {
    const c = ensure(); if (!c) return;
    const o = c.createOscillator(), g = c.createGain();
    o.type = type; o.frequency.value = freq;
    g.gain.setValueAtTime(0, c.currentTime + start);
    g.gain.linearRampToValueAtTime(vol, c.currentTime + start + 0.02);
    g.gain.exponentialRampToValueAtTime(0.001, c.currentTime + start + dur);
    o.connect(g).connect(c.destination);
    o.start(c.currentTime + start); o.stop(c.currentTime + start + dur + 0.05);
  }
  return {
    unlocked: () => !!ctx && ctx.state === "running",
    unlock: () => ensure(),
    chime() { tone(880, 0, 0.25); tone(1320, 0.18, 0.35); },
    warn() { tone(660, 0, 0.2, "triangle", 0.3); tone(660, 0.28, 0.2, "triangle", 0.3); tone(495, 0.56, 0.35, "triangle", 0.3); },
    alarmStart() {
      if (alarmTimer) return;
      const burst = () => { for (let i = 0; i < 4; i++) { tone(1000, i * 0.3, 0.14, "square", 0.18); tone(750, i * 0.3 + 0.15, 0.14, "square", 0.18); } };
      burst(); alarmTimer = setInterval(burst, 2500);
    },
    alarmStop() { clearInterval(alarmTimer); alarmTimer = null; },
    alarming: () => !!alarmTimer,
  };
})();
