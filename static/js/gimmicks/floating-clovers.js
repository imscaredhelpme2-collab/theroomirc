/* Clovers and alphabet blocks drift up the background. */
Room.gimmick({ id: "floating-clovers", init(R) {
  const layer = R.background;
  if (!layer) return;
  for (let i = 0; i < 16; i++) {
    const s = document.createElement("span");
    s.className = "clover"; s.textContent = "☘";
    s.style.left = Math.random() * 100 + "%";
    s.style.fontSize = 18 + Math.random() * 38 + "px";
    s.style.animationDuration = 14 + Math.random() * 18 + "s";
    s.style.animationDelay = -Math.random() * 30 + "s";
    layer.appendChild(s);
  }
  const faces = ["A", "B", "C", "1", "2", "3", "★", "♥"];
  const paints = ["#ff6b6b", "#4dabf7", "#fcc419", "#51cf66", "#ff922b", "#cc5de8"];
  for (let i = 0; i < 9; i++) {
    const b = document.createElement("span");
    b.className = "abc"; b.textContent = faces[i % faces.length]; b.style.background = paints[i % paints.length];
    b.style.left = Math.random() * 94 + "%";
    b.style.animationDuration = 20 + Math.random() * 18 + "s";
    b.style.animationDelay = -Math.random() * 38 + "s";
    layer.appendChild(b);
  }
} });
