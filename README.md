# ParkZen1

ParkZen1 extends the original ParkZen smart parking application with a transactional parking hierarchy, advance reservations, vehicle compatibility, memberships, loyalty points, notifications, and isolated support tickets. The original manager-confirmed YOLO workflow remains intact: occupancy is never treated as a reservation confirmation.

## Architecture

- React 19 and Vite frontend (`src/`)
- Flask JSON API and SQLite persistence (`backend/app.py`)
- Bundled Ultralytics YOLO weights (`backend/best.pt`)
- SQLite database and manager uploads are created under `backend/` at runtime and excluded from Git.
- Additive schema migrations are applied by `initialize()`; existing records and model weights are preserved.

The six included destination records are sample locations in Hyderabad. Replace their details in `DESTINATIONS` in `backend/app.py` with verified venue and parking data before production use. No initial parking counts are inserted. Customer availability appears only after a manager confirms an analysis.

## Run locally

1. Install frontend packages with `npm install`.
2. Install backend dependencies with `python -m pip install -r backend/requirements.txt`.
3. Start the API from the repository root: `python backend/app.py`.
4. In a second terminal run `npm run dev` and open the Vite URL.

The API defaults to `http://127.0.0.1:5000`; set `VITE_API_URL` to change the frontend API origin. The API allows both `http://localhost:5173` and `http://127.0.0.1:5173` by default; use comma-separated `PARKZEN_ORIGIN` values for another origin or origins. Set `PARKZEN_DB` to point at a backup or a separate test database.

On first startup a manager account is created:

- Email: `manager@parkzen.local`
- Password: `1234`

Set `PARKZEN_MANAGER_PASSWORD` before first startup to choose the initial password. Change it before deployment. Passwords are stored as Werkzeug hashes; sessions use random bearer tokens stored in SQLite. The legacy default is retained for local compatibility only and must not be used in a hosted deployment.

An administrator account is also created on first startup (`admin@parkzen.local`, initial password `1234`). Set `PARKZEN_ADMIN_PASSWORD` before first startup and change it before deployment. The admin interface provides account-directory and live availability summaries; it does not permit account role changes or destination edits.

## API overview

- `POST /api/auth/register`, `POST /api/auth/login`, `GET /api/auth/me`, `POST /api/auth/logout`
- `GET /api/destinations?q=&type=all|mall|restaurant`, `GET /api/destinations/<id>`
- `GET /api/availability/updates?since=<ISO timestamp>` returns authorized public availability changes; the customer UI polls this data every 15 seconds while viewing parking.
- `GET /api/history`, `GET/PATCH /api/profile`
- Parking structure: `GET /api/parking/<destination_id>`, `POST /api/manager/floors`
- Customer vehicles: `GET/POST /api/vehicles`
- Reservations: `GET/POST /api/reservations`, `POST /api/reservations/<id>/cancel`, `/check-in`, `/check-out`
- QR sessions: authenticated `GET /api/reservations/<id>/qr` returns a PNG encoding only the opaque booking nonce; the customer reservation page can download it or scan a QR through a supported browser camera.
- Holds and waitlists: `POST/DELETE /api/reservation-holds`, `GET/POST/DELETE /api/waitlist`, `POST /api/waitlist/<id>/claim`; released slots create expiring offers for the oldest eligible waiting customer.
- Policies, analytics, and test billing: `GET/PATCH /api/policies`, `GET /api/admin/analytics`, `POST /api/payments/<reservation_id>/test-confirm`
- Membership, loyalty, passes, and notifications: `GET /api/membership-plans`, `GET /api/memberships/me`, `POST /api/memberships/purchase`, `POST /api/memberships/renew`, `POST /api/memberships/cancel`, `GET/POST /api/passes`, `GET /api/notifications`, `POST /api/notifications/<id>/read`
- Help and feedback: `GET/POST /api/support`, `GET /api/support/<id>`, `POST /api/support/<id>/replies`
- Manager only: `POST /api/manager/analyze` (multipart image/video + destination ID), `POST /api/manager/confirm/<prediction_id>`, `GET /api/manager/predictions`
- `GET /api/health`

The bundled model was verified as a detection model with `{0: "space-empty", 1: "space-occupied"}`. Image and MP4/AVI/MOV/MKV uploads are supported. Detections are sorted spatially and mapped to the configured facility slots in floor/zone/code order; the API rejects partial coverage or a mismatch between detected and configured slots rather than publishing an arbitrary count. Confirming a mapped result writes `slots.ai_status` separately from reservation state and publishes the aggregate availability. The included parking photographs are sample assets and are not automatically presented as live availability.

## Reservation and data model

Startup creates one `B1` floor with zone `A` and twenty slots per existing destination as a safe demonstration configuration. Managers can add floors through `POST /api/manager/floors`; production operators should then configure zones, slot types, rates, operating hours, and maintenance state. Reservations use an atomic SQLite write transaction (`BEGIN IMMEDIATE`) and a server-side interval overlap check, so a frontend availability check cannot create a conflicting booking. SQLite is suitable for this prototype; a high-volume deployment should move the same constraints and transaction logic to PostgreSQL.

The QR value returned as `qr_reference` is an opaque nonce and contains no customer data. Check-in requires that nonce and changes the reservation state, preventing replay. Membership and pass activation require a configured payment provider; local demonstrations can explicitly enable the guarded test path with `PARKZEN_TEST_PAYMENTS=1` and `"payment_mode":"test"`. Parking checkout calculates membership discounts and optional loyalty-point redemption on the server. Provider-backed payment, pass consumption against reservations, and refunds still require additional integration.

## Testing

Run the backend integration tests with:

```powershell
$env:PARKZEN_DB = "$env:TEMP\parkzen-test.sqlite3"
python -m unittest discover -s backend -p "test_*.py" -v
```

The tests cover registration, vehicle management, reservation overlap prevention, QR replay protection, waitlist recovery, manager structure access, YOLO slot mapping, membership activation/renewal, pass creation, and loyalty redemption. Membership/pass test activation is enabled by the test suite only; do not enable it in production. Load testing should use a separate database and multiple concurrent HTTP clients before production deployment; no autoscaling or live camera ingestion is claimed by this repository.

## Production notes

This repository is a local application baseline, not a hardened hosted deployment. Before public deployment, configure HTTPS, a strong initial manager password, an appropriate session expiration/rotation policy, production CORS origins, database backups, and verified destination records. Account recovery is not implemented because no email delivery provider is configured.
