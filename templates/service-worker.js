const CACHE_NAME = "qfp-push-v2";

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", event => event.waitUntil(self.clients.claim()));

self.addEventListener("push", event => {
    let data = {};
    try {
        data = event.data ? event.data.json() : {};
    } catch (e) {
        data = { body: event.data ? event.data.text() : "You have a new print order update." };
    }

    const title = data.title || "Queue-Free Print";
    const options = {
        body: data.body || "You have a new print order update.",
        data: {
            url: data.url || "/recent-orders",
            order_id: data.order_id || null
        },
        tag: data.order_id ? "qfp-order-" + data.order_id : "qfp-notification",
        renotify: true,
        requireInteraction: false
    };

    event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", event => {
    event.notification.close();
    const target = event.notification.data && event.notification.data.url
        ? event.notification.data.url
        : "/recent-orders";

    event.waitUntil(
        clients.matchAll({type:"window", includeUncontrolled:true}).then(list => {
            for (const client of list) {
                if ("focus" in client) {
                    if ("navigate" in client) client.navigate(target);
                    return client.focus();
                }
            }
            if (clients.openWindow) return clients.openWindow(target);
        })
    );
});
