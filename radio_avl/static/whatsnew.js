"use strict";
/* What's New: after an update, each user sees what changed once, then not again until the next one.
   KTWhatsNew.check()   show it if there's something this user hasn't seen
   KTWhatsNew.show()    show the whole history (from My account) */
window.KTWhatsNew = (() => {
  const CSS = `
  .wn-back { position: fixed; inset: 0; z-index: 5000; background: rgba(4, 12, 26, .55); backdrop-filter: blur(3px);
    display: flex; align-items: center; justify-content: center; padding: 16px; animation: wnfade .25s ease-out; }
  .wn { width: 100%; max-width: 560px; max-height: calc(100vh - 32px); display: flex; flex-direction: column; overflow: hidden;
    background: var(--panel, #fff); color: var(--ink, #111); border-radius: 18px; box-shadow: 0 24px 70px rgba(0,0,0,.4);
    animation: wnpop .35s cubic-bezier(.2,.9,.25,1.15); font-family: var(--font, system-ui, sans-serif); }
  .wn-head, .wn-foot { flex-shrink: 0; }
  .wn-head { position: relative; padding: 22px 24px 18px; color: #fff; overflow: hidden;
    background: radial-gradient(500px 200px at 85% -40%, rgba(94,234,212,.55), transparent 60%), linear-gradient(135deg, #0b1b33, #0f4c5c); }
  .wn-head::after { content: "✦"; position: absolute; right: 22px; top: 14px; font-size: 54px; opacity: .14; transform: rotate(12deg); }
  .wn-kicker { display: inline-flex; align-items: center; gap: 6px; font-size: 11.5px; font-weight: 700; letter-spacing: .08em;
    text-transform: uppercase; color: #5eead4; }
  .wn-head h2 { text-transform: none; margin: 6px 0 2px; font-size: 22px; font-weight: 750; letter-spacing: -.01em; color: #fff; }
  .wn-head .wn-sub { font-size: 13px; color: rgba(214,232,240,.75); }
  .wn-body { overflow-y: auto; padding: 6px 24px 8px; }
  .wn-rel + .wn-rel { border-top: 1px solid var(--line, #e5e7eb); margin-top: 6px; }
  .wn-rel h3 { text-transform: none; letter-spacing: 0; margin: 16px 0 4px; font-size: 13px; font-weight: 650; color: var(--muted, #6b7280); display: flex; gap: 8px; align-items: baseline; }
  .wn-rel h3 .v { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12px; padding: 1px 7px; border-radius: 999px;
    background: color-mix(in srgb, var(--accent, #2563eb) 12%, transparent); color: var(--accent, #2563eb); }
  .wn-item { display: grid; grid-template-columns: 40px 1fr; gap: 12px; padding: 12px 0; opacity: 0; transform: translateY(6px);
    animation: wnin .4s ease-out forwards; }
  .wn-item + .wn-item { border-top: 1px dashed var(--line, #e5e7eb); }
  .wn-ico { width: 40px; height: 40px; border-radius: 11px; display: grid; place-items: center; font-size: 20px;
    background: color-mix(in srgb, var(--accent, #2563eb) 10%, transparent); }
  .wn-item b { display: block; font-size: 15px; margin-bottom: 2px; }
  .wn-item p { margin: 0; font-size: 13.5px; line-height: 1.45; color: var(--ink2, inherit); opacity: .85; }
  .wn-item a { display: inline-block; margin-top: 6px; font-size: 13px; font-weight: 600; color: var(--accent, #2563eb); text-decoration: none; }
  .wn-item a:hover { text-decoration: underline; }
  .wn-foot { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 14px 24px 18px;
    border-top: 1px solid var(--line, #e5e7eb); }
  .wn-foot span { font-size: 12px; color: var(--muted, #6b7280); }
  .wn-foot button { flex-shrink: 0; white-space: nowrap; font: inherit; font-weight: 650; font-size: 14px; padding: 9px 20px; border: 0; border-radius: 10px; cursor: pointer;
    color: #062028; background: linear-gradient(135deg, #5eead4, #22d3ee); box-shadow: 0 6px 18px rgba(34,211,238,.3); }
  .wn-foot button:hover { filter: brightness(1.05); }
  .wn-x { position: absolute; top: 10px; right: 10px; z-index: 1; width: 32px; height: 32px; border: 0; border-radius: 8px; cursor: pointer;
    background: rgba(255,255,255,.1); color: #fff; font-size: 18px; line-height: 1; }
  .wn-x:hover { background: rgba(255,255,255,.2); }
  @keyframes wnfade { from { opacity: 0; } }
  @keyframes wnpop { from { opacity: 0; transform: translateY(14px) scale(.96); } }
  @keyframes wnin { to { opacity: 1; transform: none; } }
  @media (prefers-reduced-motion: reduce) { .wn-back, .wn, .wn-item { animation: none; opacity: 1; transform: none; } }
  @media (max-width: 480px) { .wn-head, .wn-body, .wn-foot { padding-left: 16px; padding-right: 16px; } }`;

  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const fmtDate = (d) => { try { return new Date(d + "T12:00").toLocaleDateString([], { day: "numeric", month: "long", year: "numeric" }); } catch (e) { return d; } };
  let open = false;

  function render(data, history) {
    if (open || !data.releases.length) return;
    open = true;
    if (!document.getElementById("wn-css")) { const st = el("style"); st.id = "wn-css"; st.textContent = CSS; document.head.appendChild(st); }
    const back = el("div", "wn-back");
    const box = el("div", "wn"); box.setAttribute("role", "dialog"); box.setAttribute("aria-modal", "true"); box.setAttribute("aria-labelledby", "wn-title");
    const head = el("div", "wn-head");
    const x = el("button", "wn-x", "×"); x.type = "button"; x.setAttribute("aria-label", "Close");
    const first = data.releases[0];
    const title = el("h2", null, history ? "What's new" : first.title); title.id = "wn-title";
    head.append(x, el("div", "wn-kicker", history ? "✦ Release history" : `✦ New in version ${first.version}`), title,
      el("div", "wn-sub", history ? "Everything that's changed, newest first." : fmtDate(first.date)));
    const body = el("div", "wn-body");
    let n = 0;
    data.releases.forEach((r, ri) => {
      const sec = el("div", "wn-rel");
      if (history || data.releases.length > 1 || ri > 0) {
        const h = el("h3"); h.append(el("span", "v", "v" + r.version), document.createTextNode(`${r.title} · ${fmtDate(r.date)}`)); sec.appendChild(h);
      }
      for (const it of r.items) {
        const row = el("div", "wn-item"); row.style.animationDelay = (0.12 + 0.07 * n++) + "s";
        const txt = el("div"); txt.append(el("b", null, it.title), el("p", null, it.text));
        if (it.link && /^\/[^/]/.test(it.link)) { const a = el("a", null, (it.link_text || "Open") + " →"); a.href = it.link; txt.appendChild(a); }
        row.append(el("div", "wn-ico", it.icon || "✦"), txt);
        sec.appendChild(row);
      }
      body.appendChild(sec);
    });
    const foot = el("div", "wn-foot");
    const ok = el("button", null, history ? "Close" : "Got it"); ok.type = "button";
    foot.append(el("span", null, history ? "" : "You won't see this again until the next update."), ok);
    box.append(head, body, foot); back.appendChild(box); document.body.appendChild(back);
    const prevFocus = document.activeElement; ok.focus();

    const close = () => {
      if (!open) return;
      open = false; back.remove(); document.removeEventListener("keydown", key, true);
      if (prevFocus && prevFocus.focus) prevFocus.focus();
      if (!history) fetch("/api/whatsnew/seen", { method: "POST", keepalive: true, headers: { "Content-Type": "application/json" },
                                                  body: JSON.stringify({ version: data.latest }) }).catch(() => {});
    };
    const key = (e) => {
      if (e.key === "Escape") { e.preventDefault(); close(); }
      if (e.key === "Tab") {                       // keep focus inside the dialog
        const f = [...box.querySelectorAll("button, a[href]")]; const i = f.indexOf(document.activeElement);
        e.preventDefault(); f[(i + (e.shiftKey ? f.length - 1 : 1)) % f.length].focus();
      }
    };
    document.addEventListener("keydown", key, true);
    x.onclick = ok.onclick = close;
    back.addEventListener("click", (e) => { if (e.target === back) close(); });
    for (const a of body.querySelectorAll("a")) a.addEventListener("click", close);
  }

  async function get(all) {
    const r = await fetch("/api/whatsnew" + (all ? "?all=1" : ""));
    if (!r.ok) throw new Error("What's new: " + r.status);
    return r.json();
  }
  return {
    check: (delay = 900) => setTimeout(() => get(false).then((d) => render(d, false)).catch(() => {}), delay),
    show: () => get(true).then((d) => render(d, true)).catch(() => {}),
  };
})();
