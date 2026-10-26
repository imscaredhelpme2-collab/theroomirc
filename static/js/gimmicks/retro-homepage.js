/* A 1990s homepage as remembered in a dream: ticker, construction banner, rooms, visitor counter, room ring. */
Room.gimmick({ id: "retro-homepage", init(R) {
  const DATES = ["SEPTEMBER 7, 2036", "??/??/????", "FEBRUARY 30, 1999", "DAY 0", "NOVEMBER 31, 2003", "—:——", "13:61", "SEPTEMBER 7, 2036"];
  const COUNTERS = ["0000007", "0000001", "0000000", "0013337", "0000007", "9999999"];
  const pick = (a) => a[Math.floor(Math.random() * a.length)];

  const cons = document.createElement("div");
  cons.className = "dream-construction"; cons.setAttribute("aria-hidden", "true");
  cons.innerHTML = "<b>THIS ROOM IS UNDER CONSTRUCTION</b> <i>please excuse the hallways</i>";
  const ticker = document.createElement("div");
  ticker.className = "dream-ticker"; ticker.setAttribute("aria-hidden", "true");
  ticker.innerHTML = "<span>★ WELCOME TO MY HOMEPAGE ★ you have been here before ★ please remain seated ★ best viewed in Netscape Navigator 3.0 at 800x600 ★ the room is always open ★ sign my guestbook ★</span>";
  R.banner(cons); R.banner(ticker);

  const rooms = R.card("Rooms");
  rooms.insertAdjacentHTML("beforeend",
    '<ul class="plain"><li>Gallery <em>[corrupted]</em></li><li>Files <em>[corrupted]</em></li><li>The Endless Page <em>[∞]</em></li></ul>' +
    '<p class="dreamdate">Last updated: <span class="rh-date">SEPTEMBER 7, 2036</span></p>');
  const counter = R.card("Visitor Counter");
  counter.insertAdjacentHTML("beforeend", '<div class="hitcounter">0000007</div><p class="counternote">You are visitor number...</p>');
  const ring = R.card("Room Ring");
  ring.insertAdjacentHTML("beforeend", '<p class="ring">&lt;&lt; Prev &nbsp;|&nbsp; Random &nbsp;|&nbsp; Next &gt;&gt;</p><p class="ringnote">This room is part of the Room Ring.</p>');

  const date = rooms.querySelector(".rh-date"), hits = counter.querySelector(".hitcounter");
  setInterval(() => { date.textContent = pick(DATES); }, 6500);
  setInterval(() => { hits.textContent = pick(COUNTERS); }, 9000);
} });
