/* Gabby's status card: clock, battery, and a mood chip that reacts to the room. */
Room.gimmick({ id: "status-chips", init(R) {
  const card = R.card("My status");
  card.insertAdjacentHTML("beforeend",
    '<div class="chips"><span class="chip" id="chipBattery" hidden>🔋 --</span><span class="chip" id="chipTime">🕒 --:--</span><span class="chip" id="chipMood">✨ Curious</span></div>');
  const time = card.querySelector("#chipTime"), mood = card.querySelector("#chipMood"), batt = card.querySelector("#chipBattery");
  const MOODS = { idle: "Curious", incoming: "Surprised!", sent: "Happy!", error: "Uh oh..." };
  let timer = null;
  function setMood(kind) {
    mood.textContent = "✨ " + (MOODS[kind] || MOODS.idle);
    mood.classList.add("bump"); setTimeout(() => mood.classList.remove("bump"), 250);
    clearTimeout(timer);
    if (kind !== "idle") timer = setTimeout(() => setMood("idle"), 6000);
  }
  const tick = () => { time.textContent = "🕒 " + new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); };
  tick(); setInterval(tick, 20000);
  if (navigator.getBattery) {   // only where the browser exposes it (needs a secure context in Chrome)
    navigator.getBattery().then((b) => {
      const show = () => { batt.hidden = false; batt.textContent = "🔋 " + Math.round(b.level * 100) + "%"; };
      show(); b.addEventListener("levelchange", show);
    }).catch(() => {});
  }
  R.on("incoming", () => setMood("incoming"));
  R.on("sent", () => setMood("sent"));
  R.on("send-error", () => setMood("error"));
  R.on("conn", (ok) => { if (!ok) setMood("error"); });
} });
