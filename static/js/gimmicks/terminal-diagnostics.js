/* Maxwell's diagnostics card. */
Room.gimmick({ id: "terminal-diagnostics", init(R) {
  const card = R.card("System", "diag");
  card.insertAdjacentHTML("beforeend",
    "<div>STATUS CODE .. 0x07</div><div>CHANNEL ..... UNKNOWN CHANNEL</div><div>PRIORITY .... UNASSIGNED</div><div>MODE ........ ONLINE</div>" +
    '<div>LINK ........ <span class="d-link">OK</span></div><div>MSG COUNT ... <span class="d-count">0</span></div><div>LAST RX ..... <span class="d-rx">--:--:--</span></div>');
  const link = card.querySelector(".d-link"), cnt = card.querySelector(".d-count"), rx = card.querySelector(".d-rx");
  R.on("conn", (ok) => { link.textContent = ok ? "OK" : "LOST"; });
  R.on("count", (n) => { cnt.textContent = String(n); });
  R.on("message", (m) => { rx.textContent = new Date(m.ts).toLocaleTimeString([], { hour12: false }); });
} });
