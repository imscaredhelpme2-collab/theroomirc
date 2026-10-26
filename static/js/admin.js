/* admin.js -- user management and room settings. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const tbody = document.querySelector("#usersTable tbody");
  let state = null;

  function say(el, text, ok) { el.textContent = text; el.className = ok ? "ok" : "bad"; }
  function ago(iso) {
    if (!iso) return "never";
    const s = (Date.now() - new Date(iso).getTime()) / 1000;
    if (s < 90) return "just now";
    if (s < 5400) return Math.round(s / 60) + " min ago";
    if (s < 129600) return Math.round(s / 3600) + " h ago";
    return new Date(iso).toLocaleDateString();
  }
  function tag(text, cls) { const t = document.createElement("span"); t.className = "tag " + cls; t.textContent = text; return t; }

  async function act(u, action, body, okText) {
    const r = await api("POST", "/api/admin/users/" + u.id + "/" + action, body || {});
    if (!r.ok) alert(r.data.error || "That did not work.");
    await load();
  }

  function row(u) {
    const tr = document.createElement("tr");
    const who = document.createElement("td");
    const b = document.createElement("b"); b.textContent = u.display_name;
    const sub = document.createElement("span"); sub.className = "sub"; sub.textContent = "@" + u.username;
    who.append(b, sub);
    if (u.is_disabled && u.disabled_message) { const m = document.createElement("span"); m.className = "sub"; m.textContent = "“" + u.disabled_message + "”"; who.appendChild(m); }
    const st = document.createElement("td");
    if (u.is_admin) st.appendChild(tag("admin", "adm"));
    st.appendChild(u.is_disabled ? tag("disabled", "dis") : u.online ? tag("online", "on") : tag("offline", "off"));
    if (!u.has_password) st.appendChild(tag("no password", "off"));
    const n = document.createElement("td");
    n.textContent = u.irc ? u.irc : "not set up";
    if (u.irc) { const sub = document.createElement("span"); sub.className = "sub"; sub.textContent = u.irc_state; n.appendChild(sub); }
    const seen = document.createElement("td"); seen.textContent = u.online ? "now" : ago(u.last_seen);
    const acts = document.createElement("td"); const box = document.createElement("div"); box.className = "acts";
    const btn = (label, fn, cls) => { const x = document.createElement("button"); x.type = "button"; x.textContent = label; if (cls) x.className = cls; x.addEventListener("click", fn); box.appendChild(x); };
    const self = u.id === state.me;
    if (u.is_disabled) btn("Enable", () => act(u, "enable"));
    else if (!self) btn("Disable…", () => {
      const msg = prompt("Message the user will see (optional):", "Your account has been disabled by the admin.");
      if (msg !== null) act(u, "disable", { message: msg });
    });
    btn(u.has_password ? "Set password…" : "Give password…", () => {
      const pw = prompt("New password for " + u.display_name + ":");
      if (pw) act(u, "password", { password: pw, enable: !u.has_password });
    });
    btn("Rename…", () => { const n2 = prompt("New display name:", u.display_name); if (n2) act(u, "rename", { display_name: n2 }); });
    if (!self) btn(u.is_admin ? "Remove admin" : "Make admin", () => act(u, "admin", { value: !u.is_admin }));
    if (!self && u.has_password) btn("Sign out", () => act(u, "logout"));
    if (!self) btn("Delete…", async () => {
      if (!confirm("Delete " + u.display_name + "? Their saved IRC login and chat logs are deleted too. This cannot be undone.")) return;
      const r = await api("DELETE", "/api/admin/users/" + u.id, {});
      if (!r.ok) alert(r.data.error || "Could not delete."); await load();
    }, "danger");
    acts.appendChild(box);
    tr.append(who, st, n, seen, acts);
    return tr;
  }

  function render() {
    tbody.textContent = "";
    for (const u of state.users) tbody.appendChild(row(u));
    $("userCount").textContent = "(" + state.users.length + ")";
  }
  let filled = false;
  async function load() {
    const r = await api("GET", "/api/admin/users");
    if (!r.ok) return;
    state = r.data; render();
    if (!filled) {
      filled = true;
      $("roomName").value = state.settings.room_name;
      $("inviteCode").value = state.settings.invite_code; $("regOpen").checked = state.settings.registration_open === "1";
    }
  }

  $("saveRoom").addEventListener("click", async () => {
    const r = await api("POST", "/api/admin/settings", { room_name: $("roomName").value,
      invite_code: $("inviteCode").value, registration_open: $("regOpen").checked });
    say($("roomMsg"), r.ok ? "Saved." : (r.data.error || "Could not save."), r.ok);
  });
  /* ---------------- helpers ---------------- */
  const mk = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const btn = (label, fn, cls) => { const b = mk("button", cls, label); b.type = "button"; b.addEventListener("click", fn); return b; };
  const when = (iso) => new Date(iso).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  const fail = (r, fallback) => alert((r.data && r.data.error) || fallback);

  /* ---------------- presets + event look ---------------- */
  let presetState = null, presets = [];
  function renderEventSelect() {
    const sel = $("eventPreset"); sel.textContent = "";
    for (const p of presets) { const o = mk("option", null, p.name); o.value = p.id; sel.appendChild(o); }
  }
  function renderPresets() {
    const list = $("presetList"); list.textContent = "";
    presets.forEach((p, idx) => {
      const c = mk("div", "preset-edit"), g = mk("div", "grid2");
      const field = (label, el) => { const l = mk("label", null, label); l.appendChild(el); g.appendChild(l); return el; };
      const id = field("Id", mk("input")); id.value = p.id; id.maxLength = 24; id.addEventListener("input", () => { p.id = id.value; });
      const nm = field("Name", mk("input")); nm.value = p.name; nm.maxLength = 30; nm.addEventListener("input", () => { p.name = nm.value; });
      const ds = field("Description", mk("input")); ds.value = p.description || ""; ds.maxLength = 120; ds.addEventListener("input", () => { p.description = ds.value; });
      const th = field("Theme", mk("select"));
      for (const t of presetState.themes) { const o = mk("option", null, t.name); o.value = t.id; th.appendChild(o); }
      th.value = p.theme; th.addEventListener("change", () => { p.theme = th.value; });
      const col = field("Name colour", mk("input")); col.type = "color"; col.value = p.name_color || "#7ab8ff"; col.addEventListener("input", () => { p.name_color = col.value; });
      c.appendChild(g);
      const gims = mk("div", "gims");
      for (const gm of presetState.gimmicks) {
        const l = mk("label", "check"), cb = mk("input"); cb.type = "checkbox"; cb.checked = (p.gimmicks || []).includes(gm.id);
        cb.addEventListener("change", () => { const s = new Set(p.gimmicks || []); cb.checked ? s.add(gm.id) : s.delete(gm.id); p.gimmicks = [...s]; });
        l.append(cb, document.createTextNode(" " + gm.label)); gims.appendChild(l);
      }
      c.appendChild(gims);
      c.appendChild(btn("Remove this preset", () => { presets.splice(idx, 1); renderPresets(); }, "danger"));
      list.appendChild(c);
    });
  }
  async function loadPresets() {
    const r = await api("GET", "/api/admin/presets");
    if (!r.ok) return;
    presetState = r.data; presets = JSON.parse(JSON.stringify(r.data.presets));
    renderPresets(); renderEventSelect();
  }
  $("addPreset").addEventListener("click", () => { presets.push({ id: "new-preset", name: "New preset", description: "", theme: presetState.themes[0].id, gimmicks: [], name_color: "" }); renderPresets(); });
  $("savePresets").addEventListener("click", async () => {
    const r = await api("POST", "/api/admin/presets", { presets: presets.map((p) => ({ ...p, name_color: p.name_color || "" })) });
    say($("presetMsg"), r.ok ? "Saved." : (r.data.error || "Could not save."), r.ok);
    if (r.ok) { loadPresets(); loadAudit(true); }
  });
  $("resetPresets").addEventListener("click", async () => {
    if (!confirm("Throw away your edits and go back to presets.json?")) return;
    const r = await api("POST", "/api/admin/presets", { reset: true });
    say($("presetMsg"), r.ok ? "Reset." : (r.data.error || "Could not reset."), r.ok);
    loadPresets(); loadAudit(true);
  });
  async function loadEvent() {
    const r = await api("GET", "/api/admin/event");
    const ev = r.ok ? r.data.event : null;
    $("eventNow").textContent = ev ? "On now: “" + ev.title + "”" + (ev.until ? ", until " + when(ev.until) : ", until you switch it off") + "." : "No event look is running.";
  }
  $("eventOn").addEventListener("click", async () => {
    const r = await api("POST", "/api/admin/event", { preset: $("eventPreset").value, title: $("eventTitle").value, hours: +$("eventHours").value || 0 });
    say($("eventMsg"), r.ok ? "Switched on." : (r.data.error || "Could not switch on."), r.ok); loadEvent(); loadAudit(true);
  });
  $("eventOff").addEventListener("click", async () => {
    const r = await api("POST", "/api/admin/event", { off: true });
    say($("eventMsg"), r.ok ? "Switched off." : (r.data.error || "Could not switch off."), r.ok); loadEvent(); loadAudit(true);
  });

  /* ---------------- backups ---------------- */
  const mb = (b) => (b / 1048576).toFixed(b > 10485760 ? 0 : 1) + " MB";
  async function loadMaintenance() {
    const r = await api("GET", "/api/admin/maintenance");
    if (!r.ok) return;
    const d = r.data;
    $("backupInfo").textContent = d.backups_enabled
      ? "A copy is made every night after " + String(d.backup_hour).padStart(2, "0") + ":00; the latest few are kept. Last nightly run: " + (d.last_nightly || "not yet") + "."
      : "Nightly copies are switched off (ROOM_BACKUPS=0).";
    const ul = $("backupList"); ul.textContent = "";
    for (const b of d.backups) {
      const li = mk("li"), a = mk("a", null, b.name); a.href = "/api/admin/backups/" + encodeURIComponent(b.name);
      li.append(a, document.createTextNode(" · " + mb(b.size))); ul.appendChild(li);
    }
    if (!d.backups.length) ul.appendChild(mk("li", "hint", "No copies yet."));
    $("backupInfo").textContent += " IRC logs older than " + d.log_days + " days are trimmed each night.";
  }
  $("backupNow").addEventListener("click", async () => {
    $("backupNow").disabled = true;
    const r = await api("POST", "/api/admin/backup");
    $("backupNow").disabled = false;
    say($("backupMsg"), r.ok ? "Saved " + r.data.name : (r.data.error || "Backup failed."), r.ok); loadMaintenance(); loadAudit(true);
  });
  /* ---------------- audit log ---------------- */
  let auditOldest = null;
  async function loadAudit(reset) {
    if (reset) auditOldest = null;
    const r = await api("GET", "/api/admin/audit" + (auditOldest ? "?before=" + auditOldest : ""));
    if (!r.ok) return;
    const body = $("auditBody"); if (reset) body.textContent = "";
    for (const e of r.data.entries) {
      const tr = mk("tr");
      tr.append(mk("td", null, when(e.ts)), mk("td", null, e.actor), mk("td", null, e.action), mk("td", null, e.target), mk("td", null, e.detail));
      body.appendChild(tr);
      auditOldest = e.id;
    }
    $("auditMore").hidden = !r.data.more;
  }
  $("auditMore").addEventListener("click", () => loadAudit(false));

  loadPresets(); loadEvent(); loadMaintenance(); loadAudit(true);

  load(); setInterval(load, 15000);
})();
