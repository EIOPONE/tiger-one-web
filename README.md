# Tiger One — web build (v0.1)

This is the hosted-app replatform of the original Tiger One desktop tool.
Same business rules (recipes, stock reservation, quote/order lifecycle),
now as a FastAPI service on Postgres, plus a first working slice of the
field-facing pieces: driver delivery link, GPS tracking, digital POD.

## What's in here

- `app/models.py` — the data model (Postgres-ready via SQLAlchemy;
  runs on SQLite with zero setup for local dev). Same tables as the
  desktop app's `database.py`, plus new `deliveries` / `location_pings`
  tables for tracking and POD.
- `app/crud.py` — the business logic: recipe-driven stock reservation on
  quote acceptance / order confirmation, the same rounding rules, plus
  the new delivery/POD/tracking functions.
- `app/security.py` — bcrypt password hashing (the desktop build used
  unsalted SHA-256 — this replaces it now that the app is internet-facing).
- `app/main.py` — the API: login, customers, materials, quotes, orders,
  and the driver-facing routes.
- `app/templates/delivery.html` — the driver's page: no login needed,
  just the link. Shows the order, pings GPS in the background while
  open, and captures a signature + optional photo for sign-off.
- `tests/test_business_rules.py` — the original desktop self-test suite,
  ported to pytest, plus new tests for the delivery/POD/tracking flow.
  All passing.

## Allocate stock (the manual failsafe)

Every quote and order now has an `allocate_stock` flag (defaults to `True`).
An Accepted quote or Confirmed order only reserves materials if this is
`True` — so a quote accepted for a job next week can be left un-reserved
for now, keeping stock free for what's happening in the next day or two,
then switched on when it's actually needed:

```
POST /api/quotes/{quote_id}/allocate-stock?allocate=false
POST /api/orders/{order_id}/allocate-stock?allocate=true
```

Flipping it doesn't touch the status or the lines — it just adds or
removes the reservation.

## What's NOT in here yet

- A hard stop preventing `available` stock from going negative when
  something IS allocated — the allocate_stock flag above is a manual
  failsafe for holding reservations back, not a check on whether enough
  stock actually exists. Worth deciding on before this goes live.
- A proper migrations tool (Alembic). Right now `database.py` has a small
  stopgap that adds new columns to the live Postgres database automatically
  on startup — fine for the changes made so far (all nullable, additive),
  but worth replacing before schema changes get more involved.

## Truck tracking (Traccar)

The beginnings of live tracking, built ahead of having a real Traccar
server to test against — proven with a simulated Traccar server in the
test suite, ready to go live the moment yours is up.

- **/vehicles** now has an optional Traccar device ID field per vehicle —
  this is the identifier you type into Traccar Client on that vehicle's
  tablet, linking Tiger One's vehicle record to Traccar's device record.
- A background task polls Traccar's REST API every 30 seconds and updates
  each linked vehicle's last known position — but only starts at all if
  `TRACCAR_URL`, `TRACCAR_USERNAME` and `TRACCAR_PASSWORD` are set as
  environment variables. Until they are, this is a complete no-op, not
  even a background task — nothing to break, nothing running.
- Traccar's REST API keys positions by its own internal numeric device
  id, not the friendly identifier typed into Traccar Client — the sync
  logic bridges the two via Traccar's devices list. This was the trickiest
  part to get right, and it's specifically tested for.
- A Traccar outage (or it simply not being configured yet) never breaks
  anything else — same principle as the Xero integration.

**Not built yet**: a live map showing vehicle positions (currently just
shows "last seen HH:MM" on the Vehicles page), and ETA calculation
(needs a routing API — e.g. Google Directions — fed with the live
position plus the delivery address). Natural next pieces once a real
Traccar server is up and reporting.

## Xero

One-way only — Tiger One is the source of truth for the business; Xero is
kept updated automatically purely for reconciliation on the accounts side.
Nothing flows back the other way.

- Office staff connect it at **/xero** — a "Connect to Xero" button starts
  the standard OAuth flow, then shows which organisation is connected.
- **New customers** get pushed to Xero as Contacts automatically the
  moment they're saved (matched by name first, so connecting to a Xero
  org that already has these customers doesn't create duplicates).
- **Completed orders** (i.e. actually delivered) get pushed as an
  Authorised invoice automatically the moment their status becomes
  `Completed` — idempotent, so re-saving Completed never creates a
  second invoice.
- A Xero outage or misconfiguration never blocks office work — saving a
  customer or completing an order always succeeds even if the Xero push
  fails. If a push does fail, a "Retry sync" button appears next to that
  order on the Orders page so the office can see it and retry manually.

Three environment variables needed on Render for this to work:
`XERO_CLIENT_ID`, `XERO_CLIENT_SECRET` (from the app you create at
developer.xero.com), and `XERO_REDIRECT_URI` (must be exactly
`https://<your-render-url>/xero/callback`, entered letter-for-letter in
the Xero app's settings too).

## Sales reports

**/reports** — pick any date range (defaults to the last 7 days) and see
completed (delivered) orders in that window: order count, subtotal, tax,
and total, plus the full list. "Download PDF" is the main option — a
proper branded report (KPI summary + itemised table + total), generated
with `reportlab` so it works identically on Render as on the office PC,
no Excel or any spreadsheet software needed to read it. A CSV export is
still there as a small secondary link for anyone who does want the raw
data, but PDF is the one built for actually handing someone a report.
Built as a flexible date range rather than a fixed "weekly" report so it
covers weekly, monthly, or any custom period
without needing separate buttons for each.

## Fleet, reassignment, and completion notifications

- **Vehicles** (`/vehicles`) — the fleet, added once. Scheduling and
  reassigning a delivery now picks from a dropdown instead of a driver
  typing a plate in by hand.
- **Removing a driver** (`/drivers`) is a soft-delete — it blocks their
  login and drops them off the active list, but keeps their delivery and
  vehicle-check history intact rather than deleting it.
- **Reassigning a job** — any delivery that isn't yet Delivered has a
  "Reassign" control on the Orders page to change driver and/or vehicle
  (wrong driver assigned, a truck's broken down, etc). Blocked once a
  delivery's already signed off, since that history shouldn't change
  retroactively.
- **Completion notifications** — a dismissible toast (bottom-right) on
  every office page, polling every 20 seconds for newly-signed PODs.
  Auto-clears after 15s if not dismissed. Worth knowing: it only catches
  drops signed off while an office page is already open — it doesn't
  currently remember what happened while nobody had a page open, so
  there's no catch-up notification after being away. Fine for a first
  version; a "since I was last here" version is a natural next step if
  that matters in practice.

## Daily vehicle checks

- **/driver/vehicle-check** — the driver's daily walkaround check, built
  from the official DVSA HGV walkaround checklist (the same standard
  Predrive and similar apps are built around) — grouped into Inside the
  cab / Outside the vehicle / Concrete equipment (mixer drum, water tank,
  load security), each item a Pass/Defect toggle, defect notes, and a
  signature. One per driver, per vehicle, per day.
- The driver dashboard shows a prominent reminder banner until today's
  check is done — it's a visible nudge, not a hard block on viewing jobs,
  since trapping a driver from seeing their work over a paperwork step
  felt like the wrong trade-off for a first version.
- Office sees everything submitted at **/vehicle-checks**, with any
  check that has a defect clearly flagged alongside the driver's notes.

## Driver logins, tracking and signed PODs

Drivers get their own account, separate from office logins:

- Office staff add drivers at **/drivers** — a name, a username, and a
  short PIN (4–6 digits). No password to remember.
- Drivers sign in at **/driver/login** — tap their name from a list, then
  enter their PIN on a big on-screen keypad. No typing a username.
- Once in, **/driver** shows just their own jobs as cards: project,
  customer, status, a "📍 Navigate" button that opens the site address
  directly in Google Maps, and an "Open job" button for the same POD page
  as before (signature pad, photo capture, GPS ping while it's open).
- Once a POD is signed, both the driver and the office can download it as
  a PDF (`/d/{token}/pod.pdf` for the driver/customer copy, or the "POD
  PDF" link next to the delivery on the Orders page for the office). This
  PDF is generated with `reportlab` — pure Python, so unlike the quote/order
  PDF button (which needs a local browser), this one works the same on
  Render as it does on your PC.
- The signed-off location shows as a small static map with a pin (via
  `staticmap`, using OpenStreetMap tiles — no API key needed), not raw
  coordinates. If the map image can't be generated for any reason (no
  internet reachable, tile server down), it falls back to plain text
  coordinates instead of breaking the PDF — tested and confirmed working,
  though the map image itself (the "happy path") still needs a real test
  once deployed, since this sandbox's network can't reach the tile
  servers to prove that part end-to-end.
- Scheduling a delivery from the Orders page now picks a driver from a
  dropdown (instead of typing a name) and a date, so it shows up
  correctly on that driver's dashboard.

## Deploying to Render (GitHub)

1. Push this folder to a GitHub repo.
2. On Render, create a **Postgres** database first (free tier is fine for a demo) — note its name.
3. Create a **Web Service** from that GitHub repo:
   - Build command: `pip install -r requirements-render.txt`
   - Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
   - Environment variables: `DATABASE_URL` (the Postgres "Internal Database URL" from step 2) and `SECRET_KEY` (any random string — this signs login sessions).
4. Render builds and gives you a public URL like `https://tiger-one-web.onrender.com` — reachable from any phone, tablet or PC, not just your office network.

`requirements-render.txt` pulls in `psycopg2-binary` on top of the normal
requirements — that's kept separate from `requirements.txt` because it
sometimes fails to install on Windows without extra build tools, but
installs cleanly on Render's Linux servers.

There's also a `render.yaml` in this folder — if you use Render's
"Blueprint" option instead of setting the service up by hand, it creates
the web service and the database together in one step, with `SECRET_KEY`
generated automatically.

**One thing to know for now:** uploaded photos and signatures (from the
driver POD page) are stored on the web service's own disk, which Render
wipes on every redeploy on the free tier. Fine for demoing the flow;
before drivers rely on it for real, that needs to move to a proper file
store (e.g. Render's paid persistent disk, or S3-compatible storage) —
happy to do that when you're ready to go beyond demo.

## Running it locally

**Windows — easiest way:** just double-click `RUN_TIGER_ONE_WEB.bat`. First
run it sets up its own Python environment and installs everything; every
run after that just starts the server. Leave the window open while you're
using it, and press `Ctrl+C` in that window to stop it.

**Manual way (any OS):**

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Defaults to a local SQLite file (`tiger_one.db`) with zero setup — the
first admin login is `admin` / `tigerone`, same as before.

For Postgres, first install the driver (`pip install psycopg2-binary`), then
set `DATABASE_URL` before starting, e.g.:

```bash
export DATABASE_URL="postgresql+psycopg2://user:password@host:5432/tiger_one"
```

## Running the tests

```bash
pytest tests/ -v
```

## Trying the driver page

1. Create a customer, material, product+recipe, and an order via the
   API (see `tests/test_business_rules.py` for the shapes, or hit
   `/docs` for the interactive API explorer once the server's running).
2. Confirm the order (`POST /api/orders/{id}/status?status=Confirmed`).
3. Schedule a delivery (`POST /api/orders/{id}/deliveries?driver_name=...&vehicle=...`)
   — this returns a `driver_link` like `/d/<token>`.
4. Open that link on a phone (or in a browser with location permission
   allowed) to see the driver's view, GPS pinging, and the sign-off form.
