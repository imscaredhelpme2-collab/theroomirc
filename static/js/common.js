/* common.js -- fetch helper for the settings and admin pages */
(() => {
  "use strict";
  const token = () => document.querySelector('meta[name="csrf-token"]').content;
  window.api = async function api(method, url, body) {
    const r = await fetch(url, {
      method, credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": token() },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await r.json().catch(() => ({}));
    if (r.status === 401) location.href = "/login";
    if (r.status === 403 && data.disabled) location.href = "/disabled";
    return { ok: r.ok, status: r.status, data };
  };
})();
