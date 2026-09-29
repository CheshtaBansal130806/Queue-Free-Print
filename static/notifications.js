/* Queue-Free Print - unread notifications, in-page popup toast + Web Push */
(function () {
    const API = "/api/notifications";
    const READ_API = "/api/notifications/read";
    const VAPID_API = "/push/vapid-public-key";
    const SUBSCRIBE_API = "/push/subscribe";
    const SW_URL = "/service-worker.js";
    const SEEN_KEY = "qfp_seen_notification_ids";

    function ensureNotificationPanel() {
        if (document.getElementById("qfpNotificationOverlay")) return;

        const style = document.createElement("style");
        style.id = "qfpNotificationPanelStyles";
        style.textContent = `
            #qfpNotificationOverlay {
                position: fixed; inset: 0; z-index: 10000;
                background: rgba(0,0,0,.45);
                display: none; align-items: center; justify-content: center;
                padding: 20px;
            }
            #qfpNotificationOverlay.open { display: flex; }
            #qfpNotificationOverlay .qfp-notification-panel {
                width: min(520px, 100%); max-height: 75vh; overflow: hidden;
                background: #fff; border-radius: 16px; box-shadow: 0 20px 60px rgba(0,0,0,.25);
                display: flex; flex-direction: column;
            }
            #qfpNotificationOverlay .qfp-panel-head {
                display:flex; align-items:center; justify-content:space-between;
                padding:18px 20px; border-bottom:1px solid #e5e7eb;
            }
            #qfpNotificationOverlay .qfp-panel-head h3 { margin:0; font-size:20px; }
            #qfpNotificationOverlay .qfp-panel-close {
                border:0; background:transparent; font-size:28px; cursor:pointer;
                line-height:1; color:#6b7280;
            }
            #qfpNotificationList { overflow:auto; padding:10px 14px 14px; }
            #qfpNotificationList .qfp-item {
                display:flex; gap:12px; padding:14px 8px;
                border-bottom:1px solid #f0f0f0;
            }
            #qfpNotificationList .qfp-icon { font-size:22px; }
            #qfpNotificationList .qfp-content h4 { margin:0 0 5px; font-size:15px; }
            #qfpNotificationList .qfp-content p { margin:0; color:#4b5563; font-size:14px; }
            #qfpNotificationEmpty { padding:35px 20px; text-align:center; color:#6b7280; }
        `;
        document.head.appendChild(style);

        const overlay = document.createElement("div");
        overlay.id = "qfpNotificationOverlay";
        overlay.innerHTML = `
            <div class="qfp-notification-panel" role="dialog" aria-modal="true" aria-label="Notifications">
                <div class="qfp-panel-head">
                    <h3>🔔 Notifications</h3>
                    <button class="qfp-panel-close" type="button" aria-label="Close">×</button>
                </div>
                <div id="qfpNotificationList"></div>
                <div id="qfpNotificationEmpty">No notifications right now.</div>
            </div>
        `;
        document.body.appendChild(overlay);

        overlay.addEventListener("click", function (event) {
            if (event.target === overlay) window.qfpCloseNotifications();
        });
        overlay.querySelector(".qfp-panel-close").addEventListener("click", window.qfpCloseNotifications);
    }


    function esc(value) {
        return String(value ?? "").replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[c]));
    }

    function icon(type, message) {
        const text = (message || "").toLowerCase();
        if (text.includes("printed") || text.includes("collect")) return "📦";
        if (text.includes("accepted")) return "✅";
        if (text.includes("declined")) return "❌";
        if (text.includes("received")) return "📄";
        return "🔔";
    }

    function title(type, message) {
        const text = (message || "").toLowerCase();
        if (text.includes("printed") || text.includes("collect")) return "Printed — Collect Your Document";
        if (text.includes("accepted")) return "Order Accepted";
        if (text.includes("declined")) return "Order Declined";
        if (text.includes("received")) return "Order Received";
        return type ? type.charAt(0).toUpperCase() + type.slice(1) : "Notification";
    }

    function formatDate(value) {
        if (!value) return "";
        const d = new Date(value);
        if (Number.isNaN(d.getTime())) return value;
        return d.toLocaleString([], {day:"2-digit", month:"long", year:"numeric", hour:"2-digit", minute:"2-digit"});
    }

    function setBadge(count) {
        const n = Number(count || 0);
        document.querySelectorAll("#notificationBadge").forEach(badge => {
            badge.textContent = n > 99 ? "99+" : String(n);
            badge.hidden = n === 0;
        });

        // Highlight dot on the notification navigation item.
        document.querySelectorAll(".notification-link").forEach(link => {
            link.classList.toggle("has-unread", n > 0);
            link.setAttribute("data-unread", n > 0 ? String(n) : "0");
        });
    }

    function ensureToastStyles() {
        if (document.getElementById("qfpNotificationToastStyles")) return;
        const style = document.createElement("style");
        style.id = "qfpNotificationToastStyles";
        style.textContent = `
            .qfp-notification-toast {
                position: fixed;
                right: 22px;
                bottom: 22px;
                width: min(390px, calc(100vw - 30px));
                z-index: 2147483000;
                display: flex;
                align-items: flex-start;
                gap: 12px;
                padding: 15px 16px;
                background: #ffffff;
                color: #202124;
                border: 1px solid rgba(0,0,0,.10);
                border-radius: 14px;
                box-shadow: 0 12px 35px rgba(0,0,0,.20);
                cursor: pointer;
                animation: qfpToastIn .28s ease-out;
                font-family: inherit;
            }
            .qfp-notification-toast-icon {
                width: 42px;
                height: 42px;
                flex: 0 0 42px;
                display: grid;
                place-items: center;
                border-radius: 50%;
                background: #f1f3f4;
                font-size: 21px;
            }
            .qfp-notification-toast-content { min-width: 0; flex: 1; }
            .qfp-notification-toast-brand {
                margin: 0 0 3px;
                font-size: 12px;
                font-weight: 700;
                letter-spacing: .2px;
                opacity: .68;
            }
            .qfp-notification-toast-title {
                margin: 0 0 4px;
                font-size: 15px;
                line-height: 1.25;
                font-weight: 700;
            }
            .qfp-notification-toast-message {
                margin: 0;
                font-size: 13px;
                line-height: 1.4;
                opacity: .82;
            }
            .qfp-notification-toast-close {
                border: 0;
                background: transparent;
                color: inherit;
                opacity: .55;
                font-size: 18px;
                line-height: 1;
                padding: 1px 3px;
                cursor: pointer;
            }
            .qfp-notification-toast-close:hover { opacity: 1; }
            @keyframes qfpToastIn {
                from { opacity: 0; transform: translateY(18px) scale(.98); }
                to { opacity: 1; transform: translateY(0) scale(1); }
            }
            @media (max-width: 600px) {
                .qfp-notification-toast { right: 12px; bottom: 12px; width: calc(100vw - 24px); }
            }
            /* Small notification indicator on every existing navbar design. */
            .notification-link { position: relative !important; }
            .notification-badge {
                display: inline-flex; min-width: 18px; height: 18px;
                align-items: center; justify-content: center;
                margin-left: 4px; padding: 0 5px; border-radius: 999px;
                font-size: 11px; line-height: 18px; font-weight: 700;
                background: #ef4444; color: #fff; vertical-align: middle;
            }
            .notification-link.has-unread::after {
                content: "";
                position: absolute;
                width: 8px;
                height: 8px;
                border-radius: 50%;
                background: #e53935;
                top: 2px;
                right: -4px;
                border: 2px solid #fff;
                box-sizing: content-box;
            }
        `;
        document.head.appendChild(style);
    }

    function showToast(notification) {
        ensureToastStyles();

        const existing = document.getElementById("qfpNotificationToast");
        if (existing) existing.remove();

        const toast = document.createElement("div");
        toast.id = "qfpNotificationToast";
        toast.className = "qfp-notification-toast";
        toast.setAttribute("role", "status");
        toast.setAttribute("aria-live", "polite");
        toast.innerHTML = `
            <div class="qfp-notification-toast-icon">${icon(notification.notification_type, notification.message)}</div>
            <div class="qfp-notification-toast-content">
                <p class="qfp-notification-toast-brand">Queue-Free Print</p>
                <h3 class="qfp-notification-toast-title">${esc(title(notification.notification_type, notification.message))}</h3>
                <p class="qfp-notification-toast-message">${esc(notification.message)}</p>
            </div>
            <button class="qfp-notification-toast-close" type="button" aria-label="Close notification">×</button>
        `;

        toast.querySelector(".qfp-notification-toast-close").addEventListener("click", function (event) {
            event.stopPropagation();
            toast.remove();
        });

        // Clicking the toast opens the existing notification panel.
        toast.addEventListener("click", function () {
            if (window.qfpOpenNotifications) window.qfpOpenNotifications();
            toast.remove();
        });

        document.body.appendChild(toast);
        setTimeout(() => {
            if (toast.isConnected) toast.remove();
        }, 8000);
    }

    function getSeenIds() {
        try {
            const value = JSON.parse(localStorage.getItem(SEEN_KEY) || "[]");
            return new Set(Array.isArray(value) ? value.map(String) : []);
        } catch (e) { return new Set(); }
    }

    function saveSeenIds(ids) {
        try {
            const values = Array.from(ids).slice(-100);
            localStorage.setItem(SEEN_KEY, JSON.stringify(values));
        } catch (e) {}
    }

    function renderNotifications(items) {
        // The API already returns unread items only. Keep this extra filter as
        // protection so a read notification can never appear in the panel.
        items = (items || []).filter(n => Number(n.is_read) === 0);

        const lists = document.querySelectorAll("#notificationList, #qfpNotificationList");
        lists.forEach(list => {
            const empty = list.parentElement.querySelector("#noNotification, #qfpNotificationEmpty") || document.getElementById("noNotification") || document.getElementById("qfpNotificationEmpty");
            if (!items.length) {
                list.innerHTML = "";
                list.style.display = "none";
                if (empty) empty.style.display = "block";
                return;
            }
            list.style.display = "flex";
            if (empty) empty.style.display = "none";
            list.innerHTML = items.map(n => `
                <div class="notification-item qfp-item unread" data-notification-id="${esc(n.notification_id)}">
                    <div class="notification-icon qfp-icon">${icon(n.notification_type, n.message)}</div>
                    <div class="notification-content qfp-content">
                        <h3>${esc(title(n.notification_type, n.message))}</h3>
                        <p>${esc(n.message)}</p>
                        <span>${esc(formatDate(n.created_at))}</span>
                    </div>
                </div>`).join("");
        });
    }

    let firstLoad = true;

    async function loadNotifications(showNewToast = true) {
        ensureNotificationPanel();
        try {
            const response = await fetch(API, {credentials:"same-origin", cache:"no-store", headers:{"Cache-Control":"no-cache"}});
            if (!response.ok) return;
            const data = await response.json();
            if (!data.success) return;

            const isAdminPage = document.body.dataset.userRole === "admin" ||
                !!document.querySelector('body.admin-page, .admin-dashboard, [data-role="admin"]');
            let items = (data.notifications || []).filter(n => Number(n.is_read) === 0);
            // Final UI guard: admin gets only Order Received; students never get it.
            items = items.filter(n => {
                const msg = String(n.message || "").toLowerCase();
                const isReceived = /^order #\d+ received\.?/.test(msg) ||
                    msg.includes("order has been received successfully");
                return isAdminPage ? isReceived : !isReceived;
            });
            renderNotifications(items);
            setBadge(items.length);

            // Do not pop an old notification merely because the page was
            // refreshed. Popups are for newly received notifications.
            const seen = getSeenIds();
            const newItems = items.filter(n => !seen.has(String(n.notification_id)));

            if (firstLoad) {
                items.forEach(n => seen.add(String(n.notification_id)));
                saveSeenIds(seen);
                firstLoad = false;
            } else if (showNewToast && newItems.length) {
                const newest = newItems[0];
                newItems.forEach(n => seen.add(String(n.notification_id)));
                saveSeenIds(seen);
                showToast(newest);
            }
        } catch (e) {}
    }

    async function markRead() {
        try {
            const response = await fetch(READ_API, {
                method:"POST",
                credentials:"same-origin",
                headers:{"X-Requested-With":"XMLHttpRequest"}
            });
            if (!response.ok) return false;
            const data = await response.json().catch(() => ({}));
            if (data.success === false) return false;

            setBadge(0);
            document.querySelectorAll(".notification-item.unread").forEach(item => {
                item.classList.remove("unread");
                item.classList.add("read");
            });
            return true;
        } catch(e) { return false; }
    }

    window.qfpOpenNotifications = async function (event) {
        if (event) event.preventDefault();
        await loadNotifications(false);

        ensureNotificationPanel();
        const popup = document.getElementById("notificationPopup") || document.getElementById("notificationOverlay") || document.getElementById("qfpNotificationOverlay");
        if (popup) {
            popup.style.display = "flex";
            popup.classList.add("open");
            document.body.classList.add("notification-modal-open");
            document.body.style.overflow = "hidden";
        }

        // Opening the notification panel counts as reading the currently
        // displayed unread notifications. Next time only new ones remain.
        await markRead();
    };

    window.qfpCloseNotifications = function () {
        const popup = document.getElementById("notificationPopup") || document.getElementById("notificationOverlay") || document.getElementById("qfpNotificationOverlay");
        if (popup) {
            popup.style.display = "none";
            popup.classList.remove("open");
        }
        document.body.classList.remove("notification-modal-open");
        if (!document.body.classList.contains("logout-modal-open")) document.body.style.removeProperty("overflow");
    };

    async function syncPushSubscription() {
        if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window) || Notification.permission !== "granted") return;
        try {
            const reg = await navigator.serviceWorker.register(SW_URL, {scope:"/"});
            let sub = await reg.pushManager.getSubscription();
            if (!sub) {
                const keyResponse = await fetch(VAPID_API, {cache:"no-store"});
                if (!keyResponse.ok) return;
                const keyData = await keyResponse.json();
                if (!keyData.public_key) return;
                sub = await reg.pushManager.subscribe({userVisibleOnly:true, applicationServerKey:base64ToUint8Array(keyData.public_key)});
            }
            localStorage.setItem("qfp_push_subscription", JSON.stringify(sub.toJSON()));
            await fetch(SUBSCRIBE_API, {
                method:"POST",
                credentials:"same-origin",
                headers:{"Content-Type":"application/json","X-Requested-With":"XMLHttpRequest"},
                body:JSON.stringify(sub.toJSON())
            });
        } catch (e) {}
    }

    function base64ToUint8Array(base64) {
        const padding = "=".repeat((4 - base64.length % 4) % 4);
        const raw = atob((base64 + padding).replace(/-/g,"+").replace(/_/g,"/"));
        return Uint8Array.from([...raw].map(c => c.charCodeAt(0)));
    }

    window.qfpSyncPushSubscription = syncPushSubscription;
    window.qfpLoadNotifications = loadNotifications;

    document.addEventListener("DOMContentLoaded", function () {
        ensureToastStyles();
        loadNotifications(true);
        syncPushSubscription();

        // Poll while the student is on any of the three pages. A newly
        // created unread notification immediately gets a bottom-right toast.
        setInterval(function () {
            const popup = document.getElementById("notificationPopup") || document.getElementById("notificationOverlay") || document.getElementById("qfpNotificationOverlay");
            const isOpen = popup && getComputedStyle(popup).display !== "none";
            if (!isOpen) loadNotifications(true);
        }, 3000);

        // If the student returns to the tab after it was in the background,
        // immediately refresh the unread count and notification state.
        document.addEventListener("visibilitychange", function () {
            if (!document.hidden) {
                loadNotifications(true);
                syncPushSubscription();
            }
        });
    });
})();
