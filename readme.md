# Open Bus

Open Bus is a Django backend for collecting and working with public transport
data in the [General Transit Feed Specification (GTFS)](https://gtfs.org/).

The project currently provides:

- Django models for agencies, calendars, routes, shapes, stops, trips, and stop
  times
- PostgreSQL-backed persistence
- a read-only endpoint for listing imported stops
- a management command that downloads one or more GTFS ZIP feeds once or on a
  schedule
- a small sample GTFS feed in `etc/sample-gtfs`

## HTTP API

The OpenAPI schema is available at `/api/schema/`. Interactive Swagger UI is
available at `/api/docs/`.

List every imported stop with:

```text
GET /api/stops/
```

The response is a JSON array ordered by stop name and stop code, with one
representative physical stop for each stop name. Each item
contains `stop_id`, `stop_code`, `stop_name`, `stop_lat`, `stop_lon`, and
`zone_id`.

Suggest stops by name prefix with:

```text
GET /api/stops/suggest/?name=swi
```

The `name` query parameter must contain at least three characters. Matching is
case-insensitive and ignores diacritics, so `swi` also matches `Święty`.
Suggestions also contain only one representative stop for each exact name.

Request a route between two stops with:

```text
POST /api/routes/
Content-Type: application/json

{
  "from_stop_id": "STOP_A",
  "to_stop_id": "STOP_C",
  "service_date": "2026-09-04",
  "departure_time": "08:30:00",
  "hops": 2,
  "min_exchange_time": "00:02:00",
  "max_exchange_time": "00:30:00"
}
```

Both stop IDs and `departure_time` are required. `service_date` is optional and
defaults to the current date in the feed's agency timezone. It identifies the
GTFS service day, so durations beyond 24 hours remain valid. `hops` is an
optional maximum transfer count and falls back to `ROUTE_MAX_HOPS`. Exchange
times are optional GTFS-style durations and fall back to the configured
defaults. The response contains the resolved `service_date` and a `routes`
array. Each route includes its departure, arrival, total duration, walking
time, transfer count, and ordered stops. `legs` retains the transit-only view,
while `segments` gives the complete ordered journey using `transit` and `walk`
modes. Walking segments contain `distance_meters`; transit segments contain the
trip, route, line, direction, and intermediate stops.

The selected stop ID represents its stop name during routing. RAPTOR considers
every physical stop with that exact name for both the journey origin and
destination, and returned routes contain the physical stop IDs actually used.

The `transfers` array identifies the incoming and outgoing stops and lines,
total alight-to-board wait, and the walking portion of that interval.
The number of results for each transfer count is capped by
`ROUTE_MAX_ALTERNATIVES_PER_HOP`, while `ROUTE_MAX_ROUTES` caps the total and
stops the search once enough routes are found. A bounded multi-label RAPTOR
search computes arrival/transfer/walking Pareto routes, retains alternatives
within the configured absolute and relative detour limits, and uses a
revisioned in-process timetable snapshot. Trips are filtered through
`calendar.txt` and `calendar_dates.txt`; boarding and alighting restrictions
are honored. Transfers may walk between physical stops within the configured
radius and must fall within the inclusive exchange-time range.

```json
{
  "service_date": "2026-09-04",
  "routes": [
    {
      "hops": 1,
      "departure_time": "08:30:00",
      "arrival_time": "09:00:00",
      "duration": "00:30:00",
      "walking_time": "00:00:00",
      "transfers": [
        {
          "stop": {"stop_id": "STOP_B"},
          "from_stop": {"stop_id": "STOP_B"},
          "to_stop": {"stop_id": "STOP_B"},
          "arrival_time": "08:40:00",
          "departure_time": "08:45:00",
          "wait_time": "00:05:00",
          "walk_time": "00:00:00",
          "from_route_id": "route-10",
          "from_line_number": "10",
          "to_route_id": "route-20",
          "to_line_number": "20"
        }
      ],
      "legs": [
        {
          "trip_id": "trip-10-a",
          "route_id": "route-10",
          "line_number": "10",
          "line_name": "City Centre",
          "direction": "Market Square",
          "direction_id": 0,
          "from_stop": {"stop_id": "STOP_A"},
          "to_stop": {"stop_id": "STOP_B"},
          "departure_time": "08:30:00",
          "arrival_time": "08:40:00",
          "stops": [
            {"stop_id": "STOP_A"},
            {"stop_id": "STOP_B"}
          ]
        },
        {
          "trip_id": "trip-20-a",
          "route_id": "route-20",
          "line_number": "20",
          "line_name": "Station Link",
          "direction": "Central Station",
          "direction_id": 1,
          "from_stop": {"stop_id": "STOP_B"},
          "to_stop": {"stop_id": "STOP_C"},
          "departure_time": "08:45:00",
          "arrival_time": "09:00:00",
          "stops": [
            {"stop_id": "STOP_B"},
            {"stop_id": "STOP_C"}
          ]
        }
      ],
      "segments": [
        {"mode": "transit", "trip_id": "trip-10-a"},
        {"mode": "transit", "trip_id": "trip-20-a"}
      ],
      "stops": [
        {"stop_id": "STOP_A"},
        {"stop_id": "STOP_B"},
        {"stop_id": "STOP_C"}
      ]
    }
  ]
}
```

Stop and segment objects are abbreviated in this example; actual objects
contain all fields returned by `GET /api/stops/`.

## Requirements

- Python 3.12 or newer
- PostgreSQL 17, or Docker with Docker Compose

## Local setup

Create a virtual environment and install the dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Start PostgreSQL:

```bash
docker compose up -d db
```

The Compose configuration and Django both use these defaults:

| Variable | Default |
| --- | --- |
| `POSTGRES_DB` | `open_bus` |
| `POSTGRES_USER` | `open_bus` |
| `POSTGRES_PASSWORD` | `open_bus` |
| `POSTGRES_HOST` | `localhost` |
| `POSTGRES_PORT` | `5432` |

Apply the migrations and start the development server:

```bash
python manage.py migrate
python manage.py runserver
```

The Django admin is then available at <http://127.0.0.1:8000/admin/>. Create an
administrator first if you want to sign in:

```bash
python manage.py createsuperuser
```

Browser clients may call `/api/` from any origin by default. To restrict CORS
access, disable the allow-all setting and provide a comma-separated origin
list, including each origin's scheme and port:

```bash
export CORS_ALLOW_ALL_ORIGINS=false
export CORS_ALLOWED_ORIGINS="https://app.example.com,http://localhost:5173"
```

## Downloading GTFS feeds

Set `GTFS_URLS` to a comma-separated list of feed URLs. Download every feed
once with:

```bash
export GTFS_URLS="https://example.org/gtfs.zip"
python manage.py download_gtfs --once
```

Run the downloader continuously by omitting `--once`:

```bash
python manage.py download_gtfs
```

The downloader validates that each response is a ZIP archive and compares its
SHA-256 hash with the previously downloaded file. Unchanged feeds are skipped.
When a feed changes, all existing GTFS records are purged and the new archive
is imported in a single database transaction. The stored ZIP is replaced only
after a successful import, so an invalid feed leaves both the database and the
previous archive untouched. A failure from one URL does not prevent later URLs
in the same batch from being attempted. After every feed in a batch downloads
and imports successfully, ZIP archives that do not belong to the current feed
URLs are removed from the download directory.

Application settings can be changed with environment variables:

| Variable | Default | Description |
| --- | --- | --- |
| `GTFS_URLS` | empty | Comma-separated feed URLs |
| `GTFS_TEMP_DIR` | system temp directory under `open_bus_gtfs` | Download destination |
| `GTFS_DOWNLOAD_INTERVAL_SECONDS` | `3600` | Delay between continuous download batches |
| `GTFS_DOWNLOAD_TIMEOUT_SECONDS` | `60` | HTTP request timeout |
| `GTFS_LOG_LEVEL` | `INFO` | Console logging level for GTFS services, including route search progress |
| `CORS_ALLOW_ALL_ORIGINS` | `true` | Allow every origin to access `/api/` |
| `CORS_ALLOWED_ORIGINS` | empty | Comma-separated allowed origins when allow-all is disabled |
| `ROUTE_MAX_HOPS` | `1` | Maximum number of transfers considered during route selection |
| `ROUTE_MAX_ALTERNATIVES_PER_HOP` | `3` | Maximum number of route alternatives returned for each transfer count |
| `ROUTE_MAX_ROUTES` | `5` | Maximum total number of route alternatives returned |
| `ROUTE_CALCULATION_WORKERS` | available CPU threads | Deprecated compatibility setting; RAPTOR scans are single-threaded |
| `ROUTE_MIN_EXCHANGE_TIME_SECONDS` | `0` | Default minimum time allowed for a transfer |
| `ROUTE_MAX_EXCHANGE_TIME_SECONDS` | `3600` | Default maximum time allowed for a transfer |
| `ROUTE_MAX_WALK_DISTANCE_METERS` | `500` | Maximum direct walking link between stops |
| `ROUTE_WALK_SPEED_METERS_PER_SECOND` | `1.4` | Walking speed used to time footpaths |
| `ROUTE_MAX_EXTRA_TRAVEL_SECONDS` | `1800` | Maximum arrival delay for a same-hop alternative |
| `ROUTE_MAX_EXTRA_TRAVEL_RATIO` | `1.5` | Maximum duration ratio for a same-hop alternative |
| `ROUTE_MAX_LABELS_PER_STOP` | `4` | Working alternative-label limit per stop and RAPTOR round |
| `ROUTE_SNAPSHOT_CACHE_SIZE` | `2` | Date-specific routing snapshots retained per process |

## Tests

Run the test suite with:

```bash
python manage.py test
```

## Project structure

```text
open_bus/                 Django project configuration
gtfs/                     GTFS models, services, commands, and tests
etc/sample-gtfs/          Sample GTFS source files and archive
compose.yaml              Local PostgreSQL service
manage.py                 Django command-line entry point
requirements.txt          Python dependencies
```
