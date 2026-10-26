/* Two out of three message times become dates that cannot exist. */
Room.gimmick({ id: "impossible-dates", init(R) {
  const DATES = ["SEPTEMBER 7, 2036", "??/??/????", "FEBRUARY 30, 1999", "DAY 0", "NOVEMBER 31, 2003", "—:——", "13:61", "SEPTEMBER 7, 2036"];
  R.setTimeFormatter((m) => (m.id % 3 !== 0 ? DATES[(m.id * 5) % DATES.length] : null));
} });
