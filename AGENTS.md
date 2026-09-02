# Repository Guidelines

## Project Structure & Module Organization

`open_bus/` contains the Django project configuration, including settings,
URLs, and ASGI/WSGI entry points. The `gtfs/` app owns transit models and
migrations. Keep services separated by responsibility under `gtfs/services/`;
the downloader and database importer live in `downloader.py` and `importer.py`.
Management commands belong in `gtfs/management/commands/`, and tests currently
live in `gtfs/tests.py`. Sample GTFS text files and archives are stored under
`etc/sample-gtfs/`. Local PostgreSQL infrastructure is defined in
`compose.yaml`.

## Build, Test, and Development Commands

Create the environment and install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Use `docker compose up -d db` to start PostgreSQL. Run
`python manage.py migrate` after model or migration changes, and start Django
with `python manage.py runserver`. Use
`python manage.py download_gtfs --once` for a single configured feed refresh;
omit `--once` to run continuously. Run all tests with
`python manage.py test` and configuration checks with `python manage.py check`.

## Coding Style & Naming Conventions

Use four-space indentation and follow PEP 8. Prefer short, cohesive modules and
explicit imports. Add type annotations to every function argument and return
value, including methods. Use `snake_case` for modules, functions, and
variables; `PascalCase` for classes; and descriptive Django model field names
matching GTFS fields where practical. Keep service classes named by purpose,
such as `GtfsDownloadService`. No formatter or linter is configured, so
preserve the surrounding style and run `git diff --check` before submitting.

## Testing Guidelines

Tests use Django's `SimpleTestCase` for isolated logic and `TestCase` for
database behavior. Name test methods `test_<expected_behavior>`. Mock network
requests; tests must not depend on live GTFS providers. Add regression tests
for hashing, transactional imports, cleanup, and failure paths. PostgreSQL must
be running for database-backed tests. There is no enforced coverage threshold,
but new behavior should include focused tests.

## Commit & Pull Request Guidelines

History follows Conventional Commit-style subjects, primarily `feat:`,
`test:`, and `chore:`. Write concise, imperative messages such as
`feat: split GTFS services`. Keep commits focused. Pull requests should explain
the change, note database or environment impacts, link relevant issues, and
include test results. Screenshots are only needed for user-visible UI changes.

## Configuration & Safety

Configure feeds and database access through environment variables such as
`GTFS_URLS`, `POSTGRES_HOST`, and `POSTGRES_PASSWORD`. Never commit production
credentials or downloaded provider data. Keep GTFS purge-and-import operations
transactional so invalid feeds cannot erase valid database contents.
