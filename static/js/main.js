document.addEventListener("DOMContentLoaded", function () {
    var toggle = document.getElementById("navToggle");
    var mobileNav = document.getElementById("navMobile");
    if (toggle && mobileNav) {
        toggle.addEventListener("click", function () {
            mobileNav.classList.toggle("open");
        });
    }
});
