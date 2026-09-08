(function () {
    var POLL_MS = 4000;
    var root = document.getElementById('waitingLounge');
    if (!root) return;

    var code = root.dataset.trackCode;
    var stepSubmitted = document.getElementById('wlStepSubmitted');
    var stepReview = document.getElementById('wlStepReview');
    var stepDone = document.getElementById('wlStepDone');
    var statusMsg = document.getElementById('wlStatusMessage');
    var successBox = document.getElementById('wlSuccessBox');
    var rejectedBox = document.getElementById('wlRejectedBox');
    var timer = null;

    function setStep(el, state) {
        // state: 'done' | 'active' | 'pending'
        el.classList.remove('wl-step-done', 'wl-step-active', 'wl-step-pending');
        el.classList.add('wl-step-' + state);
    }

    function render(data) {
        if (!data.found) {
            // Request not found yet (e.g. DB replication lag) — keep waiting silently.
            return;
        }
        if (data.status === 'approved') {
            setStep(stepSubmitted, 'done');
            setStep(stepReview, 'done');
            setStep(stepDone, 'active');
            if (statusMsg) statusMsg.textContent = 'Alhamdulillah! Your payment has been confirmed.';
            if (successBox) successBox.style.display = 'block';
            stopPolling();
        } else if (data.status === 'rejected') {
            setStep(stepSubmitted, 'done');
            setStep(stepReview, 'done');
            setStep(stepDone, 'pending');
            if (statusMsg) statusMsg.textContent = 'We could not verify this payment.';
            if (rejectedBox) rejectedBox.style.display = 'block';
            stopPolling();
        } else {
            setStep(stepSubmitted, 'done');
            setStep(stepReview, 'active');
            setStep(stepDone, 'pending');
            if (statusMsg) statusMsg.textContent = 'We are reviewing your transaction. Please keep this page open.';
        }
    }

    function poll() {
        fetch('/track/' + encodeURIComponent(code))
            .then(function (r) { return r.json(); })
            .then(render)
            .catch(function () {});
    }

    function stopPolling() {
        if (timer) { clearInterval(timer); timer = null; }
    }

    if (code) {
        poll();
        timer = setInterval(poll, POLL_MS);
        // Stop polling if the tab is hidden a long time / page is unloaded,
        // so we don't leave background timers running forever.
        window.addEventListener('beforeunload', stopPolling);
    }
})();
