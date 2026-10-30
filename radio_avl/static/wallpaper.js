"use strict";
// Animated "live dispatch" background for the sign-in pages: units gliding along a faint road
// network leaving trails, with radio pulses from the repeater. Drawn on a canvas in the browser,
// so it costs the Pi nothing and works offline. Holds still if the user prefers reduced motion.
//
// Interactive: the pointer is a handheld radio. Units near it show a link line; clicking the
// background sends a poll and every unit the pulse reaches answers with its ID.
// There may also be one or two things to find. 73.
window.KTWallpaper = function (canvas) {
  const ctx = canvas.getContext("2d");
  const still = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  let W, H, dpr, roads = [], units = [], hub, t0 = performance.now(), now = 0;
  const rnd = (a, b) => a + Math.random() * (b - a);
  const ptr = { x: -1e4, y: -1e4, seen: false, last: 0 };
  const polls = [];          // {x, y, t}
  const replies = [];        // {u, t}
  let parts = [];            // formation particles
  let mode = null;           // {kind:'formation'|'convoy', t, until, target?}

  // ---- geometry ---------------------------------------------------------------
  function catmull(pts, steps = 18) {
    const out = [];
    for (let i = 0; i < pts.length - 1; i++) {
      const p0 = pts[Math.max(0, i - 1)], p1 = pts[i], p2 = pts[i + 1], p3 = pts[Math.min(pts.length - 1, i + 2)];
      for (let s = 0; s < steps; s++) {
        const t = s / steps, t2 = t * t, t3 = t2 * t;
        out.push([
          0.5 * (2 * p1[0] + (-p0[0] + p2[0]) * t + (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t2 + (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t3),
          0.5 * (2 * p1[1] + (-p0[1] + p2[1]) * t + (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2 + (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3)]);
      }
    }
    out.push(pts[pts.length - 1]);
    let len = 0; const cum = [0];
    for (let i = 1; i < out.length; i++) { len += Math.hypot(out[i][0] - out[i - 1][0], out[i][1] - out[i - 1][1]); cum.push(len); }
    return { pts: out, cum, len };
  }
  function at(road, d) {
    d = ((d % road.len) + road.len) % road.len;
    let lo = 0, hi = road.cum.length - 1;
    while (hi - lo > 1) { const m = (lo + hi) >> 1; if (road.cum[m] < d) lo = m; else hi = m; }
    const f = (d - road.cum[lo]) / ((road.cum[hi] - road.cum[lo]) || 1);
    const a = road.pts[lo], b = road.pts[hi];
    return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f];
  }

  function build() {
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    W = canvas.clientWidth; H = canvas.clientHeight;
    canvas.width = W * dpr; canvas.height = H * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    hub = [W * rnd(0.62, 0.78), H * rnd(0.25, 0.4)];
    roads = [];
    const n = Math.round(Math.max(6, Math.min(14, (W * H) / 120000)));
    for (let i = 0; i < n; i++) {
      const horiz = i % 2 === 0, pts = [], k = 5;
      for (let j = 0; j <= k; j++) {
        const u = -0.1 + 1.2 * j / k;
        pts.push(horiz ? [u * W, H * (0.08 + 0.84 * (i / n)) + rnd(-H * 0.08, H * 0.08)]
                       : [W * (0.06 + 0.88 * (i / n)) + rnd(-W * 0.06, W * 0.06), u * H]);
      }
      roads.push(catmull(pts));
    }
    units = [];
    const colours = ["#5eead4", "#5eead4", "#38bdf8", "#a5f3fc", "#fbbf24"];
    const m = Math.round(Math.max(5, Math.min(12, (W * H) / 140000)));
    for (let i = 0; i < m; i++) {
      const road = roads[i % roads.length];
      units.push({ id: "U00" + (112 + i), road, d: rnd(0, road.len), v: rnd(18, 48) * (Math.random() < 0.5 ? -1 : 1),
                   c: colours[i % colours.length], pause: 0, trail: [], pos: [0, 0], free: null, blend: 0, hit: -1 });
    }
  }

  // ---- interaction ---------------------------------------------------------------
  const onBackground = (e) => e.target === canvas || e.target === document.body;
  addEventListener("pointermove", (e) => { ptr.x = e.clientX; ptr.y = e.clientY; ptr.seen = true; ptr.last = now; });
  addEventListener("pointerleave", () => { ptr.seen = false; });
  addEventListener("pointerdown", (e) => {
    if (!onBackground(e) || still) return;
    polls.push({ x: e.clientX, y: e.clientY, t: now, id: Math.random() });
    if (polls.length > 6) polls.shift();
  });

  // ---- the fun bits ---------------------------------------------------------------
  function textPoints(text, maxW, maxH) {
    const c = document.createElement("canvas"), x = c.getContext("2d");
    c.width = 400; c.height = 160;
    x.fillStyle = "#000"; x.font = "800 130px system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif";
    x.textAlign = "center"; x.textBaseline = "middle"; x.fillText(text, 200, 84);
    const img = x.getImageData(0, 0, 400, 160).data, pts = [];
    for (let y = 0; y < 160; y += 7) for (let xx = 0; xx < 400; xx += 7) if (img[(y * 400 + xx) * 4 + 3] > 128) pts.push([xx, y]);
    const s = Math.min(maxW / 400, maxH / 160);
    return pts.map(([a, b]) => [(a - 200) * s, (b - 80) * s]);
  }
  function formation(text, caption) {
    if (still) return;
    const card = document.querySelector(".auth-card"), r = card ? card.getBoundingClientRect() : { top: H * 0.4, bottom: H * 0.6 };
    const above = r.top, below = H - r.bottom, room = Math.max(above, below) - 30;
    const cx = W / 2, cy = above >= below ? above / 2 : r.bottom + below / 2;
    const pts = textPoints(text, Math.min(W * 0.6, 440), Math.max(60, Math.min(room, 170)));
    parts = pts.map(([dx, dy], i) => {
      const edge = Math.random() * 4 | 0;
      const sx = edge === 0 ? -20 : edge === 1 ? W + 20 : Math.random() * W, sy = edge < 2 ? Math.random() * H : edge === 2 ? -20 : H + 20;
      return { x: sx, y: sy, vx: 0, vy: 0, tx: cx + dx, ty: cy + dy, delay: Math.random() * 0.6, c: i % 9 === 0 ? "#fbbf24" : "#5eead4" };
    });
    mode = { kind: "formation", t: now, until: now + 7.5, caption };
  }
  function convoy(target, seconds, caption) {
    if (still) return;
    for (const u of units) u.free = u.pos.slice();
    mode = { kind: "convoy", t: now, until: now + seconds, target, caption };
  }

  // Konami code anywhere (not while typing in a field): the fleet forms up and follows you.
  const KONAMI = ["ArrowUp", "ArrowUp", "ArrowDown", "ArrowDown", "ArrowLeft", "ArrowRight", "ArrowLeft", "ArrowRight", "b", "a"];
  let kpos = 0;
  addEventListener("keydown", (e) => {
    if (/INPUT|TEXTAREA|SELECT/.test(document.activeElement && document.activeElement.tagName)) return;
    kpos = (e.key === KONAMI[kpos] || e.key.toLowerCase() === KONAMI[kpos]) ? kpos + 1 : (e.key === KONAMI[0] ? 1 : 0);
    if (kpos === KONAMI.length) { kpos = 0; convoy(null, 14, "Convoy mode. They'll follow you anywhere."); }
  });

  // ---- drawing ---------------------------------------------------------------------
  function frame(ms) {
    now = (ms - t0) / 1000;
    const dt = Math.min(0.05, (ms - (frame.last || ms)) / 1000); frame.last = ms;
    const g = ctx.createLinearGradient(0, 0, W, H);
    g.addColorStop(0, "#071226"); g.addColorStop(1, "#0b3340");
    ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);

    ctx.strokeStyle = "rgba(94,234,212,0.045)"; ctx.lineWidth = 1;
    for (let x = (now * 4) % 48; x < W; x += 48) { ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke(); }
    for (let y = 0; y < H; y += 48) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke(); }

    ctx.strokeStyle = "rgba(148,210,220,0.10)"; ctx.lineWidth = 2;
    for (const r of roads) { ctx.beginPath(); r.pts.forEach((p, i) => i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1])); ctx.stroke(); }

    for (let k = 0; k < 3; k++) {
      const ph = ((now / 4.5) + k / 3) % 1, r = 20 + ph * Math.max(W, H) * 0.55;
      ctx.strokeStyle = `rgba(251,191,36,${0.22 * (1 - ph)})`; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.arc(hub[0], hub[1], r, 0, Math.PI * 2); ctx.stroke();
    }
    ctx.fillStyle = "#fbbf24"; ctx.beginPath(); ctx.arc(hub[0], hub[1], 4, 0, Math.PI * 2); ctx.fill();
    ctx.strokeStyle = "rgba(251,191,36,.5)"; ctx.beginPath(); ctx.moveTo(hub[0], hub[1] - 4); ctx.lineTo(hub[0], hub[1] - 22); ctx.stroke();

    // poll pulses from clicks; units answer as the wave reaches them
    for (let i = polls.length - 1; i >= 0; i--) {
      const p = polls[i], age = now - p.t, r = age * 520;
      if (age > 1.6) { polls.splice(i, 1); continue; }
      ctx.strokeStyle = `rgba(94,234,212,${0.6 * (1 - age / 1.6)})`; ctx.lineWidth = 2.5;
      ctx.beginPath(); ctx.arc(p.x, p.y, r, 0, Math.PI * 2); ctx.stroke();
      for (const u of units) {
        if (u.hit !== p.id && Math.hypot(u.pos[0] - p.x, u.pos[1] - p.y) < r) { u.hit = p.id; replies.push({ u, t: now + 0.15 + Math.random() * 0.25 }); }
      }
    }

    // move units: along roads, or free-flying in convoy mode
    const convoyOn = mode && mode.kind === "convoy";
    units.forEach((u, i) => {
      if (u.pause > 0) u.pause -= dt; else { u.d += u.v * dt; if (Math.random() < dt * 0.04) u.pause = rnd(1.5, 4); }
      const roadPos = at(u.road, u.d);
      if (convoyOn) {
        const lead = i === 0 ? (mode.target || (ptr.seen ? [ptr.x, ptr.y] : [W / 2, H / 2])) : units[i - 1].free;
        const dx = lead[0] - u.free[0], dy = lead[1] - u.free[1], dist = Math.hypot(dx, dy), gap = i === 0 ? 0 : 26;
        if (dist > gap) { const sp = Math.min(dist - gap, (i === 0 ? 520 : 640) * dt); u.free[0] += dx / dist * sp; u.free[1] += dy / dist * sp; }
        u.blend = Math.min(1, u.blend + dt * 2);
      } else if (u.blend > 0) {
        u.blend = Math.max(0, u.blend - dt * 0.8);
      }
      u.pos = u.free && u.blend > 0 ? [roadPos[0] + (u.free[0] - roadPos[0]) * ease(u.blend), roadPos[1] + (u.free[1] - roadPos[1]) * ease(u.blend)] : roadPos;
      const last = u.trail[u.trail.length - 1];
      if (!last || Math.hypot(u.pos[0] - last[0], u.pos[1] - last[1]) > 6) u.trail.push(u.pos.slice());
      if (u.trail.length > (convoyOn ? 70 : 46)) u.trail.shift();
    });

    // pointer: a handheld radio linking to nearby units
    const ptrLive = ptr.seen && now - ptr.last < 6;
    if (ptrLive) {
      for (const u of units) {
        const d = Math.hypot(u.pos[0] - ptr.x, u.pos[1] - ptr.y);
        if (d < 190) {
          ctx.strokeStyle = `rgba(94,234,212,${0.45 * (1 - d / 190)})`; ctx.lineWidth = 1.2; ctx.setLineDash([3, 5]);
          ctx.beginPath(); ctx.moveTo(ptr.x, ptr.y); ctx.lineTo(u.pos[0], u.pos[1]); ctx.stroke(); ctx.setLineDash([]);
        }
      }
      const ph = (now * 1.4) % 1;
      ctx.strokeStyle = `rgba(94,234,212,${0.35 * (1 - ph)})`; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.arc(ptr.x, ptr.y, 6 + ph * 26, 0, Math.PI * 2); ctx.stroke();
    }

    for (const u of units) {
      for (let i = 1; i < u.trail.length; i++) {
        const a = u.trail[i - 1], b = u.trail[i];
        if (Math.hypot(b[0] - a[0], b[1] - a[1]) > 40) continue;
        ctx.strokeStyle = u.c; ctx.globalAlpha = (i / u.trail.length) * 0.55; ctx.lineWidth = 2.5;
        ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke();
      }
      ctx.globalAlpha = 1;
      const p = u.pos, near = ptrLive && Math.hypot(p[0] - ptr.x, p[1] - ptr.y) < 190;
      const glow = ctx.createRadialGradient(p[0], p[1], 0, p[0], p[1], near ? 22 : 16);
      glow.addColorStop(0, u.c + "88"); glow.addColorStop(1, u.c + "00");
      ctx.fillStyle = glow; ctx.beginPath(); ctx.arc(p[0], p[1], near ? 22 : 16, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = u.c; ctx.beginPath(); ctx.arc(p[0], p[1], 3.6, 0, Math.PI * 2); ctx.fill();
      ctx.strokeStyle = "rgba(255,255,255,.85)"; ctx.lineWidth = 1.5; ctx.stroke();
    }

    // poll replies: amber ring and the unit's ID
    for (let i = replies.length - 1; i >= 0; i--) {
      const r = replies[i], age = now - r.t;
      if (age < 0) continue;
      if (age > 1.8) { replies.splice(i, 1); continue; }
      const p = r.u.pos, a = 1 - age / 1.8;
      ctx.strokeStyle = `rgba(251,191,36,${a})`; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.arc(p[0], p[1], 6 + age * 18, 0, Math.PI * 2); ctx.stroke();
      ctx.font = "600 11px ui-monospace, Menlo, Consolas, monospace"; ctx.fillStyle = `rgba(254,243,199,${a})`;
      ctx.fillText(r.u.id, p[0] + 10, p[1] - 10);
    }

    // formation particles
    if (parts.length) {
      const m = mode && mode.kind === "formation" ? mode : null, release = !m || now > m.until - 1.6;
      for (const q of parts) {
        if (now - (m ? m.t : 0) < q.delay && !release) continue;
        if (release) { q.vy += 260 * dt; q.vx *= 0.99; q.life = (q.life ?? 1) - dt * 0.7; }
        else { const ax = (q.tx - q.x) * 26, ay = (q.ty - q.y) * 26; q.vx = (q.vx + ax * dt) * 0.86; q.vy = (q.vy + ay * dt) * 0.86; }
        q.x += q.vx * dt; q.y += q.vy * dt;
        const a = release ? Math.max(0, q.life) : 1;
        ctx.globalAlpha = a; ctx.fillStyle = q.c;
        ctx.beginPath(); ctx.arc(q.x, q.y, 2.6, 0, Math.PI * 2); ctx.fill();
      }
      ctx.globalAlpha = 1;
      if (release && parts.every((q) => (q.life ?? 1) <= 0)) parts = [];
    }

    // caption for the fun bits
    if (mode && mode.caption) {
      const a = Math.min(1, (now - mode.t) * 2, (mode.until - now) * 2);
      if (a > 0) {
        ctx.font = "600 14px system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"; ctx.textAlign = "center";
        ctx.fillStyle = `rgba(254,243,199,${a})`;
        const words = mode.caption.split(" "), lines = [""];
        for (const w of words) {
          const tryLine = (lines[lines.length - 1] + " " + w).trim();
          if (ctx.measureText(tryLine).width > W - 32 && lines[lines.length - 1]) lines.push(w); else lines[lines.length - 1] = tryLine;
        }
        lines.forEach((l, i) => ctx.fillText(l, W / 2, H - 28 - (lines.length - 1 - i) * 19));
        ctx.textAlign = "start";
      }
    }
    if (mode && now > mode.until) mode = null;

    if (!still) requestAnimationFrame(frame);
  }
  const ease = (x) => x * x * (3 - 2 * x);

  build();
  if (still) { for (let i = 0; i < 240; i++) frame(t0 + i * 33); } else requestAnimationFrame(frame);
  let rt; addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => { build(); if (still) frame(performance.now()); }, 150); });

  // ---- the logo is a straight key -------------------------------------------------------
  // Press for a dit, hold for a dah. Some calls get an answer.
  function morseKey(el) {
    if (!el || still) return;
    let audio = null, osc = null, gain = null, down = 0, sym = "", letters = [], gapTimer = null;
    const CODE = { "-.-.": "C", "--.-": "Q", "...": "S", "---": "O", "...--": "3", "--...": "7", "-.-": "K", "-..": "D", ".": "E" };
    const toneOn = () => {
      try {
        audio = audio || new (window.AudioContext || window.webkitAudioContext)();
        if (audio.state === "suspended") audio.resume();
        osc = audio.createOscillator(); gain = audio.createGain();
        osc.frequency.value = 680; osc.type = "sine";
        gain.gain.setValueAtTime(0, audio.currentTime); gain.gain.linearRampToValueAtTime(0.16, audio.currentTime + 0.008);
        osc.connect(gain).connect(audio.destination); osc.start();
      } catch (e) { /* no audio */ }
    };
    const toneOff = () => { try { gain.gain.linearRampToValueAtTime(0, audio.currentTime + 0.01); osc.stop(audio.currentTime + 0.02); } catch (e) { /* none */ } };
    const finishLetter = () => {
      if (sym) { letters.push(CODE[sym] || "?"); sym = ""; }
      const word = letters.join("").slice(-3);
      if (/CQ$/.test(word)) { letters = []; formation("73", "CQ heard. 73 de KenTrackIt ✦ best regards from the whole fleet"); }
      else if (/SOS$/.test(word)) {
        letters = [];
        const c = (el.closest(".auth-card") || el).getBoundingClientRect();
        convoy([c.left + c.width / 2, Math.max(24, c.top - 26)], 9, "SOS received. Every unit is on its way.");
      }
      if (letters.length > 12) letters = letters.slice(-6);
    };
    el.setAttribute("draggable", "false");
    el.style.touchAction = "none"; el.style.userSelect = "none"; el.style.cursor = "pointer";
    el.addEventListener("contextmenu", (e) => e.preventDefault());
    el.addEventListener("pointerdown", (e) => {
      e.preventDefault(); clearTimeout(gapTimer); down = performance.now(); toneOn();
      el.classList.add("keyed");
    });
    const up = () => {
      if (!down) return;
      toneOff(); el.classList.remove("keyed");
      sym += (performance.now() - down) < 220 ? "." : "-"; down = 0;
      clearTimeout(gapTimer); gapTimer = setTimeout(finishLetter, 600);
    };
    el.addEventListener("pointerup", up); el.addEventListener("pointerleave", up); el.addEventListener("pointercancel", up);
  }

  try { console.log("%c📻 CQ CQ de KenTrackIt", "color:#5eead4;font:600 14px system-ui",
                    "\nThat logo looks a lot like a straight key. Some calls get an answer."); } catch (e) { /* no console */ }
  return { morseKey, formation, convoy };
};
