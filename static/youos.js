/* YouOS shared front-end helpers.
 *
 * Loaded by every page. Hydrates the shared chrome from /api/config:
 *   - #appName     -> display name
 *   - #appVersion  -> "v<version>" (single source of truth; was hardcoded
 *                     and drifted across templates/footers)
 * Also exposes YouOS.* helpers used by the draft UI (candidate rendering,
 * badges) so new capabilities style consistently. Vanilla JS, no build step.
 */
(function () {
  window.YouOS = window.YouOS || {};

  function hydrateChrome() {
    fetch("/api/config")
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d) return;
        if (d.display_name) {
          var an = document.getElementById("appName");
          if (an) an.textContent = d.display_name;
        }
        var ver = document.getElementById("appVersion");
        if (ver && d.version) ver.textContent = "v" + d.version;
      })
      .catch(function () {});
  }
  YouOS.hydrateChrome = hydrateChrome;

  // Escape text for safe insertion into HTML.
  YouOS.esc = function (s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", hydrateChrome);
  } else {
    hydrateChrome();
  }
})();
