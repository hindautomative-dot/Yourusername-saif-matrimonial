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

document.addEventListener("DOMContentLoaded", function () {
    // ---- Auto-rotating banner carousels (fade transition) ----
    document.querySelectorAll(".banner-carousel").forEach(function (carousel) {
        var slides = carousel.querySelectorAll(".banner-slide");
        if (slides.length <= 1) return;
        var idx = 0;
        var interval = parseInt(carousel.getAttribute("data-autorotate") || "4500", 10);
        setInterval(function () {
            slides[idx].classList.remove("active");
            idx = (idx + 1) % slides.length;
            slides[idx].classList.add("active");
        }, interval);
    });

    // ---- Best-effort screenshot/save deterrent on protected photos ----
    // Note: this only discourages casual right-click/long-press saving.
    // No website can technically block a phone's screenshot/screen-record function.
    document.querySelectorAll(".protected-photo img, .full-photo").forEach(function (img) {
        img.addEventListener("dragstart", function (e) { e.preventDefault(); });
        img.addEventListener("contextmenu", function (e) { e.preventDefault(); });
    });

    // ---- Simple FAQ chatbot (client-side keyword matching, no backend/AI cost) ----
    var chatFab = document.getElementById("chatbotFab");
    var chatPanel = document.getElementById("chatbotPanel");
    var chatBody = document.getElementById("chatbotBody");
    var chatForm = document.getElementById("chatbotForm");
    var chatInput = document.getElementById("chatbotInput");
    var chatClose = document.getElementById("chatbotClose");

    if (chatFab && chatPanel) {
        var FAQ = [
            { keys: ["unlock", "149", "package", "4 profile", "4profile"],
              a: "Our ₹149 package lets you pick any 4 profiles yourself (tap 'Add to package' on each one), then pay once and submit — you'll get access to exactly those 4 once approved." },
            { keys: ["price", "cost", "fee", "charge"],
              a: "Unlocking a single profile and our 4-profile package pricing are shown on the pricing section of the homepage — rates can change, so please check there for the latest." },
            { keys: ["register", "registration", "add my profile", "create profile"],
              a: "Tap 'Register Yourself' from the menu, fill in your details + payment screenshot, and our team reviews it before it goes live." },
            { keys: ["verify", "verified", "tick"],
              a: "The green tick on a photo means our team has reviewed that profile. It's not a legal guarantee — always do your own due diligence before proceeding." },
            { keys: ["photo", "more photo", "picture"],
              a: "Once you've unlocked a profile, open it and use 'Request More Photos' — our team follows up with you directly." },
            { keys: ["screenshot", "misuse", "share"],
              a: "Screenshotting or forwarding profile photos/details outside genuine matrimonial consideration isn't allowed — please respect everyone's privacy." },
            { keys: ["contact", "whatsapp", "support", "admin", "help"],
              a: "You can reach our team on WhatsApp using the button on any unlocked profile, or via the Contact page in the menu." },
            { keys: ["hobbies", "match", "recommend", "similar"],
              a: "We suggest 'You Might Also Like' profiles based on shared hobbies and similar location — you'll see this on each profile page." },
        ];
        var addMsg = function (text, who) {
            var div = document.createElement("div");
            div.className = "chatbot-msg " + who;
            div.textContent = text;
            chatBody.appendChild(div);
            chatBody.scrollTop = chatBody.scrollHeight;
        };
        var answer = function (q) {
            var lower = q.toLowerCase();
            for (var i = 0; i < FAQ.length; i++) {
                for (var j = 0; j < FAQ[i].keys.length; j++) {
                    if (lower.indexOf(FAQ[i].keys[j]) !== -1) return FAQ[i].a;
                }
            }
            return "I'm a simple FAQ helper, so I couldn't match that exactly. For anything specific, please reach our team on WhatsApp via the Contact page.";
        };
        chatFab.addEventListener("click", function () { chatPanel.classList.toggle("open"); });
        if (chatClose) chatClose.addEventListener("click", function () { chatPanel.classList.remove("open"); });
        document.querySelectorAll(".chatbot-quick button").forEach(function (btn) {
            btn.addEventListener("click", function () {
                addMsg(btn.textContent, "user");
                addMsg(answer(btn.textContent), "bot");
            });
        });
        if (chatForm) {
            chatForm.addEventListener("submit", function (e) {
                e.preventDefault();
                var val = (chatInput.value || "").trim();
                if (!val) return;
                addMsg(val, "user");
                addMsg(answer(val), "bot");
                chatInput.value = "";
            });
        }
    }
});

document.addEventListener("DOMContentLoaded", function () {
    // ---- Gentle scroll-reveal for cards/sections (purely cosmetic, degrades safely) ----
    var revealTargets = document.querySelectorAll(
        ".trust-card, .step-card, .profile-card, .pricing-card, .hero-panel, .testimonial-card, .product-card"
    );
    if ("IntersectionObserver" in window && revealTargets.length) {
        revealTargets.forEach(function (el) { el.classList.add("reveal-init"); });
        var io = new IntersectionObserver(
            function (entries) {
                entries.forEach(function (entry) {
                    if (entry.isIntersecting) {
                        entry.target.classList.add("revealed");
                        io.unobserve(entry.target);
                    }
                });
            },
            { threshold: 0.12 }
        );
        revealTargets.forEach(function (el) { io.observe(el); });
    }
});
