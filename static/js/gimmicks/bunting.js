/* A string of pennants across the top. */
Room.gimmick({ id: "bunting", init(R) {
  const el = document.createElement("div");
  el.className = "bunting"; el.setAttribute("aria-hidden", "true"); el.textContent = "▼".repeat(80);
  R.banner(el);
} });
