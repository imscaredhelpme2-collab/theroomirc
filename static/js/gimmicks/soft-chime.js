/* A quiet chime when someone else's message arrives; a brighter two-note chime when someone @mentions you.
   (Browsers only allow sound after you have tapped the page once.) */
Room.gimmick({ id: "soft-chime", init(R) {
  let ctx = null;
  function note(t, from, to, peak, len) {
    const o = ctx.createOscillator(), g = ctx.createGain();
    o.type = "sine"; o.frequency.setValueAtTime(from, t); o.frequency.exponentialRampToValueAtTime(to, t + len * 0.6);
    g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(peak, t + 0.02); g.gain.exponentialRampToValueAtTime(0.0001, t + len);
    o.connect(g); g.connect(ctx.destination); o.start(t); o.stop(t + len + 0.05);
  }
  R.on("incoming", (m) => {
    try {
      ctx = ctx || new (window.AudioContext || window.webkitAudioContext)();
      const t = ctx.currentTime;
      if ((m.mentions || []).includes(R.me.id)) { note(t, 660, 990, 0.1, 0.3); note(t + 0.18, 990, 1320, 0.1, 0.45); }   // you were named
      else note(t, 880, 660, 0.08, 0.4);
    } catch (e) {}
  });
} });
