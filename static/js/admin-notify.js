/*
 * Works with zero setup as long as the admin has a tab open on any
 * admin page: polls /admin/api/pending-count every 15s, and the moment
 * the pending-payments count goes UP, plays a short beep and (if the
 * browser has granted permission) shows a desktop notification. This is
 * on top of, not instead of, the SMS/Email/WhatsApp alerts in
 * notify_admin() on the server side — this one needs no API keys at all.
 */
(function () {
  var POLL_MS = 15000;
  var lastCount = null;
  var badge = document.getElementById("adminPendingBadge");
  var eventsBadge = document.getElementById("dashEventsBadge");

  function beep() {
    try {
      var ctx = new (window.AudioContext || window.webkitAudioContext)();
      var o = ctx.createOscillator();
      var g = ctx.createGain();
      o.connect(g);
      g.connect(ctx.destination);
      o.type = "sine";
      o.frequency.value = 880;
      g.gain.setValueAtTime(0.15, ctx.currentTime);
      o.start();
      o.stop(ctx.currentTime + 0.18);
      setTimeout(function () {
        var o2 = ctx.createOscillator();
        o2.connect(g);
        o2.frequency.value = 1100;
        o2.start();
        o2.stop(ctx.currentTime + 0.36);
      }, 220);
    } catch (e) {
      /* Audio not available — silently skip, badge still updates */
    }
  }

  function notifyBrowser(count) {
    if (!("Notification" in window)) return;
    if (Notification.permission === "granted") {
      new Notification("New payment request", {
        body: count + " request(s) waiting for approval.",
      });
    }
  }

  function poll() {
    fetch("/admin/api/pending-count", { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) return;
        if (badge) {
          badge.textContent = data.pending;
          badge.style.display = data.pending > 0 ? "inline-block" : "none";
        }
        if (eventsBadge) {
          eventsBadge.textContent = data.unread_events;
          eventsBadge.style.display = data.unread_events > 0 ? "inline-block" : "none";
        }
        if (lastCount !== null && data.pending > lastCount) {
          beep();
          notifyBrowser(data.pending);
        }
        lastCount = data.pending;
      })
      .catch(function () { /* network hiccup — try again next tick */ });
  }

  document.addEventListener("DOMContentLoaded", function () {
    if (!document.getElementById("adminPendingBadge")) return; // not an admin page
    if ("Notification" in window && Notification.permission === "default") {
      Notification.requestPermission();
    }
    poll();
    setInterval(poll, POLL_MS);
  });
})();
