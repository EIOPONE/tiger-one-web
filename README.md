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

- Xero connection.
- A hard stop preventing `available` stock from going negative when
  something IS allocated — the allocate_stock flag above is a manual
  failsafe for holding reservations back, not a check on whether enough
  stock actually exists. Worth deciding on before this goes live.
- A proper migrations tool (Alembic). Right now `database.py` has a small
  stopgap that adds new columns to the live Postgres database automatically
  on startup — fine for the changes made so far (all nullable, additive),
  but worth replacing before schema changes get more involved.

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
