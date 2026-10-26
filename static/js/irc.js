/* irc.js -- the IRC connection page: save the server/nick/password, connect, sign up, watch the status. */
(() => {
  "use strict";
  const D = JSON.parse(document.getElementById("ircData").textContent);
  const $ = (id) => document.getElementById(id);
  const form = $("ircForm"), F = (n) => form.querySelector('[name="' + n + '"]');
  const mk = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const say = (el, text, ok) => { el.textContent = text || ""; el.className = ok ? "ok" : "bad"; };
  if (location.protocol !== "https:" && !/^(localhost|127\.|\[::1\])/.test(location.hostname)) $("httpWarn").hidden = false;

  let S = D.settings, configured = S.configured;
  function fill(s) {
    F("server").value = s.server || D.defaultServer; F("port").value = s.port || 6697;
    F("nick").value = s.nick || ""; F("account").value = s.account || ""; F("realname").value = s.realname || "";
    F("password").value = ""; F("password").placeholder = s.has_password ? "saved (leave empty to keep it)" : "your NickServ password";
    for (const n of ["tls", "tls_verify", "use_sasl", "autoconnect"]) F(n).checked = !!s[n];
    $("backLink").hidden = !s.configured;
  }
  fill(S);
  F("tls").addEventListener("change", () => {            // follow the usual port when the box is toggled
    const p = +F("port").value;
    if (F("tls").checked && p === 6667) F("port").value = 6697;
    if (!F("tls").checked && p === 6697) F("port").value = 6667;
  });

  function body(extra) {
    const b = { server: F("server").value.trim(), nick: F("nick").value.trim(), account: F("account").value.trim(),
      realname: F("realname").value, tls: F("tls").checked, tls_verify: F("tls_verify").checked,
      use_sasl: F("use_sasl").checked, autoconnect: F("autoconnect").checked, ...extra };
    if (F("port").value) b.port = +F("port").value;
    if (F("password").value) b.password = F("password").value;
    return b;
  }
  async function save(extra) {
    const r = await api("POST", "/api/irc/settings", body(extra));
    if (r.ok) { S = r.data.settings; configured = true; fill(S); }
    return r;
  }
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    $("saveBtn").disabled = true;
    const r = await save({ connect: true });
    $("saveBtn").disabled = false;
    say($("saveMsg"), r.ok ? "Saved. Connecting…" : (r.data.error || "Could not save."), r.ok);
    if (r.ok) refresh();
  });
  $("regBtn").addEventListener("click", async () => {
    say($("regMsg"), "");
    const saved = await save({ use_sasl: false });                        // the password is saved first; REGISTER uses it
    if (!saved.ok) { say($("regMsg"), saved.data.error || "Fill in the nick and password above first.", false); return; }
    F("use_sasl").checked = false;
    const r = await api("POST", "/api/irc/register", { email: $("regEmail").value.trim() });
    say($("regMsg"), r.ok ? "Connecting and sending your sign-up… watch the status box." : (r.data.error || "Could not sign up."), r.ok);
    if (r.ok) { $("regEmail").value = ""; refresh(); }
  });
  $("verifyBtn").addEventListener("click", async () => {
    const r = await api("POST", "/api/irc/verify", { code: $("verifyCode").value.trim() });
    say($("verifyMsg"), r.ok ? "Code sent. If it worked, tick “log in with SASL” and press Save & connect." : (r.data.error || "Could not verify."), r.ok);
    if (r.ok) { $("verifyCode").value = ""; setTimeout(refresh, 800); }
  });
  $("identifyBtn").addEventListener("click", async () => { const r = await api("POST", "/api/irc/identify"); if (!r.ok) alert(r.data.error || "Could not send that."); setTimeout(refresh, 800); });
  $("disconnectBtn").addEventListener("click", async () => { await api("POST", "/api/irc/disconnect"); refresh(); });
  $("reconnectBtn").addEventListener("click", async () => { const r = await api("POST", "/api/irc/connect"); if (!r.ok) alert(r.data.error || "Could not connect."); refresh(); });

  const LABEL = { connected: "Connected", connecting: "Connecting…", disconnected: "Disconnected (trying again)", failed: "Not connected", stopped: "Disconnected", idle: "Starting…" };
  async function refresh() {
    const r = await api("GET", "/api/irc");
    if (!r.ok) return;
    const st = r.data.status, s = r.data.settings;
    $("stDot").className = "dot" + (st.state === "connected" ? "" : " bad");
    $("stText").textContent = (LABEL[st.state] || st.state) + (st.state === "connected" && st.network ? " to " + st.network + " as " + st.nick : "");
    const bits = [];
    if (st.state === "connected") bits.push(st.authenticated ? "Logged in as " + (st.account || st.nick) + " (SASL)." : "Not logged in to an account (guest).");
    if (st.error && st.state !== "connected") bits.push(st.error);
    if (st.state === "disconnected" && st.retry_in) bits.push("Next attempt in about " + st.retry_in + " s.");
    if (s.auth_failed) bits.push("Automatic reconnecting is paused until you change your details or press Reconnect.");
    $("stDetail").textContent = bits.join(" ");
    const box = $("stLines"); box.textContent = "";
    for (const l of st.lines || []) box.appendChild(mk("div", "irc-line " + l.kind, (l.sender ? "<" + l.sender + "> " : "") + l.text));
    box.hidden = !box.childNodes.length;
    $("identifyBtn").hidden = !(st.state === "connected" && !st.authenticated && s.has_password);
    $("disconnectBtn").hidden = !(st.state === "connected" || st.state === "connecting" || st.state === "disconnected");
    $("reconnectBtn").hidden = !(configured && (st.state === "stopped" || st.state === "failed"));
  }
  refresh(); setInterval(refresh, 3000);
  if (!configured) $("signupBox").open = false;
})();
