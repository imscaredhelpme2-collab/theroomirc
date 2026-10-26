/* room.js -- the IRC chat page. Exposes window.Room for gimmicks and your own custom JS.
   All message text is inserted with textContent / text nodes (never innerHTML), so HTML in messages never renders. */
(() => {
  "use strict";
  const boot = JSON.parse(document.getElementById("boot").textContent);
  const $ = (id) => document.getElementById(id);
  const mk = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const boxes = $("boxes"), form = $("composer"), input = $("input"), sendBtn = $("sendBtn"), counter = $("counter"),
        errEl = $("err"), connEl = $("conn"), connText = $("connText"), side = $("side"), appEl = $("app"),
        typingEl = $("typing"), rosterEl = $("rosterList"), motdEl = $("motd"), tabsEl = $("roomTabs"),
        noticeEl = $("notice"), replyBar = $("replyBar"), replyText = $("replyText"),
        outboxNote = $("outboxNote"), mentionList = $("mentionList"), profileEl = $("profileCard"),
        youNick = $("youNick"), rosterHead = document.querySelector("#side .roster h3");
  const baseTitle = boot.pageTitle || document.title;
  const KEY = (k) => "room_" + k + "_" + boot.me.id;
  const lsGet = (k, d) => { try { const v = localStorage.getItem(KEY(k)); return v == null ? d : JSON.parse(v); } catch (e) { return d; } };
  const lsSet = (k, v) => { try { localStorage.setItem(KEY(k), JSON.stringify(v)); } catch (e) {} };
  try { document.cookie = "tzo=" + new Date().getTimezoneOffset() + ";path=/;max-age=31536000;SameSite=Lax"; } catch (e) {}
  const lc = (s) => String(s || "").toLowerCase();
  const CHAT = new Set(["user", "action", "notice"]);
  const isChat = (m) => CHAT.has(m.kind);

  /* ---------------- event bus + registry ---------------- */
  const listeners = {};
  function on(evt, fn) { (listeners[evt] = listeners[evt] || []).push(fn); }
  function emit(evt, ...args) {
    for (const fn of listeners[evt] || []) { try { fn(...args); } catch (e) { console.error("[room] " + evt, e); } }
  }
  const registry = [];
  function gimmick(def) { if (def && typeof def.init === "function") registry.push(def); }
  let timeFmt = null;

  /* ---------------- state ---------------- */
  let rev = 0, firstLoad = true, ignores = {}, features = {}, irc = { state: "connecting", nick: "", error: "" };
  let current = null, replyTo = null, namesVer = null, namesTotal = 0, httpOk = true;
  const users = new Map();    // nick (lower case) -> {id,name,username,color,status,state,prefix}  (the open channel's people)
  const items = new Map();    // message id -> {m, el, room}
  const rooms = new Map();    // room id -> {id,name,kind,joined,topic, box, more, lastDay, count, unread, mentioned, hasMore, loading, oldest, atBottom}
  const myNick = () => irc.nick || boot.me.name;

  async function api(method, url, body) {
    const r = await fetch(url, {
      method, credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": boot.csrf },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await r.json().catch(() => ({}));
    if (r.status === 401) location.href = "/login";
    if (r.status === 403 && data.disabled) location.href = "/disabled";
    return { ok: r.ok, status: r.status, data };
  }
  const uid8 = () => (crypto.randomUUID ? crypto.randomUUID().replace(/-/g, "") : Math.random().toString(36).slice(2) + Date.now().toString(36)).slice(0, 32);
  function say(text, ok) { errEl.textContent = text || ""; errEl.classList.toggle("ok", !!ok && !!text); if (ok && text) setTimeout(() => { if (errEl.textContent === text) errEl.textContent = ""; }, 3500); }

  /* ---------------- time ---------------- */
  function fmtTime(m) {
    const d = new Date(m.ts);
    if (isNaN(d)) return "";
    if (timeFmt) { const s = timeFmt(m, d); if (s != null) return s; }
    if (boot.timeFormat === "24s") return d.toLocaleTimeString([], { hour12: false });
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }
  const dayKey = (d) => d.getFullYear() + "-" + d.getMonth() + "-" + d.getDate();
  function dayLabel(d) {
    const now = new Date(), y = new Date(now.getTime() - 864e5);
    if (dayKey(d) === dayKey(now)) return "Today";
    if (dayKey(d) === dayKey(y)) return "Yesterday";
    return d.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric", year: d.getFullYear() === now.getFullYear() ? undefined : "numeric" });
  }

  /* ---------------- names, colours, text (links and your own nick) ---------------- */
  function nickColor(nick) {                      // the same nick always gets the same colour; only you see it
    let h = 0; for (const c of lc(nick)) h = (h * 31 + c.charCodeAt(0)) >>> 0;
    return "hsl(" + (h % 360) + ",60%,62%)";
  }
  const WORD = /([A-Za-z0-9_\[\]\\`^{|}\-]+)/;
  const URLS = /(https?:\/\/[^\s<>"']+)/gi;
  function addPlain(el, part) {
    const me = lc(myNick());
    for (const tok of part.split(WORD)) {
      if (!tok) continue;
      if (me && lc(tok) === me) el.appendChild(mk("span", "mention me", tok)); else el.appendChild(document.createTextNode(tok));
    }
  }
  function renderText(el, text) {
    el.textContent = "";
    if (!text) return;
    const parts = text.split(URLS);
    parts.forEach((part, i) => {
      if (i % 2 === 0) { addPlain(el, part); return; }
      const m = /[.,;:!?)\]}]+$/.exec(part), tail = m ? m[0] : "", url = tail ? part.slice(0, -tail.length) : part;
      let ok = false; try { const u = new URL(url); ok = u.protocol === "http:" || u.protocol === "https:"; } catch (e) {}
      if (ok) { const a = mk("a", null, url); a.href = url; a.target = "_blank"; a.rel = "noopener noreferrer nofollow"; el.appendChild(a); } else el.appendChild(document.createTextNode(url));
      if (tail) el.appendChild(document.createTextNode(tail));
    });
  }

  /* ---------------- rooms (server window, channels, private chats) ---------------- */
  function roomLabel(r) { return r.name; }
  function makeRoom(info) {
    const box = mk("div", "messages"); box.dataset.room = info.id; box.hidden = true;
    const more = mk("button", "loadolder", "Load older messages"); more.type = "button"; more.hidden = true;
    box.appendChild(more);
    const r = Object.assign({}, info, { box, more, lastDay: "", count: 0, unread: 0, mentioned: false, hasMore: false, loading: false, oldest: null, atBottom: true });
    more.addEventListener("click", () => loadOlder(r));
    box.addEventListener("scroll", () => {
      r.atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 90;
      if (box.scrollTop < 80 && r.hasMore && !r.loading) loadOlder(r);
    });
    boxes.appendChild(box); rooms.set(info.id, r);
    return r;
  }
  function applyRooms(list) {
    const seen = new Set();
    for (const info of list) {
      seen.add(info.id);
      const r = rooms.get(info.id);
      if (r) Object.assign(r, info); else makeRoom(info);
    }
    for (const [id, r] of [...rooms]) {
      if (seen.has(id)) continue;
      for (const [mid, it] of [...items]) if (it.room === r) items.delete(mid);
      r.box.remove(); rooms.delete(id);
    }
    if (!current || !rooms.has(current)) {
      const saved = lsGet("cur", null);
      showRoom(rooms.has(saved) ? saved : (list[0] && list[0].id));
    } else { renderTabs(); renderTopic(); renderNotice(); }
  }
  function totalUnread() { let n = 0; for (const r of rooms.values()) n += r.unread; return n; }
  function joinPrompt() {
    const name = prompt("Channel to join (for example #python):", "#");
    if (!name || name.trim() === "#") return;
    const status = [...rooms.values()].find((r) => r.kind === "status") || rooms.get(current);
    if (status) postText(status.id, "/join " + name.trim());
  }
  async function postText(room, text) {
    const r = await api("POST", "/api/messages", { text, room, client_id: uid8() });
    if (!r.ok) say(r.data.error || "Could not do that.");
    else if (r.data.open !== undefined) openWhenReady(r.data.open);
    sync();
    return r;
  }
  async function closeRoom(r) {
    if (r.kind === "channel" && !confirm("Leave " + r.name + " and close its window? (Its log here is deleted.)")) return;
    if (r.kind === "query" && !confirm("Close this chat and delete its log?")) return;
    const res = await api("POST", "/api/rooms/" + r.id + "/close");
    if (!res.ok) say(res.data.error || "Could not close that."); else sync();
  }
  function renderTabs() {
    tabsEl.textContent = "";
    const list = [...rooms.values()];
    tabsEl.hidden = !list.length;
    for (const r of list) {
      const b = mk("button", "tab" + (r.id === current ? " current" : "") + (r.mentioned ? " has-mention" : "") + (r.kind === "channel" && !r.joined ? " parted" : ""));
      b.type = "button"; b.dataset.room = r.id; b.dataset.kind = r.kind;
      b.append(mk("span", "tabicon", r.kind === "query" ? "@" : r.kind === "status" ? "⚙" : ""), mk("span", "tabname", roomLabel(r)));
      if (r.unread && r.id !== current) b.appendChild(mk("span", "tabcount", r.mentioned ? "@" : String(r.unread > 99 ? "99+" : r.unread)));
      if (r.kind !== "status") {
        const x = mk("button", "tabclose", "×"); x.type = "button"; x.title = r.kind === "channel" ? "Leave this channel" : "Close this chat"; x.setAttribute("aria-label", x.title);
        x.addEventListener("click", (e) => { e.stopPropagation(); closeRoom(r); });
        b.appendChild(x);
      }
      b.addEventListener("click", () => showRoom(r.id));
      tabsEl.appendChild(b);
    }
    const add = mk("button", "addtab", "+"); add.type = "button"; add.title = "Join a channel"; add.setAttribute("aria-label", "Join a channel");
    add.addEventListener("click", joinPrompt);
    tabsEl.appendChild(add);
  }
  function renderTopic() {
    const r = rooms.get(current), t = r && r.kind === "channel" ? (r.topic || "") : "";
    motdEl.hidden = !t; motdEl.textContent = t ? "Topic: " + t : "";
  }
  function resetRoster(r) {
    users.clear(); namesVer = null; namesTotal = 0;
    if (r && r.kind === "query") {
      users.set(lc(r.name), { id: r.name, name: r.name, username: r.name, color: "", status: "", state: "online", prefix: "" });
      users.set(lc(myNick()), { id: myNick(), name: myNick(), username: myNick(), color: "", status: "", state: "online", prefix: "" });
    }
    renderRoster(); emit("users", [...users.values()]);
  }
  function showRoom(id) {
    const r = rooms.get(id);
    if (!r) return;
    const changed = current !== id;
    current = id; lsSet("cur", id);
    for (const x of rooms.values()) x.box.hidden = x !== r;
    r.unread = 0; r.mentioned = false;
    if (changed) { cancelReply(); closeSearch(); resetRoster(r); }
    input.placeholder = r.kind === "status" ? "Commands here: /join #channel, /msg nick hello, /help" : boot.labels.placeholder;
    renderTabs(); updateTitle(); renderTopic(); renderNotice();
    if (r.atBottom || !r.shown) r.box.scrollTop = r.box.scrollHeight;
    r.shown = true;
    if (changed) { emit("room", r); sync(); }
  }
  /** a brand-new window (a channel you just joined, a new private chat) only exists here after the next sync, so wait for it */
  async function openWhenReady(target) {
    const find = () => typeof target === "number" ? rooms.get(target) : [...rooms.values()].find((r) => lc(r.name) === lc(target));
    for (let i = 0; i < 25 && !find(); i++) { await sync(); if (!find()) await new Promise((r) => setTimeout(r, 200)); }
    const r = find(); if (r) showRoom(r.id);
  }
  function updateTitle() { const n = totalUnread(); document.title = (n ? "(" + n + ") " : "") + baseTitle; }

  /* ---------------- messages ---------------- */
  function button(label, onClick) {
    const b = document.createElement("button");
    b.type = "button"; b.textContent = label; b.addEventListener("click", onClick);
    return b;
  }

  function paintReactions(box, m) {
    box.textContent = "";
    const me = lc(myNick());
    for (const [emoji, nicks] of Object.entries(m.reactions || {})) {
      if (!nicks.length) continue;
      const mine = nicks.some((n) => lc(n) === me);
      const b = mk("button", "react" + (mine ? " mine" : ""));
      b.type = "button"; b.dataset.emoji = emoji;
      b.append(mk("span", "emoji", emoji), mk("span", "n", String(nicks.length)));
      b.title = nicks.join(", ");
      b.setAttribute("aria-pressed", mine ? "true" : "false");
      b.addEventListener("click", (e) => { e.stopPropagation(); react(m.id, emoji); });
      box.appendChild(b);
    }
    box.hidden = !box.childNodes.length;
  }
  function paintQuote(q, m) {
    q.textContent = "";
    if (!m.reply) { q.hidden = true; return; }
    q.hidden = false;
    q.dataset.target = m.reply.id;
    q.classList.toggle("gone", m.reply.deleted);
    q.append(mk("span", "qname", m.reply.name), mk("span", "qtext", m.reply.deleted ? "(not available)" : m.reply.text));
  }
  function ignoreKind(m) { return isChat(m) && !m.own ? ignores[lc(m.name)] : null; }
  function applyVisibility(el, m) {
    const k = ignoreKind(m);
    el.hidden = k === "block";
    el.classList.toggle("is-muted", k === "mute");
    if (k !== "mute") el.classList.remove("revealed");
  }

  function paint(el, m) {
    if (!isChat(m)) { el.textContent = m.text; return; }
    el.dataset.sender = m.name;
    const name = el.querySelector(".name");
    name.textContent = m.kind === "action" ? "* " + m.name : m.kind === "notice" ? "-" + m.name + "-" : m.name;
    name.style.color = boot.showNameColors ? (m.own && boot.me.color ? boot.me.color : nickColor(m.name)) : "";
    name.title = m.account ? m.name + " (logged in as " + m.account + ")" : m.name;
    const t = el.querySelector(".time");
    t.textContent = fmtTime(m);
    t.title = new Date(m.ts).toLocaleString();
    renderText(el.querySelector(".text"), m.text);
    el.querySelector(".text").hidden = !m.text;
    el.classList.toggle("mention-me", !!m.highlight && !m.own);
    paintQuote(el.querySelector(".quote"), m);
    paintReactions(el.querySelector(".reactions"), m);
    applyVisibility(el, m);
  }

  async function react(id, emoji) {
    const r = await api("POST", "/api/messages/" + id + "/react", { emoji });
    if (!r.ok) say(r.data.error || "Could not react."); else sync();
  }

  /** the ⋯ menu is filled in when it is opened, so it always matches what the server supports right now */
  function fillActions(el, acts, m) {
    acts.textContent = "";
    if (features.react && m.msgid) {
      const picker = mk("div", "picker");
      for (const e of boot.reactions || []) {
        const b = button(e, (ev) => { ev.stopPropagation(); el.classList.remove("open"); react(m.id, e); });
        b.className = "pick"; b.setAttribute("aria-label", "React " + e); picker.appendChild(b);
      }
      acts.appendChild(picker);
    }
    const row = mk("div", "actrow");
    if (features.reply && m.msgid) row.appendChild(button("Reply", () => { el.classList.remove("open"); setReply(m); }));
    row.appendChild(button("Copy", () => { el.classList.remove("open"); try { navigator.clipboard.writeText(m.text); say("Copied.", true); } catch (e) { say("Could not copy."); } }));
    if (!m.own) row.appendChild(button("Message " + m.name, async () => {
      el.classList.remove("open");
      const d = await api("POST", "/api/query", { nick: m.name });
      if (!d.ok) say(d.data.error || "Could not open that chat."); else openWhenReady(d.data.room);
    }));
    acts.appendChild(row);
  }

  function buildMessage(m) {
    const el = document.createElement("div");
    el.dataset.id = m.id;
    if (!isChat(m)) { el.className = "msg system kind-" + m.kind; paint(el, m); return el; }
    el.className = "msg kind-" + m.kind + (m.own ? " mine" : "");
    el.dataset.uid = m.own ? boot.me.id : "";
    const meta = mk("div", "meta");
    const name = mk("span", "name"); name.tabIndex = 0; name.setAttribute("role", "button");
    const showCard = (e) => { e.stopPropagation(); openProfile(items.get(m.id) ? items.get(m.id).m.name : m.name); };
    name.addEventListener("click", showCard);
    name.addEventListener("keydown", (e) => { if (e.key === "Enter") showCard(e); });
    const time = mk("span", "time");
    meta.append(name, time);
    const quote = mk("div", "quote"); quote.hidden = true;
    quote.addEventListener("click", () => { const id = +quote.dataset.target, it = items.get(id); if (it) jumpTo(it.room.id, id); });
    const text = mk("div", "text");
    const reactions = mk("div", "reactions"); reactions.hidden = true;
    const muteNote = button("Muted. Tap to show.", () => el.classList.add("revealed")); muteNote.className = "mutenote";
    el.append(meta, quote, text, reactions, muteNote);

    const acts = mk("div", "msg-actions");
    const menu = button("⋯", (ev) => {
      ev.stopPropagation();
      document.querySelectorAll(".msg.open").forEach((x) => { if (x !== el) x.classList.remove("open"); });
      const it = items.get(m.id);
      fillActions(el, acts, it ? it.m : m);
      el.classList.toggle("open");
    });
    menu.className = "msg-menu"; menu.setAttribute("aria-label", "Message options");
    meta.appendChild(menu);
    el.appendChild(acts);
    paint(el, m);
    return el;
  }

  const sepFor = (d) => { const s = mk("div", "daysep", dayLabel(d)); s.dataset.key = dayKey(d); return s; };
  function appendItem(r, m, el) {
    const d = new Date(m.ts);
    if (!isNaN(d) && dayKey(d) !== r.lastDay) { r.lastDay = dayKey(d); r.box.appendChild(sepFor(d)); }
    r.box.appendChild(el);
  }

  /** returns true if the message was new */
  function applyMessage(m) {
    const r = rooms.get(m.room);
    if (!r) return false;
    const cur = items.get(m.id);
    if (m.deleted) { if (cur) { cur.el.remove(); items.delete(m.id); r.count--; } return false; }
    if (cur) { cur.m = m; paint(cur.el, m); return false; }
    const el = buildMessage(m);
    items.set(m.id, { m, el, room: r });
    appendItem(r, m, el); r.count++;
    if (r.oldest == null || m.id < r.oldest) r.oldest = m.id;
    return true;
  }
  document.addEventListener("click", () => document.querySelectorAll(".msg.open").forEach((x) => x.classList.remove("open")));

  /* ---- older messages ---- */
  function prependItems(r, list) {
    const frag = document.createDocumentFragment();
    let prev = "";
    for (const m of list) {
      if (m.deleted || items.has(m.id)) continue;
      const d = new Date(m.ts), key = isNaN(d) ? "" : dayKey(d);
      if (key && key !== prev) { prev = key; frag.appendChild(sepFor(d)); }
      const el = buildMessage(m);
      items.set(m.id, { m, el, room: r }); r.count++;
      frag.appendChild(el);
    }
    const firstSep = r.box.querySelector(".daysep");
    if (firstSep && firstSep.dataset.key === prev) firstSep.remove();      // same day continues: drop the duplicate heading
    r.more.after(frag);
    if (list.length) r.oldest = Math.min(r.oldest == null ? Infinity : r.oldest, list[0].id);
    if (!r.lastDay && list.length) { const d = new Date(list[list.length - 1].ts); if (!isNaN(d)) r.lastDay = dayKey(d); }
  }
  async function loadOlder(r) {
    if (r.loading || !r.hasMore) return;
    if (r.oldest == null) { r.hasMore = false; r.more.hidden = true; return; }
    r.loading = true; r.more.textContent = "Loading…";
    try {
      const res = await api("GET", "/api/history?room=" + r.id + "&before=" + r.oldest);
      if (res.ok) {
        const h = r.box.scrollHeight, top = r.box.scrollTop;
        prependItems(r, res.data.messages);
        r.hasMore = res.data.more;
        r.box.scrollTop = top + (r.box.scrollHeight - h);        // keep the reader exactly where they were
      }
    } catch (e) { /* offline: try again on the next scroll */ }
    finally { r.loading = false; r.more.textContent = "Load older messages"; r.more.hidden = !r.hasMore; }
  }
  function flash(el) { el.classList.add("flash"); setTimeout(() => el.classList.remove("flash"), 1800); }
  async function jumpTo(roomId, id) {
    showRoom(roomId);
    const r = rooms.get(roomId);
    for (let tries = 0; r && !items.has(id) && r.hasMore && tries < 40; tries++) await loadOlder(r);
    const it = items.get(id);
    if (it) { it.el.hidden = false; it.el.scrollIntoView({ block: "center" }); flash(it.el); }
    else say("That message is too far back to jump to.");
  }

  /* ---- effect that plays on arrival (your own choice in settings; nobody else sees it) ---- */
  function typewrite(el) {
    const nodes = [], walk = (n) => { for (const c of n.childNodes) c.nodeType === 3 ? nodes.push([c, c.nodeValue]) : walk(c); };
    walk(el);
    const total = nodes.reduce((a, n) => a + n[1].length, 0);
    if (!total) return;
    nodes.forEach((n) => { n[0].nodeValue = ""; });
    const step = Math.max(1, Math.ceil(total / 90));
    let shown = 0;
    const t = setInterval(() => {
      shown = Math.min(total, shown + step);
      let left = shown;
      for (const [node, full] of nodes) { const take = Math.max(0, Math.min(full.length, left)); node.nodeValue = full.slice(0, take); left -= take; }
      if (shown >= total) clearInterval(t);
    }, 28);
  }
  function playEffect(el) {
    const fx = boot.myEffect;
    if (!boot.showEffects || !fx || fx === "none" || matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    el.dataset.fx = fx;
    if (fx === "typewriter") typewrite(el.querySelector(".text"));
    setTimeout(() => { delete el.dataset.fx; }, 3600);
  }

  /* ---------------- people in the open channel / profile cards ---------------- */
  function renderRoster() {
    rosterEl.textContent = "";
    if (rosterHead) {
      rosterHead.textContent = boot.labels.roster_title;
      if (namesTotal) rosterHead.appendChild(mk("span", "rostercount", " (" + namesTotal + ")"));
    }
    const list = [...users.values()];
    if (!list.length) { const r = rooms.get(current); rosterEl.appendChild(mk("li", "rosterempty", r && r.kind === "channel" ? (r.joined ? "Loading the list…" : "You are not in this channel.") : "")); return; }
    for (const u of list) {
      const li = document.createElement("li");
      li.className = (u.state === "away" ? "away offline" : "active") + (lc(u.name) === lc(myNick()) ? " you" : "");
      li.append(mk("span", "pprefix", u.prefix ? u.prefix[0] : ""));
      const nm = mk("button", "pname", u.name); nm.type = "button";
      if (boot.showNameColors) nm.style.color = nickColor(u.name);
      nm.addEventListener("click", (e) => { e.stopPropagation(); openProfile(u.name); });
      li.appendChild(nm);
      const k = ignores[lc(u.name)];
      if (k) li.appendChild(mk("span", "pstatus", k === "block" ? "blocked" : "muted"));
      else if (u.state === "away") li.appendChild(mk("span", "pstatus", "away"));
      rosterEl.appendChild(li);
    }
  }
  function applyNames(n) {
    if (!n || n.room !== current) return;
    namesTotal = n.total || 0;
    if (n.list) {
      namesVer = n.ver; users.clear();
      for (const e of n.list) users.set(lc(e.nick), { id: e.nick, name: e.nick, username: e.nick, color: "", status: e.away ? "away" : "", state: e.away ? "away" : "online", prefix: e.prefix || "", account: e.account || "" });
      emit("users", [...users.values()]);
    }
    renderRoster();
  }

  async function openProfile(nick) {
    const r = await api("GET", "/api/irc/user/" + encodeURIComponent(nick) + (current ? "?room=" + current : ""));
    if (!r.ok) { say(r.data.error || "Could not load that person."); return; }
    const p = r.data;
    profileEl.textContent = "";
    const head = mk("div", "pc-head");
    const nm = mk("strong", "pc-name", p.nick || nick); if (boot.showNameColors) nm.style.color = nickColor(p.nick || nick);
    const x = button("✕", closeProfile); x.className = "pc-x"; x.setAttribute("aria-label", "Close");
    head.append(nm, x);
    profileEl.appendChild(head);
    if (p.user && p.host) profileEl.appendChild(mk("div", "pc-user", p.user + "@" + p.host));
    if (p.realname) profileEl.appendChild(mk("div", "pc-status", p.realname));
    const dl = mk("dl", "pc-facts");
    const fact = (k, v) => { dl.append(mk("dt", null, k), mk("dd", null, v)); };
    if (p.account) fact("Account", p.account);
    if (p.prefix) fact("In this channel", { "~": "owner", "&": "admin", "@": "operator", "%": "half-op", "+": "voiced" }[p.prefix[0]] || p.prefix);
    if (p.away) fact("Status", "away");
    if (!p.user && !p.account && !p.realname) fact("Info", "Nothing more is known yet. Try Whois.");
    profileEl.appendChild(dl);
    if (!p.me) {
      const row = mk("div", "pc-actions");
      row.appendChild(button("Message", async () => {
        const d = await api("POST", "/api/query", { nick: p.nick || nick });
        if (!d.ok) { say(d.data.error || "Could not open that chat."); return; }
        closeProfile(); await openWhenReady(d.data.room); input.focus();
      }));
      row.appendChild(button("Whois", () => { closeProfile(); if (current) postText(current, "/whois " + (p.nick || nick)); }));
      const setRel = async (kind) => {
        const d = await api("POST", "/api/ignore", { nick: p.nick || nick, kind });
        if (d.ok) { ignores = d.data.ignores; refreshVisibility(); renderRoster(); closeProfile(); }
      };
      row.appendChild(button(p.relation === "mute" ? "Unmute" : "Mute", () => setRel(p.relation === "mute" ? "none" : "mute")));
      row.appendChild(button(p.relation === "block" ? "Unblock" : "Block", () => setRel(p.relation === "block" ? "none" : "block")));
      profileEl.appendChild(row);
      profileEl.appendChild(mk("p", "pc-hint", "Mute collapses their messages for you. Block hides them completely. Only you can see this; they are not told."));
    }
    profileEl.hidden = false;
    x.focus();
  }
  function closeProfile() { profileEl.hidden = true; }
  profileEl.addEventListener("click", (e) => e.stopPropagation());
  document.addEventListener("click", () => { if (!profileEl.hidden) closeProfile(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeProfile(); closeSearch(); hideMentions(); } });
  function refreshVisibility() { for (const it of items.values()) if (isChat(it.m)) applyVisibility(it.el, it.m); }

  /* ---------------- typing ---------------- */
  let typingList = [], typingTimer = null;
  function setTyping(list) {
    const now = Date.now();
    typingList = (list || []).map((t) => ({ name: t.name, until: now + t.ttl * 1000 }));
    renderTyping();
  }
  function renderTyping() {
    const now = Date.now();
    typingList = typingList.filter((t) => t.until > now);
    const names = typingList.map((t) => t.name);
    typingEl.textContent = !names.length ? "" : names.length > 2 ? "Several people are typing…"
      : names.join(" and ") + (names.length > 1 ? " are typing…" : " is typing…");
    clearTimeout(typingTimer);
    if (typingList.length) typingTimer = setTimeout(renderTyping, Math.max(200, Math.min(...typingList.map((t) => t.until)) - now));
  }

  /* ---------------- replies ---------------- */
  function setReply(m) {
    replyTo = { id: m.id, name: m.name, text: (m.text || "").slice(0, 80) };
    replyText.textContent = "Replying to " + m.name + ": " + replyTo.text;
    replyBar.hidden = false; input.focus();
  }
  function cancelReply() { replyTo = null; replyBar.hidden = true; }
  $("replyCancel").addEventListener("click", cancelReply);

  /* ---------------- connection to IRC: notice + indicator ---------------- */
  const STATE_TEXT = { connected: "connected", connecting: "connecting…", disconnected: "reconnecting…", failed: "login failed", stopped: "disconnected", idle: "starting…" };
  function renderNotice() {
    const r = rooms.get(current);
    let text = "";
    if (!boot.preview && irc.state !== "connected") text = "Not connected to IRC (" + (STATE_TEXT[irc.state] || irc.state) + ")." + (irc.error ? " " + irc.error : "") + " Messages cannot be sent right now.";
    else if (r && r.kind === "channel" && !r.joined) text = "You are not in " + r.name + ". Type /join " + r.name + " to come back.";
    noticeEl.hidden = !text; noticeEl.textContent = text;
    sendBtn.disabled = sending;
  }
  function applyIrc(d) {
    const nickChanged = d.nick && d.nick !== irc.nick;
    irc = d;
    if (youNick) youNick.textContent = d.nick || boot.me.name;
    if (nickChanged) { for (const it of items.values()) if (isChat(it.m) && it.m.text) renderText(it.el.querySelector(".text"), it.m.text); }
    updateConn(); renderNotice();
  }
  function updateConn() {
    const good = httpOk && irc.state === "connected";
    connEl.classList.toggle("bad", !httpOk || (irc.state !== "connected" && irc.state !== "connecting"));
    connEl.classList.toggle("warn", httpOk && irc.state === "connecting");
    connText.textContent = !httpOk ? boot.labels.lost_label : good ? boot.labels.live_label : "IRC " + (STATE_TEXT[irc.state] || irc.state);
  }

  /* ---------------- applying a sync result ---------------- */
  function noteIncoming(m, r) {
    if (document.hidden || r.id !== current) {
      r.unread++;
      if (m.highlight) r.mentioned = true;
      renderTabs(); updateTitle();
    }
  }
  function applySync(d) {
    if (d.irc) applyIrc(d.irc);
    if (d.features) features = d.features;
    if (d.ignores) { ignores = d.ignores; refreshVisibility(); }
    if (d.rooms) applyRooms(d.rooms);
    if (d.names) applyNames(d.names);
    const curRoom = rooms.get(current);
    const nearBottom = !curRoom || curRoom.box.scrollHeight - curRoom.box.scrollTop - curRoom.box.clientHeight < 90;
    const fresh = [];
    for (const m of d.messages || []) if (applyMessage(m)) fresh.push(m);
    if (typeof d.rev === "number") rev = Math.max(rev, d.rev);
    if (firstLoad) for (const r of rooms.values()) { r.hasMore = r.count >= boot.historyLimit; r.more.hidden = !r.hasMore; }
    if (curRoom && (firstLoad || nearBottom)) curRoom.box.scrollTop = curRoom.box.scrollHeight;
    for (const m of fresh) {
      const it = items.get(m.id), r = rooms.get(m.room);
      emit("message", m);
      if (!firstLoad && isChat(m) && it && !m.own) {
        const k = ignoreKind(m);
        if (k !== "block" && k !== "mute") {
          playEffect(it.el);
          emit("incoming", m);
          if (m.highlight) emit("mention", m);
          noteIncoming(m, r);
        }
      }
    }
    firstLoad = false;
    emit("count", [...rooms.values()].reduce((a, r) => a + (r.kind === "query" ? 0 : r.count), 0));
    setTyping(d.typing);
    if (d.room && d.room.name && !boot.preview) { const t = $("roomTitle"); if (t && !t.dataset.set) { t.dataset.set = "1"; } }
    if (d.event !== undefined && !boot.preview && ((d.event && d.event.id) || "") !== boot.eventId) reloadOnce();
    renderPending();
  }

  /* ---- room look changes (event themes, scheduled looks) ---- */
  function reloadOnce() {
    try { const last = +sessionStorage.getItem("room_reload") || 0; if (Date.now() - last < 20000) return; sessionStorage.setItem("room_reload", Date.now()); } catch (e) {}
    location.reload();
  }
  function ruleNow() {
    const d = new Date(), mins = d.getHours() * 60 + d.getMinutes(), t = (s) => +s.slice(0, 2) * 60 + +s.slice(3);
    const rules = boot.schedule || [];
    for (let i = 0; i < rules.length; i++) {
      const a = t(rules[i].from), b = t(rules[i].to);
      if (a < b ? (mins >= a && mins < b) : (mins >= a || mins < b)) return i;
    }
    return null;
  }
  if (!boot.preview) setInterval(() => { if (!boot.eventId && (boot.schedule || []).length && ruleNow() !== boot.activeRule) reloadOnce(); }, 30000);
  if (!boot.preview && (boot.schedule || []).length && !boot.eventId && ruleNow() !== boot.activeRule) setTimeout(reloadOnce, 600);

  /* ---------------- connection to this server ---------------- */
  let connOk = null;
  function setConn(ok) {
    httpOk = ok; updateConn();
    if (connOk !== ok) { connOk = ok; emit("conn", ok); if (ok) flushOutbox(); }
  }

  let syncing = false, again = false, lastSync = 0, streamOk = false;
  async function sync() {
    if (boot.preview) return;                       // a preview never talks to the server (it only shows sample data)
    if (syncing) { again = true; return; }
    syncing = true; lastSync = Date.now();
    try {
      const q = "/api/sync?rev=" + rev + (current ? "&room=" + current : "") + (namesVer != null ? "&nv=" + namesVer : "");
      const r = await fetch(q, { cache: "no-store", credentials: "same-origin" });
      if (r.status === 401) { location.href = "/login"; return; }
      if (r.status === 403) { const d = await r.json().catch(() => ({})); if (d.disabled) { location.href = "/disabled"; return; } }
      if (!r.ok) throw new Error("HTTP " + r.status);
      const d = await r.json();
      applySync(d); setConn(true);
      if (d.more) again = true;
    } catch (e) { setConn(false); }
    finally { syncing = false; if (again) { again = false; sync(); } }
  }

  function startStream() {
    if (!window.EventSource) return;
    const es = new EventSource("/api/stream");
    es.onopen = () => { streamOk = true; };
    es.onmessage = () => sync();               // every event is just "something changed (or heartbeat)"
    es.onerror = () => { streamOk = false; };  // the browser reconnects by itself; polling covers the gap
  }
  setInterval(() => { if (!boot.preview && (!streamOk || Date.now() - lastSync > 20000)) sync(); }, 2000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) { const r = rooms.get(current); if (r) { r.unread = 0; r.mentioned = false; } renderTabs(); updateTitle(); if (!boot.preview) sync(); } });
  window.addEventListener("online", () => { sync(); flushOutbox(); });

  /* ---------------- offline-friendly sending (only for plain messages, never for commands) ---------------- */
  let outbox = lsGet("outbox", []), flushing = false;
  const saveOutbox = () => lsSet("outbox", outbox);
  function renderPending() {
    document.querySelectorAll(".msg.pending").forEach((x) => x.remove());
    for (const it of outbox) {
      const r = rooms.get(it.room);
      if (!r) continue;
      const el = mk("div", "msg mine pending" + (it.failed ? " failed" : ""));
      const meta = mk("div", "meta");
      meta.append(mk("span", "name", myNick()), mk("span", "time", it.failed ? "not sent" : "waiting for connection…"));
      el.append(meta, mk("div", "text", it.text));
      if (it.failed) {
        el.appendChild(mk("div", "pending-err", it.failed));
        const row = mk("div", "msg-actions-inline");
        row.append(button("Try again", () => { delete it.failed; saveOutbox(); renderPending(); flushOutbox(); }),
                   button("Discard", () => { outbox = outbox.filter((x) => x !== it); saveOutbox(); renderPending(); }));
        el.appendChild(row);
      }
      r.box.appendChild(el);
    }
    const waiting = outbox.filter((x) => !x.failed).length;
    outboxNote.hidden = !waiting;
    outboxNote.textContent = waiting ? waiting + " message" + (waiting > 1 ? "s" : "") + " waiting to send" : "";
    const r = rooms.get(current);
    if (r && outbox.length && r.atBottom) r.box.scrollTop = r.box.scrollHeight;
  }
  async function flushOutbox() {
    if (flushing || boot.preview) return;
    flushing = true;
    try {
      for (;;) {
        const it = outbox.find((x) => !x.failed);
        if (!it) break;
        let r;
        try { r = await api("POST", "/api/messages", { text: it.text, room: it.room, reply_to: it.reply_to, client_id: it.cid }); }
        catch (e) { break; }                                       // still offline: keep everything queued
        if (r.ok) outbox = outbox.filter((x) => x !== it);
        else if (r.status === 429 || r.status >= 500) break;      // busy: try again shortly
        else it.failed = r.data.error || "IRC refused this message.";
        saveOutbox(); renderPending();
      }
    } finally { flushing = false; renderPending(); sync(); }
  }
  setInterval(() => { if (outbox.some((x) => !x.failed)) flushOutbox(); }, 5000);

  /* ---------------- composing ---------------- */
  let sending = false, lastTypingPing = 0;
  function autosize() { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 128) + "px"; }
  function updateCounter() { counter.textContent = input.value.length + "/" + boot.maxLength; }

  /* nick completion: type @ and a few letters, or press Tab after the first letters */
  let mentionIdx = 0, mentionStart = -1;
  function mentionQuery() {
    const pos = input.selectionStart, m = /(^|\s)@([A-Za-z0-9_\[\]\\`^{|}\-]{0,30})$/.exec(input.value.slice(0, pos));
    return m ? { start: pos - m[2].length - 1, partial: lc(m[2]) } : null;
  }
  const candidates = (partial) => [...users.values()].filter((u) => lc(u.name) !== lc(myNick()) && lc(u.name).startsWith(partial)).slice(0, 6);
  function hideMentions() { mentionList.hidden = true; mentionStart = -1; }
  function updateMentions() {
    const q = mentionQuery();
    if (!q) return hideMentions();
    const list = candidates(q.partial);
    if (!list.length) return hideMentions();
    mentionStart = q.start; mentionIdx = Math.min(mentionIdx, list.length - 1);
    mentionList.textContent = "";
    list.forEach((u, i) => {
      const li = mk("li", i === mentionIdx ? "sel" : ""); li.setAttribute("role", "option"); li.dataset.username = u.name;
      li.append(mk("b", null, u.name), mk("span", null, u.prefix || ""));
      li.addEventListener("mousedown", (e) => { e.preventDefault(); pickMention(u.name); });
      mentionList.appendChild(li);
    });
    mentionList.hidden = false;
  }
  function pickMention(nick, start, end) {
    const pos = end != null ? end : input.selectionStart, v = input.value, from = start != null ? start : mentionStart;
    const atStart = v.slice(0, from).trim() === "";
    const ins = nick + (atStart ? ": " : " ");
    input.value = v.slice(0, from) + ins + v.slice(pos);
    const np = from + ins.length;
    input.setSelectionRange(np, np); hideMentions(); autosize(); updateCounter(); input.focus();
  }
  function tabComplete() {
    const pos = input.selectionStart, m = /([A-Za-z0-9_\[\]\\`^{|}\-]+)$/.exec(input.value.slice(0, pos));
    if (!m) return false;
    const list = candidates(lc(m[1]));
    if (!list.length) return false;
    pickMention(list[0].name, pos - m[1].length, pos);
    return true;
  }

  input.addEventListener("input", () => {
    autosize(); updateCounter(); updateMentions();
    if (!boot.preview && features.typing && current && input.value.trim() && !input.value.startsWith("/") && Date.now() - lastTypingPing > 2500) {
      lastTypingPing = Date.now(); api("POST", "/api/typing", { room: current }).catch(() => {});
    }
  });
  input.addEventListener("keydown", (e) => {
    if (!mentionList.hidden) {
      const n = mentionList.children.length;
      if (e.key === "ArrowDown") { e.preventDefault(); mentionIdx = (mentionIdx + 1) % n; updateMentions(); return; }
      if (e.key === "ArrowUp") { e.preventDefault(); mentionIdx = (mentionIdx - 1 + n) % n; updateMentions(); return; }
      if (e.key === "Enter" || e.key === "Tab") { e.preventDefault(); pickMention(mentionList.children[mentionIdx].dataset.username); return; }
      if (e.key === "Escape") { hideMentions(); return; }
    }
    if (e.key === "Tab" && !e.shiftKey && input.value && tabComplete()) { e.preventDefault(); return; }
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
  });
  input.addEventListener("blur", () => setTimeout(hideMentions, 120));
  input.addEventListener("keyup", (e) => { if (e.key === "ArrowLeft" || e.key === "ArrowRight") updateMentions(); });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    if (boot.preview) { say("(preview: nothing is sent)"); return; }
    if (!current) return;
    sending = true; sendBtn.disabled = true; say("");
    const room = current, cid = uid8(), reply_to = replyTo ? replyTo.id : null;
    const isCmd = text.startsWith("/") && !text.startsWith("//");
    const done = () => { input.value = ""; autosize(); updateCounter(); cancelReply(); hideMentions(); lastTypingPing = 0; };
    try {
      const r = await api("POST", "/api/messages", { text, room, reply_to, client_id: cid });
      if (!r.ok) {
        say(r.data.error || "Could not send."); emit("send-error", r.data.error);
        if (r.status >= 502) throw new TypeError("gateway");
      } else {
        done(); emit("sent", text);
        if (r.data.open !== undefined) openWhenReady(r.data.open);
        if (r.data.ignores) { ignores = r.data.ignores; refreshVisibility(); }
        sync();
      }
    } catch (err) {
      if (isCmd) say("You look offline, so that command was not sent.");
      else {                                                  // text can wait: queue it and send when the connection returns
        outbox.push({ cid, text, room, reply_to });
        saveOutbox(); done(); renderPending(); setConn(false); emit("queued", text);
      }
      emit("send-error", "network");
    } finally { sending = false; renderNotice(); input.focus(); }
  });
  updateCounter();

  /* ---------------- search ---------------- */
  const panel = $("searchPanel"), sInput = $("searchInput"), sResults = $("searchResults");
  let sTimer = null, sSeq = 0;
  function openSearch() { panel.hidden = false; sInput.focus(); sInput.select(); }
  function closeSearch() { panel.hidden = true; sResults.textContent = ""; sInput.value = ""; }
  async function runSearch() {
    const q = sInput.value.trim(), seq = ++sSeq;
    sResults.textContent = "";
    if (q.length < 2) return;
    const r = await api("GET", "/api/search?q=" + encodeURIComponent(q) + ($("searchHere").checked && current ? "&room=" + current : ""));
    if (seq !== sSeq) return;
    if (!r.ok) { sResults.appendChild(mk("li", "sr-empty", r.data.error || "Search failed.")); return; }
    const found = (r.data.results || []).filter((m) => ignores[lc(m.name)] !== "block");
    if (!found.length) { sResults.appendChild(mk("li", "sr-empty", "Nothing found.")); return; }
    for (const m of found) {
      const li = mk("li", "sr"); li.tabIndex = 0;
      const rm = rooms.get(m.room);
      const head = mk("div", "sr-head");
      head.append(mk("b", null, m.name), mk("span", "sr-room", rm ? roomLabel(rm) : m.room_name || ""), mk("span", "sr-time", new Date(m.ts).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })));
      li.append(head, mk("div", "sr-text", m.text || ""));
      const go = () => { closeSearch(); jumpTo(m.room, m.id); };
      li.addEventListener("click", go); li.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
      sResults.appendChild(li);
    }
  }
  if ($("searchBtn")) $("searchBtn").addEventListener("click", () => (panel.hidden ? openSearch() : closeSearch()));
  $("searchClose").addEventListener("click", closeSearch);
  $("searchForm").addEventListener("submit", (e) => { e.preventDefault(); clearTimeout(sTimer); runSearch(); });
  sInput.addEventListener("input", () => { clearTimeout(sTimer); sTimer = setTimeout(runSearch, 300); });
  $("searchHere").addEventListener("change", runSearch);

  /* ---------------- intro + side panel ---------------- */
  function hash(s) { let h = 0; for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0; return (h >>> 0).toString(36); }
  function showIntro() {
    const el = $("intro");
    if (!el) return;
    if (!boot.intro || boot.introMode === "never") { el.remove(); return; }
    if (boot.introMode === "once") {
      const key = "room_intro_" + boot.me.id + "_" + hash(boot.intro);
      try { if (localStorage.getItem(key)) { el.remove(); return; } localStorage.setItem(key, "1"); } catch (e) {}
    }
    el.hidden = false;
    setTimeout(() => el.remove(), 9500);
  }
  function initSideToggle() {
    const btn = $("sideToggle");
    if (window.innerWidth <= 820) side.classList.add("collapsed");
    btn.addEventListener("click", () => side.classList.toggle("collapsed"));
  }
  function showEventNote() {
    const n = $("eventNote");
    if (n && boot.eventId) { n.hidden = false; n.textContent = "✨ " + boot.eventTitle + ": a look the admin switched on for everyone."; }
  }

  function initJoinsToggle() {
    const b = $("joinsBtn");
    if (!b) return;
    const apply = (on) => { appEl.classList.toggle("hide-joins", on); b.setAttribute("aria-pressed", on ? "true" : "false"); };
    apply(!!lsGet("hidejoins", false));
    b.addEventListener("click", () => { const on = !appEl.classList.contains("hide-joins"); apply(on); lsSet("hidejoins", on); });
  }

  /* ---------------- preview mode (settings page iframe, gimmick sandbox) ---------------- */
  function startPreview() {
    const me = boot.me, ago = (min) => new Date(Date.now() - min * 60000).toISOString();
    const nicks = [{ nick: "Mina", prefix: "@" }, { nick: "Jules", prefix: "+" }, { nick: me.name, prefix: "" }];
    const msg = (id, who, text, min, extra) => Object.assign({ id, uid: who === me.name ? me.id : null, name: who, text, ts: ago(min), edited: false, deleted: false,
      kind: "user", room: 1, own: who === me.name, account: "", msgid: "p" + id, mentions: [], highlight: false, reactions: {}, reply: null }, extra || {});
    const base = { irc: { state: "connected", nick: me.name, error: "" },
      features: { typing: true, react: true, reply: true, echo: true, edit: false, delete: false, images: false },
      ignores: {}, room: { name: boot.roomName, motd: "" } };
    applySync(Object.assign({ rev: 5, rooms: [{ id: 1, name: "#general", kind: "channel", joined: true, topic: "Welcome! This is a preview, nothing here is real." }],
      names: { room: 1, total: 3, ver: 1, list: nicks.map((n) => ({ nick: n.nick, prefix: n.prefix, away: false, account: "" })) },
      typing: [{ name: "Jules", ttl: 4 }], messages: [
        msg(1, "Mina", "hey, is anyone here?", 42), msg(2, me.name, "just got in!", 41, { reactions: { "❤️": ["Mina"] } }),
        msg(3, "Mina", "love how this looks", 40, { reactions: { "👍": [me.name, "Jules"], "🎉": ["Mina"] } }),
        msg(4, me.name, "same. this is my theme", 39, { reply: { id: 3, name: "Mina", text: "love how this looks", deleted: false } }),
        { id: 5, uid: null, name: "", text: "Jules joined", ts: ago(30), kind: "join", room: 1, own: false, reactions: {}, mentions: [], deleted: false } ] }, base));
    let nextId = 10;
    const fake = (text, extra) => applySync({ messages: [msg(nextId++, "Mina", text, 0, extra)], typing: [] });
    setTimeout(() => fake("a brand new message just arrived!"), 3500);
    window.addEventListener("message", (e) => {
      if (e.origin !== location.origin || !e.data) return;
      if (e.data.type === "css") { const s = $("liveCss"); if (s) s.textContent = e.data.css || ""; }
      if (e.data.type === "js") {
        try { new Function("Room", e.data.code)(Room); }
        catch (err) { console.error(err); parent.postMessage({ type: "js-error", message: String(err) }, location.origin); }
      }
      if (e.data.type === "fake") fake(String(e.data.text || "hello from Mina").slice(0, 200), e.data.mention ? { highlight: true, mentions: [me.id] } : {});
      if (e.data.type === "fake-fx") { fake(String(e.data.text || "watch this")); const last = [...items.values()].pop(); if (last) { const old = boot.myEffect; boot.myEffect = e.data.fx; playEffect(last.el); boot.myEffect = old; } }
    });
  }

  /* ---------------- public API ---------------- */
  const Room = {
    boot, me: boot.me, theme: boot.theme, labels: boot.labels,
    on, gimmick,
    card(title, cls) {
      const s = document.createElement("section");
      s.className = "card" + (cls ? " " + cls : "");
      if (title) { const h = document.createElement("h3"); h.textContent = title; s.appendChild(h); }
      side.appendChild(s);
      return s;
    },
    banner(el) { appEl.insertBefore(el, appEl.firstChild); return el; },
    get background() { return $("bgLayer"); },
    setTimeFormatter(fn) { timeFmt = fn; for (const it of items.values()) if (isChat(it.m)) paint(it.el, it.m); },
    users: () => [...users.values()],
    rooms: () => [...rooms.values()].map((r) => ({ id: r.id, name: roomLabel(r), kind: r.kind })),
    currentRoom: () => current,
    count: () => [...rooms.values()].reduce((a, r) => a + (r.kind === "query" ? 0 : r.count), 0),
    nick: () => myNick(),
    features: () => ({ ...features }),
    irc: () => ({ ...irc }),
    start() {
      for (const g of registry) { try { g.init(Room); } catch (e) { console.error("[gimmick " + (g.id || "?") + "]", e); } }
      initSideToggle(); initJoinsToggle(); showIntro(); showEventNote(); setConn(true);
      renderPending();
      if (boot.preview) startPreview();
      else {
        sync(); startStream();
        if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
      }
      emit("ready");
    },
  };
  window.Room = Room;
})();
