# ICP Lead Finder

Internal B2B prospecting tool for authorized team members. Define an ideal customer profile (ICP),
search for matching companies and decision-makers, review results, save leads, revisit past
searches, and export to CSV.

**Data source:** `CONTACT_PROVIDER=mock` uses clearly labeled fictional demonstration data.
`CONTACT_PROVIDER=apollo` with `APOLLO_API_KEY` uses Apollo search previews. Search
never reveals email or phone details; see
[docs/PROVIDERS.md](docs/PROVIDERS.md).

## Architecture

The app is three containers plus an optional production proxy. PostgreSQL stays on the Docker
network and is not published to the host.

| Service    | Tech                              | Default host address                 | Local demo address            |
|------------|-----------------------------------|--------------------------------------|-------------------------------|
| `frontend` | Next.js 16, TypeScript            | http://127.0.0.1:3000                | http://127.0.0.1:3012         |
| `backend`  | FastAPI, SQLAlchemy 2, Alembic    | http://127.0.0.1:8000                | http://127.0.0.1:8012         |
| `db`       | PostgreSQL 17                     | Docker volume `postgres_data` only   | same                          |
| `caddy`    | Caddy 2 (production overlay only) | ports 80/443                         | not used                      |

The local demo on this machine uses `docker-compose.demo.yml` because ports 3000 and 8000 are
already taken by another application. Authentication is off (`AUTH_REQUIRED=false`). The dashboard
opens directly into one shared workspace. API documentation is at http://127.0.0.1:8012/docs.

## Features

- Optional team sign-in (`AUTH_REQUIRED`; off by default), no public registration, session cookies,
  CSRF protection, rate limiting.
- ICP search: industry, job titles, locations, company size, keywords, company name, provider filters.
- Contact search by full name, first name, last name, email, and phone. A full name in one field matches both name columns.
- Results table with pagination, sorting, selection, and clear verified / unverified / not-available labels.
- Save leads, view/remove saved leads, and search history with "Run again".
- Excel `.xlsx` import with preview, column mapping, duplicate detection, and an error CSV.
  See [Import a spreadsheet](#import-a-spreadsheet).
- CSV export of selected search results (`POST /api/search/export/csv`) or all saved leads
  (`GET /api/leads/export`), with spreadsheet formula-injection protection.
- Administrator user management in Settings; audit log of security and admin events.

## Prerequisites

- Docker with Docker Compose v2 (`docker compose version`)
- Git

Node and Python are **not** required on the host; everything builds and runs in containers.

## First-time setup

```bash
cp .env.example .env
sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(openssl rand -hex 24)/" .env
docker compose up -d --build
```

If port 3000 or 8000 is already in use, start the local demo instead:

```bash
docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d --build
```

That overlay publishes the frontend on http://127.0.0.1:3012 and the backend on
http://127.0.0.1:8012. Set `CORS_ALLOWED_ORIGINS` to those two origins. Details are in
[DEMO-START.md](DEMO-START.md) and [DEPLOYMENT.md](DEPLOYMENT.md).

Open http://localhost:3000. Sign-in is **off by default** (`AUTH_REQUIRED=false`): the dashboard opens
directly and everyone shares one workspace (saved leads and search history).

To require sign-in, set `AUTH_REQUIRED=true` in `.env`, run `docker compose up -d`, and create an
administrator (you'll be prompted for a password of 12+ characters):

```bash
docker compose exec backend python -m app.cli create-admin --email you@yourcompany.com --name "Your Name"
```

Administrators add team members under **Settings → Team members**.

## Everyday commands

```bash
docker compose up -d --build     # build and start (migrations run automatically)
docker compose ps                # db, backend, frontend should show "(healthy)"
docker compose logs -f backend   # follow logs (or: frontend, db)
docker compose down              # stop (database data is kept)
```

`docker compose down -v` also **deletes the database volume**. Only use it to wipe local data on purpose.

## URLs to check

| URL                                  | Expected                                                 |
|--------------------------------------|----------------------------------------------------------|
| http://localhost:3000                | Dashboard (or the sign-in page if `AUTH_REQUIRED=true`)  |
| http://localhost:3000/api/health     | `{"status":"ok","service":"backend"}`                    |
| http://localhost:8000/health/ready   | `{"status":"ok","database":"ok"}`                        |
| http://localhost:8000/docs           | Interactive API docs (disabled in production)            |

## Import a spreadsheet

Saved Leads has an **Import Leads** button. The API accepts one `.xlsx` workbook:

| Endpoint | Purpose |
| --- | --- |
| `POST /api/leads/import/preview` | Parse and classify rows. Nothing is written. |
| `POST /api/leads/import` | Save ready rows in one transaction. |
| `POST /api/leads/import/errors` | Download a CSV of rejected rows. |

Limits: 10 MB upload, 50 MB uncompressed workbook, 5,000 data rows, 50 columns. Legacy `.xls`
files are rejected. The file is read in memory and is not stored on disk. Formulas are rejected
and are not evaluated. The first worksheet is used. Recognized headers include company, first
name, last name, job title, email, phone, website, industry, employees, city, state, country,
and data source. Unknown headers can be mapped in the preview.

A row is a duplicate when it repeats an earlier row in the same file, or when this user already
has an Excel import with the same record id. Duplicates are skipped unless **Update saved leads
that match these rows** is checked. A failed commit rolls back the whole file. Invalid rows are
reported and are not saved.

The committed sample is `samples/sample-prospects.xlsx` (12 fictional prospects). Rebuild it with
`python samples/build_sample_prospects.py` from a machine that has the backend dependencies.
`samples/icp-lead-finder-30-additional-prospects.xlsx` is not in this repository.

## Tests

```bash
docker compose run --rm --build backend-test    # pytest against a separate *_test database
docker compose run --rm --build frontend-test   # TypeScript type-check + Vitest
```

Backend tests cover search validation, the mock provider, provider error handling, pagination and
result formatting, authentication and authorization, saved-lead access control, CSV export,
models and migrations, the admin CLI, and health checks.

## Database migrations

Migrations run automatically when the backend starts. Manual commands:

```bash
docker compose exec backend alembic upgrade head          # apply
docker compose exec backend alembic current               # show current revision
docker compose exec backend alembic downgrade -1          # roll back one step
```

After changing `backend/app/models.py`, generate a migration and review it before committing:

```bash
docker compose run --rm --no-deps --entrypoint alembic --user "$(id -u):$(id -g)" \
  -v "$PWD/backend/alembic/versions:/app/alembic/versions" \
  backend revision --autogenerate -m "describe the change"
```

## Configuration

All configuration comes from environment variables in `.env` (see `.env.example`):

| Variable                       | Purpose                                                                  |
|--------------------------------|--------------------------------------------------------------------------|
| `APP_ENV`                      | `development` or `production`                                            |
| `POSTGRES_DB` / `POSTGRES_USER`| Database name and user                                                   |
| `POSTGRES_PASSWORD`            | Database password (required)                                             |
| `CORS_ALLOWED_ORIGINS`         | Comma-separated trusted browser origins; also used for CSRF origin checks |
| `AUTH_REQUIRED`                | `false` (default) = no sign-in, shared workspace; `true` = team sign-in  |
| `COOKIE_SECURE`                | `true` in production (HTTPS-only cookies)                                |
| `SESSION_TTL_HOURS`            | Session lifetime (default 12)                                            |
| `CONTACT_PROVIDER`             | Empty = `mock` in development; must be set explicitly in production      |
| `LOGIN_RATE_LIMIT_PER_MINUTE`  | Per-account sign-in attempts per minute (default 5)                      |
| `SEARCH_RATE_LIMIT_PER_MINUTE` | Searches per user per minute (default 20)                                |
| `SITE_ADDRESS`                 | Production only: domain or public IP served by Caddy                     |

Production refuses to start with insecure or missing settings (wildcard or non-HTTPS origins,
`COOKIE_SECURE=false`, unset `CONTACT_PROVIDER`).

## Security notes

- **With `AUTH_REQUIRED=false` there is no access control:** anyone who can reach the site can run
  searches (and use provider credits once a real provider is connected) and view or export all saved
  leads. Untrusted-origin requests are still rejected and searches are rate-limited per client address.
- Passwords are hashed with Argon2id. Sessions are random tokens stored hashed in the database,
  sent as `HttpOnly`, `SameSite=Lax` cookies (`Secure` in production) and expire after `SESSION_TTL_HOURS`.
- State-changing requests require a CSRF token header and a trusted `Origin`.
- Every data endpoint checks the session server-side; users can only see their own saved leads and history.
- Provider credentials stay on the server; the frontend only calls this app's API.
- Health checks expose no configuration. Logs never include passwords, session tokens, or API keys.
- CSV exports prefix cells starting with `=`, `+`, `-`, `@`, tab, or carriage return with `'`, so
  phone numbers such as `+1 410-555-0100` appear as `'+1 410-555-0100` in the raw file.
- Rate limits are kept in memory per backend process (the default single-process setup).

## Documentation

- [DEMO-START.md](DEMO-START.md): local demo on ports 3012 and 8012
- [DEPLOYMENT.md](DEPLOYMENT.md): Docker setup, migrations, health checks, and production considerations
- [BACKUP-MANIFEST.md](BACKUP-MANIFEST.md): what this backup includes and what it leaves out
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md): earlier notes, including an unverified DigitalOcean example
- [docs/ENVIRONMENT_CONFIGURATION.md](docs/ENVIRONMENT_CONFIGURATION.md): environment variables and sign-in readiness
- [docs/BACKUP_AND_RESTORE.md](docs/BACKUP_AND_RESTORE.md): backup, scratch restore, and live restore
- [docs/PROVIDERS.md](docs/PROVIDERS.md): provider interface and adding Apollo.io or others

## Repository layout

```text
backend/
  app/            FastAPI app: config, models, auth, providers/, routers/, CSV export, admin CLI
  alembic/        database migrations
  tests/          pytest suite
frontend/
  src/app/        Next.js pages: login, dashboard, history, saved leads, settings
  src/components/ UI components; src/lib/ API client, criteria parsing, types
deploy/Caddyfile  production reverse proxy and security headers
docs/             deployment, backup, provider guides
scripts/          backup-db.sh
docker-compose.yml        development stack (+ test runners)
docker-compose.prod.yml   production overrides (adds Caddy, removes host ports)
```

## Troubleshooting

- **`Set POSTGRES_PASSWORD in .env`**: you skipped the first-time setup step.
- **Port already in use**: something else is on 3000 or 8000. Stop it or change the host side of `ports:`.
- **"Origin not allowed" when signing in**: open the app at an address listed in `CORS_ALLOWED_ORIGINS`
  (by default `http://localhost:3000` or `http://127.0.0.1:3000`).
- **"Service unavailable" page**: run `docker compose ps` and `docker compose logs backend`.
- **WSL cannot reach GitHub or PyPI while a VPN is connected**: disconnect the VPN, or set
  `networkingMode=mirrored` under `[wsl2]` in `%UserProfile%\.wslconfig` and run `wsl --shutdown`.
