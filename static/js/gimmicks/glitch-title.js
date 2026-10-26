/* The room title glitches every so often. */
Room.gimmick({ id: "glitch-title", init() {
  const title = document.getElementById("roomTitle");
  if (!title) return;
  (function loop() {
    setTimeout(() => {
      title.classList.add("glitch");
      setTimeout(() => title.classList.remove("glitch"), 350);
      loop();
    }, 7000 + Math.random() * 9000);
  })();
} });
