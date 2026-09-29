# Queue-Free Print System — merged project

This folder combines:
- Frontend/UI: the uploaded `Queue-Free-Print-System-main (3).zip`
- Flask backend: the uploaded `Queue-Free-Print-System-main-main.zip`
- MySQL schema/data: `database.sql`

## Windows PowerShell

1. Open PowerShell in this folder.
2. Install dependencies:
   `py -m pip install -r requirements.txt`
3. Make sure the MySQL service is running.
4. If your MySQL root password is `1590`, the app works without extra configuration.
5. Run:
   `py app.py`
6. Open:
   `http://127.0.0.1:5000/`

## Database import

The merged `database.sql` keeps the original `users` table because the existing
orders/shops/notifications tables reference it. It also adds:
- `registrations`
- `login_accounts`

New registrations are written to all three relevant tables so the existing
order relationships continue to work while registration and login remain
separated.

For a fresh database, create `queue_free_print` first, then import `database.sql`.
Do not import the file over a live database unless you understand that the dump
contains DROP TABLE statements.


LOGIN FIX: User login now searches only the users table by normalized email, verifies users.role=user, then checks password. Admin login searches only admins. Database errors are shown separately.

## Student Notification + Browser Push System

The student Dashboard, Profile, and Recent Orders pages now load notifications directly from the database. Unread notifications show a number on the Notifications icon. Opening Notifications marks unread notifications as read and clears the badge.

Order status notifications:
- Accepted: `Your order #ORDxxx has been accepted by the shop.`
- Completed: `Your order #ORDxxx has been printed. Please collect your document.`

The home page asks the user to enable browser notifications. The project uses a Service Worker + Web Push, so supported browsers can show the notification even when the website tab is closed. The browser/OS must support Web Push and the app should run from `localhost` or HTTPS.

### Existing database
If you already have the database installed, run `notifications_push_update.sql` once. If you are importing `database.sql` from scratch, the new `push_subscriptions` table is already included.

Then install the new dependency:

`pip install -r requirements.txt`

The app automatically creates a local VAPID key pair on first start. The private key is stored in `.vapid_private.pem` and is ignored by Git. For production, set `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, and `VAPID_SUBJECT` as environment variables instead.

## Latest fixes
- Cancellation from Recent Student Orders keeps the confirmation popup, but no longer shows a misleading second "Unable to cancel" popup after cancellation.
- Estimated order cost includes one additional hidden page charge for every order, while the displayed/analyzed page count remains the actual document page count.
\n\n## Reorder Fix\n- Existing orders with a missing `uploaded_at` value are backfilled from the stored file timestamp/order date before the 7-day retention check.\n- Reorder failures now return JSON reliably and the frontend displays the server-provided error instead of a generic JSON parsing error.\n- Failed reorder transactions clean up copied files to avoid orphaned uploads.\n

## Nearby Shops – Google Maps Demo Key

This build uses the official Google Maps Platform **Maps Demo Key** path for prototyping. Google documents the Demo Key as no-cost and available without entering billing information; it is intended for testing/prototyping, not production.

1. Get a Maps Demo Key from Google Maps Platform / Google AI Studio.
2. Open `app.py`.
3. Find `GOOGLE_MAPS_API_KEY = ""`.
4. Paste the Demo Key between the quotes.
5. Run the Flask app normally.

The nearby-shops feature does **not** search Google's directory for shops. It reads only QueueFree-registered shops from the database and filters them to 1 km on the server.

Admin location setup: `Settings` → `Pick on Map` → click/drag the shop marker → `Confirm & Save Location`. Latitude/longitude are stored automatically and never need to be entered by the admin.
