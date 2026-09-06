(function () {
    var POLL_MS = 60000;
    var bell = document.getElementById('notifBell');
    var countEl = document.getElementById('notifCount');
    var panel = document.getElementById('notifPanel');
    if (!bell || !panel) return;

    function seenIds() {
        try { return JSON.parse(localStorage.getItem('sms_seen_notifs') || '[]'); } catch (e) { return []; }
    }
    function markSeen(id) {
        var seen = seenIds();
        if (seen.indexOf(id) === -1) {
            seen.push(id);
            try { localStorage.setItem('sms_seen_notifs', JSON.stringify(seen.slice(-50))); } catch (e) {}
        }
    }
    function showToast(n) {
        var toast = document.createElement('div');
        toast.className = 'notif-toast';
        toast.innerHTML = '<strong>' + n.title.replace(/</g, '&lt;') + '</strong><span>' + n.body.replace(/</g, '&lt;') + '</span>';
        toast.addEventListener('click', function () { if (n.action_url) window.location.href = n.action_url; });
        document.body.appendChild(toast);
        requestAnimationFrame(function () { toast.classList.add('show'); });
        setTimeout(function () {
            toast.classList.remove('show');
            setTimeout(function () { toast.remove(); }, 400);
        }, 6000);
    }

    function render(notifications) {
        var seen = seenIds();
        var unseen = notifications.filter(function (n) { return seen.indexOf(n.id) === -1; });
        if (unseen.length > 0) {
            countEl.textContent = unseen.length;
            countEl.style.display = 'inline-flex';
            bell.classList.add('has-new');
        } else {
            countEl.style.display = 'none';
            bell.classList.remove('has-new');
        }
        panel.innerHTML = notifications.length
            ? notifications.map(function (n) {
                return '<div class="notif-item" data-id="' + n.id + '" data-url="' + (n.action_url || '') + '">' +
                    '<strong>' + n.title.replace(/</g, '&lt;') + '</strong><span>' + n.body.replace(/</g, '&lt;') + '</span></div>';
            }).join('')
            : '<div class="notif-empty">No notifications right now.</div>';

        panel.querySelectorAll('.notif-item').forEach(function (item) {
            item.addEventListener('click', function () {
                var url = item.dataset.url;
                if (url) window.location.href = url;
            });
        });

        // Toast only the single newest genuinely-new notification per poll.
        if (unseen.length > 0) showToast(unseen[0]);
        notifications.forEach(function (n) { markSeen(n.id); });
    }

    function poll() {
        fetch('/api/notifications').then(function (r) { return r.json(); }).then(function (data) {
            render(data.notifications || []);
        }).catch(function () {});
    }

    bell.addEventListener('click', function () {
        panel.style.display = panel.style.display === 'block' ? 'none' : 'block';
    });
    document.addEventListener('click', function (e) {
        if (!panel.contains(e.target) && e.target !== bell) panel.style.display = 'none';
    });

    poll();
    setInterval(poll, POLL_MS);
})();
