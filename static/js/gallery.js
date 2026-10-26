/* gallery.js -- browse, try, copy and delete published themes. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const mk = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const me = JSON.parse(document.getElementById("galleryMe").textContent);

  function card(t) {
    const c = mk("article", "gcard");
    const sw = mk("div", "gswatch");
    if (t.colors && t.colors.length) for (const col of t.colors) { const s = mk("span"); s.style.background = col; sw.appendChild(s); }
    else sw.appendChild(mk("em", null, t.theme_name || t.theme));
    c.appendChild(sw);
    c.appendChild(mk("h3", null, t.title));
    c.appendChild(mk("p", "by", "by " + t.author + " · " + new Date(t.created).toLocaleDateString()));
    if (t.description) c.appendChild(mk("p", null, t.description));
    const tags = mk("p", "gtags");
    tags.append(mk("span", "tag off", t.theme_name || t.theme));
    if (t.gimmicks.length) tags.append(mk("span", "tag off", t.gimmicks.length + " gimmick" + (t.gimmicks.length > 1 ? "s" : "")));
    if (t.has_css) tags.append(mk("span", "tag off", "custom CSS"));
    if (t.has_js) tags.append(mk("span", "tag dis", "includes JS"));
    c.appendChild(tags);
    const row = mk("div", "acts");
    const tryIt = mk("a", "btn", "Try it"); tryIt.href = "/settings?import=" + t.id; row.appendChild(tryIt);
    const copy = mk("button", null, "Copy code"); copy.type = "button";
    copy.addEventListener("click", async () => {
      const r = await api("GET", "/api/gallery/" + t.id);
      if (!r.ok) return;
      try { await navigator.clipboard.writeText(r.data.code); copy.textContent = "Copied"; }
      catch (e) { prompt("Copy this code:", r.data.code); }
      setTimeout(() => { copy.textContent = "Copy code"; }, 1500);
    });
    row.appendChild(copy);
    if (t.mine || me.admin) {
      const del = mk("button", "danger", "Delete"); del.type = "button";
      del.addEventListener("click", async () => {
        if (!confirm("Delete \u201c" + t.title + "\u201d from the gallery?")) return;
        const r = await api("DELETE", "/api/gallery/" + t.id);
        if (!r.ok) alert(r.data.error || "Could not delete."); else load();
      });
      row.appendChild(del);
    }
    c.appendChild(row);
    return c;
  }
  async function load() {
    const r = await api("GET", "/api/gallery");
    const list = $("galleryList"); list.textContent = "";
    if (!r.ok) return;
    for (const t of r.data.themes) list.appendChild(card(t));
    $("galleryEmpty").hidden = r.data.themes.length > 0;
  }
  load();
})();
