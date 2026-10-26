/* sandbox.js -- a small editor for gimmicks with example snippets and a live preview. */
(() => {
  "use strict";
  const D = JSON.parse(document.getElementById("sandboxData").textContent);
  const $ = (id) => document.getElementById(id);
  const code = $("sbCode"), frame = $("sbFrame"), status = $("sbStatus");
  const say = (t, ok) => { status.textContent = t; status.className = ok ? "ok" : "bad"; };

  const EXAMPLES = {
    "Hello (log every message)": `// Runs for every message someone else sends.
Room.on("incoming", (m) => {
  console.log(m.name + " said: " + m.text);
});`,
    "Message counter card": `// A card in the side panel that counts what you've seen this visit.
const card = Room.card("Counter");
const out = document.createElement("p");
card.appendChild(out);
let n = 0;
Room.on("message", () => { n++; out.textContent = n + " messages seen"; });`,
    "Confetti when I send": `// Emoji rain after you press send.
Room.on("sent", () => {
  for (let i = 0; i < 18; i++) {
    const s = document.createElement("span");
    s.textContent = ["🎉", "✨", "💖", "⭐"][i % 4];
    s.style.cssText = "position:fixed;top:-2rem;z-index:50;pointer-events:none;font-size:1.4rem;left:" + Math.random() * 100 + "vw;transition:transform 1.6s ease-in,opacity 1.6s";
    document.body.appendChild(s);
    requestAnimationFrame(() => { s.style.transform = "translateY(105vh) rotate(" + (Math.random() * 360) + "deg)"; s.style.opacity = "0"; });
    setTimeout(() => s.remove(), 1800);
  }
});`,
    "Flash the page when @mentioned": `// A coloured flash whenever someone @mentions you.
Room.on("mention", (m) => {
  const f = document.createElement("div");
  f.style.cssText = "position:fixed;inset:0;z-index:60;pointer-events:none;background:rgba(255,200,0,.25);transition:opacity .8s";
  document.body.appendChild(f);
  setTimeout(() => { f.style.opacity = "0"; }, 60);
  setTimeout(() => f.remove(), 900);
});`,
    "Friendly times (\"5 min ago\")": `// Show how long ago a message was sent instead of the clock time.
Room.setTimeFormatter((m, d) => {
  const s = (Date.now() - d.getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return Math.round(s / 60) + " min ago";
  return null; // older: use the normal time
});`,
    "Word of the day card": `// A little card that picks a word each page load.
const words = ["serendipity", "petrichor", "halcyon", "ephemeral", "luminous"];
const c = Room.card("Word of the day");
c.appendChild(Object.assign(document.createElement("p"), { textContent: words[Math.floor(Math.random() * words.length)] }));`,
  };

  for (const name of Object.keys(EXAMPLES)) { const o = document.createElement("option"); o.value = name; o.textContent = name; $("sbExamples").appendChild(o); }
  code.value = EXAMPLES[Object.keys(EXAMPLES)[0]];
  $("sbExamples").addEventListener("change", () => { code.value = EXAMPLES[$("sbExamples").value]; run(); });
  $("sbMine").addEventListener("click", () => { code.value = D.js || "// you have no saved JS yet"; });
  code.addEventListener("keydown", (e) => {                       // Tab inserts two spaces instead of leaving the box
    if (e.key === "Tab" && !e.shiftKey) { e.preventDefault(); const a = code.selectionStart; code.setRangeText("  ", a, code.selectionEnd, "end"); }
  });

  let pending = null;
  const url = () => "/preview?theme=" + encodeURIComponent(D.theme) + "&t=" + Date.now();
  function run() { pending = code.value; frame.src = url(); say("", true); }
  frame.addEventListener("load", () => { if (pending !== null) { frame.contentWindow.postMessage({ type: "js", code: pending }, location.origin); pending = null; } });
  window.addEventListener("message", (e) => { if (e.origin === location.origin && e.data && e.data.type === "js-error") say("Your code threw an error: " + e.data.message, false); });
  $("sbRun").addEventListener("click", run);
  const send = (extra) => frame.contentWindow && frame.contentWindow.postMessage(Object.assign({ type: "fake" }, extra), location.origin);
  $("sbMsg").addEventListener("click", () => send({ text: "hello from the sandbox" }));
  $("sbMention").addEventListener("click", () => send({ text: "@" + "me look at this!", mention: true }));
  $("sbSave").addEventListener("click", async () => {
    if (code.value.length > D.max) return say("Too long (max " + D.max + " characters).", false);
    if (D.js && D.js.trim() && D.js !== code.value && !confirm("This replaces the custom JS you already saved. Continue?")) return;
    const r = await api("POST", "/api/settings", { custom_js: code.value, use_custom_js: true });
    if (r.ok) { D.js = code.value; D.use_js = true; }
    say(r.ok ? "Saved. Open the room to use it." : (r.data.error || "Could not save."), r.ok);
  });
  run();
})();
