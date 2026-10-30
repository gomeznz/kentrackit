"use strict";
// Animated "live dispatch" background for the sign-in pages: units gliding along a faint road
// network leaving trails, with radio pulses from the repeater. Drawn on a canvas in the browser,
// so it costs the Pi nothing and works offline. Holds still if the user prefers reduced motion.
window.KTWallpaper = function (canvas) {
  const ctx = canvas.getContext("2d");
  const still = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  let W, H, dpr, roads = [], units = [], hub, t0 = performance.now();
  const rnd = (a, b) => a + Math.random() * (b - a);

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
      units.push({ road, d: rnd(0, road.len), v: rnd(18, 48) * (Math.random() < 0.5 ? -1 : 1), c: colours[i % colours.length],
                   pause: 0, trail: [] });
    }
  }

  function frame(now) {
    const t = (now - t0) / 1000, dt = Math.min(0.05, (now - (frame.last || now)) / 1000); frame.last = now;
    const g = ctx.createLinearGradient(0, 0, W, H);
    g.addColorStop(0, "#071226"); g.addColorStop(1, "#0b3340");
    ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);

    // faint grid
    ctx.strokeStyle = "rgba(94,234,212,0.045)"; ctx.lineWidth = 1;
    for (let x = (t * 4) % 48; x < W; x += 48) { ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke(); }
    for (let y = 0; y < H; y += 48) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke(); }

    // roads
    ctx.strokeStyle = "rgba(148,210,220,0.10)"; ctx.lineWidth = 2;
    for (const r of roads) { ctx.beginPath(); r.pts.forEach((p, i) => i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1])); ctx.stroke(); }

    // repeater pulses
    for (let k = 0; k < 3; k++) {
      const ph = ((t / 4.5) + k / 3) % 1, r = 20 + ph * Math.max(W, H) * 0.55;
      ctx.strokeStyle = `rgba(251,191,36,${0.22 * (1 - ph)})`; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.arc(hub[0], hub[1], r, 0, Math.PI * 2); ctx.stroke();
    }
    ctx.fillStyle = "#fbbf24"; ctx.beginPath(); ctx.arc(hub[0], hub[1], 4, 0, Math.PI * 2); ctx.fill();
    ctx.strokeStyle = "rgba(251,191,36,.5)"; ctx.beginPath(); ctx.moveTo(hub[0], hub[1] - 4); ctx.lineTo(hub[0], hub[1] - 22); ctx.stroke();

    // units with trails
    for (const u of units) {
      if (u.pause > 0) u.pause -= dt; else { u.d += u.v * dt; if (Math.random() < dt * 0.04) u.pause = rnd(1.5, 4); }
      const p = at(u.road, u.d);
      if (!u.trail.length || Math.hypot(p[0] - u.trail[u.trail.length - 1][0], p[1] - u.trail[u.trail.length - 1][1]) > 6) u.trail.push(p);
      if (u.trail.length > 46) u.trail.shift();
      for (let i = 1; i < u.trail.length; i++) {
        const a = u.trail[i - 1], b = u.trail[i];
        if (Math.hypot(b[0] - a[0], b[1] - a[1]) > 40) continue;      // wrapped around the road
        ctx.strokeStyle = u.c; ctx.globalAlpha = (i / u.trail.length) * 0.55; ctx.lineWidth = 2.5;
        ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke();
      }
      ctx.globalAlpha = 1;
      const glow = ctx.createRadialGradient(p[0], p[1], 0, p[0], p[1], 16);
      glow.addColorStop(0, u.c + "88"); glow.addColorStop(1, u.c + "00");
      ctx.fillStyle = glow; ctx.beginPath(); ctx.arc(p[0], p[1], 16, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = u.c; ctx.beginPath(); ctx.arc(p[0], p[1], 3.6, 0, Math.PI * 2); ctx.fill();
      ctx.strokeStyle = "rgba(255,255,255,.85)"; ctx.lineWidth = 1.5; ctx.stroke();
    }
    if (!still) requestAnimationFrame(frame);
  }

  build();
  if (still) { for (let i = 0; i < 240; i++) frame(t0 + i * 33); } else requestAnimationFrame(frame);
  let rt; addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => { build(); if (still) frame(performance.now()); }, 150); });
};
