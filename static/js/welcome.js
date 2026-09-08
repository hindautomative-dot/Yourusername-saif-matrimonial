// First-visit welcome overlay — Phase 3.
// Shows once per browser SESSION (sessionStorage, not localStorage — so
// it reappears on a genuinely new visit/session, not permanently hidden
// forever after the first-ever visit). Never blocks the page: it's an
// overlay on top of already-loaded content, not a gate before content
// loads, and it never re-shows on other pages within the same session.
(function () {
    "use strict";

    var STORAGE_KEY = "sm_welcome_shown";
    var AUTO_DISMISS_MS = 4000;

    if (sessionStorage.getItem(STORAGE_KEY)) return;

    var overlay = document.getElementById("welcomeOverlay");
    if (!overlay) return;

    var reducedMotion = window.matchMedia &&
        window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    function dismiss() {
        overlay.classList.remove("is-visible");
        var cleanup = function () {
            overlay.hidden = true;
        };
        if (reducedMotion) {
            cleanup();
        } else {
            setTimeout(cleanup, 220); // matches the CSS transition duration
        }
        sessionStorage.setItem(STORAGE_KEY, "1");
    }

    // Reveal on next frame so the CSS transition actually runs instead of
    // starting from an already-visible state.
    overlay.hidden = false;
    requestAnimationFrame(function () {
        requestAnimationFrame(function () {
            overlay.classList.add("is-visible");
        });
    });

    var autoTimer = setTimeout(dismiss, AUTO_DISMISS_MS);

    function manualDismiss() {
        clearTimeout(autoTimer);
        dismiss();
    }

    var closeBtn = document.getElementById("welcomeClose");
    var ctaBtn = document.getElementById("welcomeCta");
    if (closeBtn) closeBtn.addEventListener("click", manualDismiss);
    if (ctaBtn) ctaBtn.addEventListener("click", manualDismiss);
    overlay.addEventListener("click", function (e) {
        if (e.target === overlay) manualDismiss();
    });
})();
