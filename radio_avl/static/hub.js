"use strict";
(async function () {
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const api = async (path, opts = {}) => {
    const r = await fetch("/hub/api" + path, opts);
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || `Error ${r.status}`);
    return j;
  };
  const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  let toastTimer;
  const toast = (msg, err) => { const t = $("toast"); t.textContent = msg; t.className = "toast" + (err ? " err" : ""); t.hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), err ? 6000 : 3500); };
  const dur = (s) => s < 60 ? s + " s" : s < 3600 ? Math.floor(s / 60) + " min" : s < 86400 ? Math.floor(s / 3600) + " h" : Math.floor(s / 86400) + " d";
  const ago = (t) => t ? dur(Math.max(0, Math.round(Date.now() / 1000 - t))) + " ago" : "never";
  const when = (t) => t ? new Date(t * 1000).toLocaleString([], { hour12: false }) : "–";
  const bytes = (n) => n < 1048576 ? (n / 1024).toFixed(0) + " KB" : (n / 1048576).toFixed(1) + " MB";

  // ---- signed out -------------------------------------------------------------------------
  let STATE = await api("/state");
  if (!STATE.user) {
    if (window.KTWallpaper) KTWallpaper($("wall"));
    $("signin").hidden = false;
    const setup = STATE.setup;
    $("setup-intro").hidden = !setup; $("f-name-wrap").hidden = !setup;
    $("f-btn").textContent = setup ? "Create account" : "Sign in";
    if (setup) $("f-pass").autocomplete = "new-password";
    $("f").onsubmit = async (ev) => {
      ev.preventDefault(); $("f-err").hidden = true; $("f-btn").disabled = true;
      try {
        const body = { username: $("f-user").value.trim(), password: $("f-pass").value };
        if (setup) await post("/setup", Object.assign({ display_name: $("f-name").value.trim() }, body));
        await post("/login", body);
        location.reload();
      } catch (e) { $("f-err").textContent = e.message; $("f-err").hidden = false; $("f-btn").disabled = false; }
    };
    return;
  }

  // ---- signed in ------------------------------------------------------------------------------
  document.body.className = "settings hub-in";
  $("main").hidden = false;
  $("h-title").textContent = STATE.title;
  document.title = STATE.title + " service";
  $("h-me").textContent = STATE.user.display_name || STATE.user.username;
  $("signout").onclick = async () => { await post("/logout"); location.reload(); };

  // companies
  let open = null;                   // slug whose manage form is open
  async function loadCompanies() {
    const { companies } = await api("/companies");
    const list = $("co-list"); list.replaceChildren();
    if (!companies.length) { list.appendChild(el("div", "co-empty", "No companies yet. Add the first one above.")); return; }
    for (const c of companies) list.appendChild(companyRow(c));
  }

  function gatewayBadge(c) {
    const g = c.gateway || {};
    const b = el("span", "gw");
    let text;
    if (!c.enabled) { text = "Disabled"; }
    else if (!g.last) { text = "Gateway not connected yet"; }
    else if (!g.online) { b.classList.add("down"); text = `Gateway offline · last ${ago(g.last)}`; }
    else if (!(g.link || {}).up) { b.classList.add("radio-off"); text = "Gateway online · radio not connected"; }
    else { b.classList.add("on"); text = "Gateway online" + (g.backlog ? ` · catching up (${g.backlog})` : ""); }
    b.append(el("span", "led"), el("span", null, text));
    return b;
  }

  function companyRow(c) {
    const box = el("div", "co" + (c.enabled ? "" : " off"));
    const top = el("div", "co-top");
    top.append(el("span", "co-name", c.name), el("span", "co-slug", c.slug), gatewayBadge(c));
    const acts = el("div", "co-acts");
    const supp = el("button", null, "Open");
    supp.title = "Open this company's KenTrackIt as ASI support (4 hours, recorded in their audit log)";
    supp.disabled = !c.enabled;
    supp.onclick = async () => {
      if (!confirm(`Open ${c.name}'s KenTrackIt as ASI support? This is recorded in their audit log.`)) return;
      const w = window.open("about:blank", "_blank");     // opened now, or the browser blocks it as a pop-up
      try { await post(`/companies/${c.slug}/support`); if (w) w.location = "/"; else location.href = "/"; }
      catch (e) { if (w) w.close(); toast(e.message, true); }
    };
    const man = el("button", null, open === c.slug ? "Close" : "Manage");
    man.onclick = () => { open = open === c.slug ? null : c.slug; loadCompanies(); };
    acts.append(supp, man);
    top.appendChild(acts);
    box.appendChild(top);

    const st = el("div", "co-stats");
    const stat = (label, v, cls) => { const s = el("span", cls); s.append(label + " ", el("b", null, v)); st.appendChild(s); };
    if (c.enabled) {
      stat("Units", String(c.units ?? "–"));
      stat("Users", String(c.users ?? "–"));
      stat("Positions", (c.positions ?? 0).toLocaleString());
      stat("Last radio data", ago(c.last_frame));
      if (c.open_alerts) stat("Open alerts", String(c.open_alerts), "alerts");
    }
    if (c.domains.length) stat("Web address", c.domains.join(", "));
    box.appendChild(st);
    if (open === c.slug) box.appendChild(manageForm(c));
    return box;
  }

  function manageForm(c) {
    const f = el("form", "sub-form");
    f.innerHTML = `
      <label>Company name <input name="name" maxlength="80" required></label>
      <label>Its own web addresses (optional) <input name="domains" placeholder="sdc.track.asiltd.co.nz">
        <span class="hint">Point the address at this service (e.g. another Cloudflare tunnel hostname). People on it can only sign in to this company. Separate several with commas.</span></label>
      <label>Notes <input name="notes" maxlength="1000" placeholder="contract, contacts, radios…"></label>
      <label class="check"><input type="checkbox" name="enabled"> Enabled (unticked: its users can't sign in and its gateway is refused; nothing is deleted)</label>
      <div class="status-grid">
        <div><span class="k">Gateway key</span><span class="gk"></span></div>
        <div><span class="k">Gateway</span><span class="gi"></span></div>
        <div><span class="k">Created</span><span class="cr"></span></div>
        <div><span class="k">Data folder</span><span class="df"></span></div>
      </div>
      <div class="actions"><button type="button" class="nk danger">New gateway key</button><button type="submit">Save</button></div>`;
    f.name.value = c.name; f.domains.value = c.domains.join(", "); f.notes.value = c.notes || ""; f.enabled.checked = c.enabled;
    const g = c.gateway || {};
    f.querySelector(".gk").textContent = c.token_hint ? `ends in …${c.token_hint}` : "–";
    f.querySelector(".gi").textContent = g.last ? `${g.version || "?"} from ${g.ip || "?"}, ${ago(g.last)}` + (g.port ? ` (${g.port})` : "") : "never connected";
    f.querySelector(".cr").textContent = when(c.created);
    f.querySelector(".df").textContent = "companies/" + c.slug;
    f.onsubmit = async (ev) => {
      ev.preventDefault();
      if (!f.enabled.checked && c.enabled && !confirm(`Disable ${c.name}? Its users are signed out and its gateway is refused until you enable it again.`)) return;
      try { await post(`/companies/${c.slug}`, { name: f.name.value, domains: f.domains.value, notes: f.notes.value, enabled: f.enabled.checked });
            toast("Saved"); open = null; loadCompanies(); loadActivity(); }
      catch (e) { toast(e.message, true); }
    };
    f.querySelector(".nk").onclick = async () => {
      if (!confirm(`Issue a new gateway key for ${c.name}? The current key stops working straight away, so the site's Pi needs the new one.`)) return;
      try { const r = await post(`/companies/${c.slug}/token`); showKey(c.name, r.token); loadActivity(); }
      catch (e) { toast(e.message, true); }
    };
    return f;
  }

  function showKey(name, token) {
    $("kb-title").textContent = `Gateway key for ${name}`;
    $("kb-url").textContent = location.origin;
    $("kb-key").textContent = token;
    $("keybox").hidden = false;
    $("keybox").scrollIntoView({ behavior: "smooth", block: "center" });
  }
  $("kb-done").onclick = () => { $("keybox").hidden = true; $("kb-key").textContent = ""; };
  for (const b of document.querySelectorAll("[data-copy]")) {
    b.onclick = async () => {
      try { await navigator.clipboard.writeText($(b.dataset.copy).textContent); toast("Copied"); }
      catch (e) { toast("Select it and copy by hand", true); }
    };
  }

  // add a company
  $("co-add-btn").onclick = () => { $("co-add").hidden = false; $("co-add-btn").hidden = true; $("ca-name").focus(); };
  $("ca-cancel").onclick = () => { $("co-add").reset(); $("co-add").hidden = true; $("co-add-btn").hidden = false; };
  let slugTouched = false;
  $("ca-slug").oninput = () => { slugTouched = true; };
  $("ca-name").oninput = () => {
    if (slugTouched) return;
    $("ca-slug").value = $("ca-name").value.toLowerCase().replace(/&/g, " and ").replace(/[^a-z0-9]+/g, "-").replace(/^[^a-z]+|-+$/g, "").slice(0, 30);
  };
  $("co-add").onsubmit = async (ev) => {
    ev.preventDefault();
    const name = $("ca-name").value.trim();
    try {
      const r = await post("/companies", { name, slug: $("ca-slug").value.trim(),
        admin: { display_name: $("ca-aname").value.trim(), username: $("ca-auser").value.trim(), password: $("ca-apass").value } });
      $("co-add").reset(); slugTouched = false; $("co-add").hidden = true; $("co-add-btn").hidden = false;
      showKey(name, r.token); loadCompanies(); loadActivity();
    } catch (e) { toast(e.message, true); }
  };

  // phone app
  async function loadApk() {
    const i = await api("/app");
    $("apk").textContent = i.version_name ? `Version ${i.version_name} (build ${i.version_code}), ${bytes(i.size)}, uploaded ${when(i.uploaded)}` : "No app uploaded yet";
    $("apk-rm").hidden = !i.version_name;
  }
  $("apk-up").onclick = async () => {
    const f = $("apk-file").files[0];
    if (!f) return toast("Choose the KenTrackIt.apk file first", true);
    $("apk-up").disabled = true; $("apk-up").textContent = "Uploading…";
    try { const i = await api("/app/upload", { method: "POST", headers: { "Content-Type": "application/vnd.android.package-archive" }, body: f });
          toast(`Uploaded ${i.version_name}. Phones will be offered the update.`); $("apk-file").value = ""; loadApk(); loadActivity(); }
    catch (e) { toast(e.message, true); }
    $("apk-up").disabled = false; $("apk-up").textContent = "Upload";
  };
  $("apk-rm").onclick = async () => {
    if (!confirm("Remove the app installer? Phones keep the app they have.")) return;
    try { await api("/app", { method: "DELETE" }); loadApk(); } catch (e) { toast(e.message, true); }
  };

  // service settings
  async function loadSettings() {
    const s = await api("/settings");
    $("svc-title").value = s.title; $("svc-tiles").value = s.tiles.source; $("svc-sat").value = s.tiles.satellite;
    $("svc-linz").value = ""; $("svc-linz").placeholder = s.tiles.linz_key_set ? "key saved; enter a new one to replace" : "from basemaps.linz.govt.nz";
  }
  $("svc").onsubmit = async (ev) => {
    ev.preventDefault();
    const tiles = { source: $("svc-tiles").value, satellite: $("svc-sat").value };
    if ($("svc-linz").value.trim()) tiles.linz_key = $("svc-linz").value.trim();
    try { await post("/settings", { web: { title: $("svc-title").value.trim() }, tiles }); toast("Saved"); loadSettings(); loadActivity();
          $("h-title").textContent = $("svc-title").value.trim(); }
    catch (e) { toast(e.message, true); }
  };

  // ASI administrators
  let editingAdmin = null;
  async function loadAdmins() {
    const { admins } = await api("/admins");
    const tb = $("ad-rows"); tb.replaceChildren();
    for (const u of admins) {
      const tr = el("tr", u.enabled ? "" : "off");
      const nm = el("td"); nm.append(el("div", "uname", u.display_name || u.username), el("div", "usub", u.username + (u.id === STATE.user.id ? " · you" : "")));
      const last = el("td", null, u.last_login ? "Signed in " + ago(u.last_login) : "Never signed in");
      const act = el("td", "act");
      const e = el("button", null, "Edit");
      e.onclick = () => { editingAdmin = u; $("ad-form").hidden = false; $("ad-name").value = u.display_name; $("ad-user").value = u.username;
                          $("ad-pass").value = ""; $("ad-pass").placeholder = "leave blank to keep"; $("ad-pass").required = false; };
      act.appendChild(e);
      if (u.id !== STATE.user.id) {
        const d = el("button", "danger", "Remove");
        d.onclick = async () => { if (!confirm(`Remove ${u.username}?`)) return;
          try { await api("/admins/" + u.id, { method: "DELETE" }); loadAdmins(); loadActivity(); } catch (err) { toast(err.message, true); } };
        act.append(" ", d);
      }
      tr.append(nm, last, act); tb.appendChild(tr);
    }
  }
  $("ad-add-btn").onclick = () => { editingAdmin = null; $("ad-form").reset(); $("ad-form").hidden = false; $("ad-pass").required = true; $("ad-pass").placeholder = "at least 8 characters"; $("ad-name").focus(); };
  $("ad-cancel").onclick = () => { $("ad-form").hidden = true; };
  $("ad-form").onsubmit = async (ev) => {
    ev.preventDefault();
    const body = { display_name: $("ad-name").value.trim(), username: $("ad-user").value.trim() };
    if (editingAdmin) body.id = editingAdmin.id;
    if ($("ad-pass").value) body.password = $("ad-pass").value;
    try { await post("/admins", body); $("ad-form").hidden = true; toast("Saved"); loadAdmins(); loadActivity(); }
    catch (e) { toast(e.message, true); }
  };

  async function loadActivity() {
    const rows = await api("/activity");
    const ul = $("activity"); ul.replaceChildren();
    for (const r of rows.slice(0, 60)) {
      const li = el("li"); li.append(el("time", null, when(r.rx_time)), el("span", null, r.detail)); ul.appendChild(li);
    }
  }

  loadCompanies(); loadApk(); loadSettings(); loadAdmins(); loadActivity();
  setInterval(() => { if (!open && $("co-add").hidden) loadCompanies().catch(() => {}); }, 10000);
})();
