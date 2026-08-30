# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Admin API — a FastAPI service for Bible Garden data management. Works with the `cep_admin` database. Used by the Dashboard and provides data for Bible-API via `GET /api/data`.

## Common Commands

### Run / Build
```bash
docker compose up -d --build                    # Start (dev mode via compose command override)
docker logs dashboard-api -f                        # View logs
docker compose down                             # Stop
```

### Tests (run inside container)
```bash
# One-time setup: create test database cep_test
docker exec dashboard-api python tests/setup_test_db.py

# Unit tests only (safe, uses mocks, no DB writes)
docker exec dashboard-api pytest tests/ -k "not integration" -v

# All tests (safe — uses test DB cep_test)
docker exec dashboard-api pytest tests/ -v

# Single test file
docker exec dashboard-api pytest tests/test_excerpt.py -v

# Single test
docker exec dashboard-api pytest tests/test_excerpt.py::test_function_name -v
```

`tests/test_data_manifest.py` (ClickUp 86cbbq5zp) is the exception: it mocks
`create_connection` and needs no database, no `API_KEY` and no admin login, so
it runs — and must be run — without the suite's `conftest.py`, which requires
all three. `--noconftest` alone is not enough: `conftest.py` is also what puts
`app/` on `sys.path`, so the import path has to be supplied explicitly.

```bash
docker exec dashboard-api sh -c \
  'cd /code && PYTHONPATH=app pytest tests/test_data_manifest.py -q --noconftest'
```

Tests run inside the `dashboard-api` container — they depend on env vars (`API_KEY`, etc.). `conftest.py` sets `DB_NAME=cep_test` before app imports, so all tests use the test database (production DB `cep_admin` is never touched). Integration tests (`test_*_integration.py`) use `TestClient` + real test DB. Unit tests use `@patch` mocks. Re-run `setup_test_db.py` after migration or seed data changes.

### Migrations
```bash
python migrate.py migrate              # Run pending migrations
python migrate.py create "name"        # Create new migration file
python migrate.py status               # Show migration status
python migrate.py mark-executed "f.sql" # Mark as already applied
```

Migration files live in `migrations/` with naming `YYYY_MM_DD_HHMMSS_name.sql`.

### OpenAPI Spec
```bash
docker exec dashboard-api bash -c "cd /code && PYTHONPATH=app python3 extract-openapi.py app.main:app"
```

## Architecture

### Application Structure (`app/`)

- **`main.py`** — FastAPI app entry point, all admin endpoints (anomalies, translations, voices, cache), the `timed_cache` decorator, and Swagger tag ordering. Imports routers from excerpt, checks, audio, data.
- **`excerpt.py`** — Core content endpoints: `chapter_with_alignment` and `excerpt_with_alignment`. Handles flexible verse reference parsing (e.g. "jhn 3:16-17"), audio alignment with manual fix overrides, and `lru_cache` for audio file existence checks.
- **`audio.py`** — MP3 file serving with HTTP Range request support. Accepts API key via query param (for HTML `<audio>` elements that can't send headers).
- **`data.py`** — Data export for Bible-API (RequireAPIKey). `GET /api/data` returns all active data with COALESCE(manual_fixes); `GET /api/data/manifest` (2026-08-30, ClickUp 86cbbq5zp) returns the *plan* of a full resync in a few kilobytes — the reference tables, `code`+`alias` of every active translation, and expected row counts per table and per translation. Bible-API's full import walks that list one translation at a time instead of downloading the 147 MB whole export, which OOM-killed the production VM on 2026-08-30 (the export was materialised in *this* process too, on the same VM). The manifest's count predicates mirror `get_data` statement for statement — they are the input of the importer's post-import verification, so a divergence would turn it into noise. `counts.per_translation` is not decoration: the importer verifies **every translation in every table it owns**, because global totals pass on compensating errors (one translation gains a hundred verses, another loses a hundred). Two consequences for this endpoint: an active translation missing from `per_translation` makes the importer refuse the resync with 502 (an unverifiable translation is a broken source), and a translation dropped from `translations` here makes the importer refuse to delete it from `cep_public` unless the operator passes `?allow_removals=1` — deactivating a translation in `cep_admin` no longer silently removes it from production.
- **`auth.py`** — Two-level auth: static API key (`X-API-Key` header) for public GET endpoints (`RequireAPIKey`), JWT Bearer tokens for admin POST/PUT/PATCH endpoints (`RequireJWT`).
- **`models.py`** — Pydantic response/request models.
- **`database.py`** — MySQL connection factory via `create_connection()`. Returns a new connection each call; callers must close it.
- **`config.py`** — Environment variable loading. `API_KEY` and `JWT_SECRET_KEY` are required (will raise on startup if missing).
- **`checks.py`** — DB integrity check endpoints (verse counts, voice alignment validation).

### Key Patterns

**Database access** — no ORM. Raw SQL with `mysql-connector-python`. Pattern:
```python
connection = create_connection()
cursor = connection.cursor(dictionary=True)
try:
    cursor.execute(sql, params)
    results = cursor.fetchall()
    connection.commit()
finally:
    cursor.close()
    connection.close()
```

**Manual fixes override alignments** — `voice_manual_fixes` table takes priority over `voice_alignments` via `COALESCE(vmf.begin, a.begin)` in excerpt SQL queries.

**Caching** — Two mechanisms: `@timed_cache(seconds=3600)` (TTL-based dict cache in `main.py`) and `@lru_cache` (for audio file checks in `excerpt.py`). Both cleared via `POST /api/cache/clear`.

**Auth dependencies** — Use `RequireAPIKey = Depends(verify_api_key)` for public endpoints, `RequireJWT = Depends(verify_jwt_token)` for admin endpoints.

**Anomaly status workflow** — `detected` -> `confirmed`/`disproved`/`corrected`/`disproved_whisper`. Status `corrected` requires `begin`/`end` timing values. Cannot revert from `corrected` to `confirmed`/`disproved`.

### Audio File Layout
```
{AUDIO_DIR}/{translation_alias}/{voice_alias}/mp3/{book_zerofill}/{chapter_zerofill}.mp3
```
Link templates in the `voices` table use placeholders: `{book_zerofill}`, `{chapter_zerofill}`, `{chapter_zerofill3}`, `{book}`, `{chapter}`, `{book_alias}`.

### Environment

Required env vars: `API_KEY`, `JWT_SECRET_KEY`, `DB_HOST`, `DB_USER`, `DB_PASSWORD`, `DB_NAME`, `AUDIO_DIR` (host path), `MP3_FILES_PATH` (container path). See `.env.example` for full list.

### All API routes are under `/api` prefix

Public (API Key): languages, translations, books, chapter/excerpt with alignment, audio streaming.
Admin (JWT): anomaly CRUD, manual fixes, translation/voice updates, cache clear, integrity checks.
Machine-to-machine (API Key): data export (`GET /api/data`) for Bible-API import.
