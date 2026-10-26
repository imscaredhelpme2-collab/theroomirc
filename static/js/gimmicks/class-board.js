/* The classroom board and today's schedule. */
Room.gimmick({ id: "class-board", init(R) {
  const NOTES = ["Aha! Indoor voices, everyone!", "Aha! Hands up before you speak!", "Aha! Eyes on Teacher!", "Aha! No running in the chatroom!"];
  const board = R.card("Class rules", "board");
  board.insertAdjacentHTML("beforeend",
    "<ol><li>Use your indoor voice.</li><li>No fighting in the chatroom.</li><li>We don't say things like that here.</li><li>Don't spam the chat.</li><li>Teacher is always right.</li></ol>" +
    '<p class="teachernote">Aha! Eyes on Teacher!</p>');
  const sched = R.card("Today's schedule");
  sched.insertAdjacentHTML("beforeend",
    '<ul class="schedule"><li><span class="sicon" aria-hidden="true">🎵</span>Circle time</li><li><span class="sicon" aria-hidden="true">🍎</span>Snack time</li>' +
    '<li><span class="sicon" aria-hidden="true">📚</span>Story time</li><li><span class="sicon" aria-hidden="true">🎨</span>Art &amp; crafts</li><li><span class="sicon" aria-hidden="true">😴</span>Nap time</li></ul><p class="helper"></p>');
  sched.querySelector(".helper").textContent = "Helper of the day: " + R.me.name + " ☘";
  const note = board.querySelector(".teachernote");
  let i = 0;
  setInterval(() => { i = (i + 1) % NOTES.length; note.textContent = NOTES[i]; }, 12000);
} });
