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

  // Light/dark toggle. The no-flash <head> snippet already set data-theme from
  // localStorage; here we add the floating button and let the user flip it.
  function effectiveTheme() {
    var a = document.documentElement.getAttribute("data-theme");
    return a || (window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
  }
  function setupThemeToggle() {
    if (document.querySelector(".yos-theme-toggle")) return;
    var btn = document.createElement("button");
    btn.className = "yos-theme-toggle";
    btn.type = "button";
    btn.setAttribute("aria-label", "Toggle light or dark theme");
    btn.title = "Toggle theme";
    function icon() { btn.textContent = effectiveTheme() === "dark" ? "☀" : "☾"; }
    icon();
    btn.addEventListener("click", function () {
      var next = effectiveTheme() === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      try { localStorage.setItem("youos-theme", next); } catch (e) {}
      icon();
    });
    try { window.matchMedia("(prefers-color-scheme: light)").addEventListener("change", icon); } catch (e) {}
    // Dock the toggle in the page header (next to the nav) so it never floats
    // over content / tap targets on mobile. Fall back to floating only on pages
    // without the standard chrome.
    var host = document.querySelector(".oc-nav") || document.querySelector(".oc-header");
    if (host) {
      btn.classList.add("yos-theme-toggle--inline");
      host.appendChild(btn);
    } else {
      document.body.appendChild(btn);
    }
  }
  YouOS.setupThemeToggle = setupThemeToggle;

  function init() { hydrateChrome(); setupThemeToggle(); }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
