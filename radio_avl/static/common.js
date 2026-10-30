"use strict";
// Shared by every signed-in page: if the session ends, go to the sign-in page.
(function () {
  const orig = window.fetch.bind(window);
  window.fetch = async function (...args) {
    const r = await orig(...args);
    if (r.status === 401 && !String(args[0]).includes("/api/login")) {
      location.href = "/login?next=" + encodeURIComponent(location.pathname + location.search);
    }
    return r;
  };
})();
