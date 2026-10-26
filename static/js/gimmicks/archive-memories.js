/* Lucy's archived memories and gallery. */
Room.gimmick({ id: "archive-memories", init(R) {
  const MEMORIES = [
    ["DAY 5", "Last entry of the day", "Goodnight day 5..."],
    ["MEMORIES", "For future Lucy", "I remember the bouncing ball. I remember the spider plant. I remember the ferris wheel. I remember the mystery note. I remember listening to Day 1 Lucy's voice."],
    ["CALENDAR", "An event nobody remembers making", "Who names a calendar event that? Not me."],
    ["DAY 7", "Final state", "No further entries."],
  ];
  const GALLERY = [["drawing_01", "Krita"], ["flipbook_01", "FlipaClip"], ["drawing_02", "Krita"], ["flipbook_02", "FlipaClip"]];
  const mem = R.card("Archived memories");
  for (const [label, title, text] of MEMORIES) {
    const a = document.createElement("article"); a.className = "memory";
    const l = document.createElement("span"); l.className = "mlabel"; l.textContent = label;
    const t = document.createElement("strong"); t.textContent = title;
    const p = document.createElement("p"); p.textContent = text;
    a.append(l, t, p); mem.appendChild(a);
  }
  const gal = R.card("Gallery");
  const grid = document.createElement("div"); grid.className = "gallery";
  for (const [name, tool] of GALLERY) {
    const f = document.createElement("figure"); f.className = "tile";
    f.innerHTML = '<div class="tileimg"></div><figcaption></figcaption>';
    const cap = f.querySelector("figcaption");
    cap.append(name, document.createElement("br"));
    const s = document.createElement("small"); s.textContent = tool; cap.appendChild(s);
    grid.appendChild(f);
  }
  gal.appendChild(grid);
} });
