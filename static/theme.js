// Shared light/dark mode toggle for the Query, Results, and Live Flights
// pages. The theme itself is applied as early as possible by a small inline
// script in each page's <head> (before this file loads) so there's no flash
// of the wrong theme; this file only wires up the toggle button's click
// handler and keeps its icon in sync.
(function () {
    var STORAGE_KEY = 'modes_logger_theme';

    // Plain single-color line icons (no fill, no bright colors) - actual
    // color comes from the button's `color` (currentColor), set per theme
    // by --icon-color in theme.css.
    var ICON_MOON = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">' +
        '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';
    var ICON_SUN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" ' +
        'stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="4.5"/>' +
        '<path d="M12 2.5v2.3M12 19.2v2.3M2.5 12h2.3M19.2 12h2.3"/></svg>';

    function currentTheme() {
        return document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
    }

    function applyIcon(btn, theme) {
        // Show the icon for the mode you'd switch *to*.
        btn.innerHTML = theme === 'dark' ? ICON_SUN : ICON_MOON;
    }

    document.addEventListener('DOMContentLoaded', function () {
        var btn = document.getElementById('theme-toggle');
        if (!btn) return;
        applyIcon(btn, currentTheme());
        btn.addEventListener('click', function () {
            var next = currentTheme() === 'dark' ? 'light' : 'dark';
            document.documentElement.setAttribute('data-theme', next);
            try { localStorage.setItem(STORAGE_KEY, next); } catch (e) { /* ignore */ }
            applyIcon(btn, next);
        });
    });
})();
