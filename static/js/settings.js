/* settings.js -- profile, look, gimmicks, custom code, live preview. */
(() => {
  "use strict";
  const D = JSON.parse(document.getElementById("settingsData").textContent);
  const S = D.settings;
  const form = document.getElementById("settingsForm");
  const F = (name) => form.querySelector('[name="' + name + '"]');
  const $ = (id) => document.getElementById(id);
  const themeSel = $("themeSel"), frame = $("previewFrame");
  const COLOR_KEYS = [["bg", "Background"], ["panel", "Panels"], ["text", "Text"], ["muted", "Muted text"], ["accent", "Accent"],
                      ["border", "Borders"], ["msg_bg", "Message"], ["msg_own_bg", "My message"], ["input_bg", "Input box"]];
  const vars = { ...D.varDefaults, ...S.theme_vars };
  let gimmicks = new Set(S.gimmicks), autoColor = !S.name_color;

  /* ---- fill simple fields ---- */
  for (const n of ["display_name", "custom_intro", "custom_css", "custom_js"]) F(n).value = S[n] || "";
  for (const n of ["show_name_colors", "use_custom_css", "use_custom_js", "show_effects", "follow_events"]) F(n).checked = !!S[n];
  F("msg_effect").value = S.msg_effect || "none";
  F("intro_mode").value = S.intro_mode;
  F("name_color").value = S.name_color || "#7ab8ff";
  F("name_color").addEventListener("input", () => { autoColor = false; refresh(); });
  $("clearColor").addEventListener("click", () => { autoColor = true; refresh(); });

  /* ---- themes + presets ---- */
  for (const t of D.themes) { const o = document.createElement("option"); o.value = t.id; o.textContent = t.name; themeSel.appendChild(o); }
  themeSel.value = S.theme;
  const presetBox = $("presets");
  for (const p of D.presets) {
    const b = document.createElement("button"); b.type = "button"; b.textContent = p.name; b.title = p.description;
    b.addEventListener("click", () => {
      themeSel.value = p.theme; gimmicks = new Set(p.gimmicks);
      if (p.name_color) { F("name_color").value = p.name_color; autoColor = false; }
      syncGimmickBoxes(); refresh(); b.classList.add("on");
      setTimeout(() => b.classList.remove("on"), 700);
    });
    presetBox.appendChild(b);
  }

  /* ---- theme maker ---- */
  const grid = $("colorGrid"), colorInputs = {};
  let gradOn = null, gradI = null;
  for (const [key, label] of COLOR_KEYS) {
    const l = document.createElement("label"); l.append(label);
    const i = document.createElement("input"); i.type = "color"; i.value = vars[key]; i.dataset.var = key;
    i.addEventListener("input", () => { vars[key] = i.value; refresh(); });
    colorInputs[key] = i;
    l.appendChild(i); grid.appendChild(l);
  }
  { // optional gradient colour
    const l = document.createElement("label"); l.append("Gradient to");
    const span = document.createElement("span"); span.className = "inline";
    const on = document.createElement("input"); on.type = "checkbox"; on.checked = !!vars.bg2; on.title = "use a gradient background";
    const i = document.createElement("input"); i.type = "color"; i.value = vars.bg2 || "#222244";
    const upd = () => { vars.bg2 = on.checked ? i.value : ""; refresh(); };
    on.addEventListener("change", upd); i.addEventListener("input", () => { on.checked = true; upd(); });
    gradOn = on; gradI = i;
    span.append(on, i); l.appendChild(span); grid.appendChild(l);
  }
  const fontSel = form.querySelector('[data-var="font"]');
  // (fontSel / radius are filled just below)
  for (const f of D.fonts) { const o = document.createElement("option"); o.value = f; o.textContent = f; fontSel.appendChild(o); }
  fontSel.value = vars.font; fontSel.addEventListener("change", () => { vars.font = fontSel.value; refresh(); });
  const radius = form.querySelector('[data-var="radius"]');
  radius.value = vars.radius; $("radiusOut").textContent = vars.radius + "px";
  radius.addEventListener("input", () => { vars.radius = +radius.value; $("radiusOut").textContent = vars.radius + "px"; refresh(); });

  /* ---- label overrides ---- */
  const labelGrid = $("labelGrid"), labelInputs = {};
  const nice = (k) => k.replace(/_/g, " ");
  for (const k of D.labelKeys) {
    const l = document.createElement("label"); l.append(nice(k));
    const i = document.createElement("input"); i.value = (S.labels || {})[k] || ""; i.maxLength = 60;
    i.addEventListener("input", refresh); labelInputs[k] = i; l.appendChild(i); labelGrid.appendChild(l);
  }
  function updatePlaceholders() {
    const t = D.themes.find((x) => x.id === themeSel.value);
    for (const k of D.labelKeys) labelInputs[k].placeholder = ((t && t.labels[k]) || "").replace("{room}", D.roomName);
    $("themeDesc").textContent = t ? t.description : "";
    $("themeMaker").hidden = themeSel.value !== "custom";
  }

  /* ---- gimmicks ---- */
  const gList = $("gimmickList"), gBoxes = {};
  for (const g of D.gimmicks) {
    const l = document.createElement("label"); l.className = "gimmick";
    const c = document.createElement("input"); c.type = "checkbox"; gBoxes[g.id] = c;
    c.addEventListener("change", () => { c.checked ? gimmicks.add(g.id) : gimmicks.delete(g.id); refresh(); });
    const t = document.createElement("div"); const b = document.createElement("b"); b.textContent = g.label;
    const s = document.createElement("span"); s.textContent = g.description; t.append(b, s);
    l.append(c, t); gList.appendChild(l);
  }
  function syncGimmickBoxes() { for (const id in gBoxes) gBoxes[id].checked = gimmicks.has(id); }
  syncGimmickBoxes();

  /* ---- scheduled looks ---- */
  let schedule = (S.schedule || []).map((r) => ({ ...r }));
  const schedBox = $("scheduleList");
  function renderSchedule() {
    schedBox.textContent = "";
    schedule.forEach((rule, idx) => {
      const row = document.createElement("div"); row.className = "inline sched-row";
      const sel = document.createElement("select");
      for (const p of D.presets) { const o = document.createElement("option"); o.value = p.id; o.textContent = p.name; sel.appendChild(o); }
      sel.value = rule.preset;
      sel.addEventListener("change", () => { rule.preset = sel.value; });
      const a = document.createElement("input"); a.type = "time"; a.value = rule.from; a.addEventListener("change", () => { rule.from = a.value; });
      const b = document.createElement("input"); b.type = "time"; b.value = rule.to; b.addEventListener("change", () => { rule.to = b.value; });
      const x = document.createElement("button"); x.type = "button"; x.className = "ghost"; x.textContent = "remove";
      x.addEventListener("click", () => { schedule.splice(idx, 1); renderSchedule(); });
      row.append(sel, document.createTextNode(" from "), a, document.createTextNode(" to "), b, x);
      schedBox.appendChild(row);
    });
    if (!schedule.length) { const p = document.createElement("p"); p.className = "hint"; p.textContent = "No scheduled looks."; schedBox.appendChild(p); }
  }
  $("addRule").addEventListener("click", () => {
    if (!D.presets.length) return;
    const pick = D.presets.find((p) => p.id === "watcher") || D.presets[0];
    schedule.push({ preset: pick.id, from: "00:00", to: "05:00" }); renderSchedule();
  });
  renderSchedule();

  /* ---- payload ---- */
  function currentLabels() { const o = {}; for (const k in labelInputs) if (labelInputs[k].value.trim()) o[k] = labelInputs[k].value.trim(); return o; }
  function collect() {
    return {
      display_name: F("display_name").value,
      msg_effect: F("msg_effect").value, show_effects: F("show_effects").checked, follow_events: F("follow_events").checked,
      schedule: schedule.filter((r) => r.from && r.to),
      name_color: autoColor ? "" : F("name_color").value, show_name_colors: F("show_name_colors").checked,
      theme: themeSel.value, gimmicks: [...gimmicks], theme_vars: vars, labels: currentLabels(),
      custom_intro: F("custom_intro").value, intro_mode: F("intro_mode").value,
      custom_css: F("custom_css").value, use_custom_css: F("use_custom_css").checked,
      custom_js: F("custom_js").value, use_custom_js: F("use_custom_js").checked,
    };
  }

  /* ---- live preview ---- */
  let timer = null, pendingJs = null;
  function previewUrl() {
    const q = new URLSearchParams({ theme: themeSel.value, g: [...gimmicks].join(","), v: JSON.stringify(vars),
      l: JSON.stringify(currentLabels()), i: F("custom_intro").value.slice(0, D.maxIntro), t: Date.now() });
    return "/preview?" + q;
  }
  function sendCss() { frame.contentWindow && frame.contentWindow.postMessage({ type: "css", css: F("use_custom_css").checked ? F("custom_css").value : "" }, location.origin); }
  frame.addEventListener("load", () => {
    sendCss();
    if (pendingJs !== null) { frame.contentWindow.postMessage({ type: "js", code: pendingJs }, location.origin); pendingJs = null; }
  });
  function refresh() { updatePlaceholders(); clearTimeout(timer); timer = setTimeout(() => { frame.src = previewUrl(); }, 350); }
  themeSel.addEventListener("change", refresh);
  F("custom_intro").addEventListener("input", refresh);
  F("custom_css").addEventListener("input", () => { clearTimeout(F("custom_css")._t); F("custom_css")._t = setTimeout(sendCss, 200); });
  F("use_custom_css").addEventListener("change", sendCss);
  $("runJs").addEventListener("click", () => { pendingJs = F("custom_js").value; frame.src = previewUrl(); });
  window.addEventListener("message", (e) => {
    if (e.origin === location.origin && e.data && e.data.type === "js-error") say($("saveMsg"), "Preview JS error: " + e.data.message, false);
  });

  $("fxTest").addEventListener("click", () => {
    const fx = F("msg_effect").value;
    frame.contentWindow && frame.contentWindow.postMessage({ type: "fake-fx", fx: fx === "none" ? "" : fx, text: fx === "none" ? "No effect selected" : "This is the " + fx + " effect" }, location.origin);
  });

  /* ---- share / import / publish ---- */
  function shareBody(extra) {
    return { theme: themeSel.value, gimmicks: [...gimmicks], theme_vars: vars, labels: currentLabels(), custom_intro: F("custom_intro").value,
      custom_css: F("custom_css").value, use_css: true, custom_js: F("custom_js").value, include_js: $("shareJs").checked, ...extra };
  }
  function applyBundle(b, withJs) {
    themeSel.value = b.theme; gimmicks = new Set(b.gimmicks); syncGimmickBoxes();
    Object.assign(vars, b.vars);
    for (const [k, i] of Object.entries(colorInputs)) i.value = vars[k];
    if (gradOn) { gradOn.checked = !!vars.bg2; gradI.value = vars.bg2 || "#222244"; }
    fontSel.value = vars.font; radius.value = vars.radius; $("radiusOut").textContent = vars.radius + "px";
    for (const k in labelInputs) labelInputs[k].value = (b.labels || {})[k] || "";
    F("custom_intro").value = b.intro || ""; F("custom_css").value = b.css || ""; F("use_custom_css").checked = !!(b.css || "").trim();
    if (withJs && b.js) { F("custom_js").value = b.js; F("use_custom_js").checked = true; }
    refresh(); sendCss();
  }
  $("exportBtn").addEventListener("click", async () => {
    const r = await api("POST", "/api/theme/export", shareBody());
    if (!r.ok) return say($("shareMsg"), r.data.error || "Could not make a code.", false);
    $("codeBox").value = r.data.code; say($("shareMsg"), "Code ready" + (r.data.has_js ? " (includes your JS)" : "") + ".", true);
  });
  $("copyCode").addEventListener("click", async () => {
    const t = $("codeBox"); t.select();
    try { await navigator.clipboard.writeText(t.value); say($("shareMsg"), "Copied.", true); } catch (e) { document.execCommand("copy"); say($("shareMsg"), "Copied (or press Ctrl+C).", true); }
  });
  $("importBtn").addEventListener("click", async () => {
    const r = await api("POST", "/api/theme/import", { code: $("codeBox").value });
    if (!r.ok) return say($("shareMsg"), r.data.error || "Could not read that code.", false);
    const withJs = $("importJs").checked && r.data.has_js;
    if (r.data.has_js && !withJs) say($("shareMsg"), "Imported without its JS (tick the box to take that too). Press Save to keep it.", true);
    else say($("shareMsg"), "Imported. Look at the preview, then press Save to keep it.", true);
    applyBundle(r.data.bundle, withJs);
  });
  $("publishBtn").addEventListener("click", async () => {
    const r = await api("POST", "/api/gallery", shareBody({ title: $("pubTitle").value, description: $("pubDesc").value }));
    say($("pubMsg"), r.ok ? "Published. Find it in the gallery." : (r.data.error || "Could not publish."), r.ok);
  });
  { // arrived from the gallery with ?import=<id>
    const id = new URLSearchParams(location.search).get("import");
    if (id) api("GET", "/api/gallery/" + encodeURIComponent(id)).then((r) => {
      if (!r.ok) return say($("saveMsg"), r.data.error || "That gallery theme is gone.", false);
      const hasJs = !!(r.data.bundle.js || "").trim();
      const withJs = hasJs && confirm("\"" + r.data.title + "\" by " + r.data.author + " includes JavaScript that would run in your browser.\n\nOK = take the JS too, Cancel = take only the look.");
      applyBundle(r.data.bundle, withJs);
      say($("saveMsg"), "\"" + r.data.title + "\" loaded into the preview. Press Save to keep it.", true);
      history.replaceState(null, "", location.pathname);
    });
  }

  /* ---- save + password ---- */
  function say(el, text, ok) { el.textContent = text; el.className = ok ? "ok" : "bad"; }
  $("saveBtn").addEventListener("click", async () => {
    const btn = $("saveBtn"); btn.disabled = true;
    const r = await api("POST", "/api/settings", collect());
    btn.disabled = false;
    say($("saveMsg"), r.ok ? "Saved. Open the room to see it." : (r.data.error || "Could not save."), r.ok);
  });
  $("pwBtn").addEventListener("click", async () => {
    const out = $("pwMsg"), a = $("pwNew").value;
    if (a !== $("pwNew2").value) return say(out, "The new passwords do not match.", false);
    const r = await api("POST", "/api/password", { current: $("pwCur").value, new: a });
    say(out, r.ok ? "Password changed." : (r.data.error || "Could not change it."), r.ok);
    if (r.ok) { $("pwCur").value = $("pwNew").value = $("pwNew2").value = ""; }
  });

  updatePlaceholders();
  frame.src = previewUrl();
})();
