document.addEventListener("DOMContentLoaded", function () {
    var toggle = document.getElementById("navToggle");
    var mobileNav = document.getElementById("navMobile");
    if (toggle && mobileNav) {
        toggle.addEventListener("click", function () {
            mobileNav.classList.toggle("open");
        });
    }

    // Premium navbar: shrink + go more translucent once the page scrolls.
    var header = document.getElementById("siteHeader");
    if (header) {
        var onScroll = function () {
            if (window.scrollY > 18) {
                header.classList.add("scrolled");
            } else {
                header.classList.remove("scrolled");
            }
        };
        window.addEventListener("scroll", onScroll, { passive: true });
        onScroll();
    }
});
