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

`tests/test_data_manifest.py` (ClickUp 86cbbq5zp) and `tests/test_data_index.py`
(ClickUp 86cbegwqg) are the exception: they mock `create_connection` and need no
database, no `API_KEY` and no admin login, so they run — and must be run —
without the suite's `conftest.py`, which requires all three. `--noconftest`
alone is not enough: `conftest.py` is also what puts `app/` on `sys.path`, so
the import path has to be supplied explicitly.

```bash
docker exec dashboard-api sh -c \
  'cd /code && PYTHONPATH=app pytest tests/test_data_manifest.py tests/test_data_index.py -q --noconftest'
```

`tests/` is **not** bind-mounted (only `app/` is — see `docker-compose.yml`),
so `docker exec dashboard-api pytest` runs the tests baked into the image.
To run the working tree's tests, either `docker cp tests/. dashboard-api:/code/tests`
or use a throwaway container:

```bash
docker run --rm --network mysql_default --env-file .env -e AUDIO_DIR=/tmp \
  -v "$PWD":/code -w /code dashboard-api-dashboard-api \
  sh -c 'PYTHONPATH=app pytest tests/test_data_index.py -q --noconftest'
```

The rest of the suite additionally needs `TEST_ADMIN_PASSWORD` — the plaintext
of `ADMIN_PASSWORD_HASH` — in the environment; it is in neither `.env` nor
`.env.example`, and without it `conftest.py`'s session-scoped `admin_token`
fixture fails every test with `401 Incorrect username or password`.

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
- **`data.py`** — Data export for Bible-API (RequireAPIKey). `GET /api/data` returns all active data with COALESCE(manual_fixes); `GET /api/data/manifest` (2026-08-30, ClickUp 86cbbq5zp) returns the *plan* of a full resync in a few kilobytes — the reference tables, `code`+`alias` of every active translation, and expected row counts per table and per translation. Bible-API's full import walks that list one translation at a time instead of downloading the 147 MB whole export, which OOM-killed the production VM on 2026-08-30 (the export was materialised in *this* process too, on the same VM). The manifest's count predicates mirror `get_data` statement for statement — they are the input of the importer's post-import verification, so a divergence would turn it into noise. `counts.per_translation` is not decoration: the importer verifies **every translation in every table it owns**, because global totals pass on compensating errors (one translation gains a hundred verses, another loses a hundred). Two consequences for this endpoint: an active translation missing from `per_translation` makes the importer refuse the resync with 502 (an unverifiable translation is a broken source), and a translation dropped from `translations` here makes the importer refuse to delete it from `cep_public` unless the operator passes `?allow_removals=1` — deactivating a translation in `cep_admin` no longer silently removes it from production. `GET /api/data/index` (2026-09-05, ClickUp 86cbegwqg) ships the RAG index of **one** translation — `translation_chunks`, `psalm_verse_mappings` and the `chunk_embeddings` of exactly one `embedding_version` — so a resync carries the index along with the text instead of a hand-made MySQL dump over an SSH tunnel. It is the one place in this service that reads the **local `cep_public`** (`PUBLIC_DB_NAME`) across databases, the `{PUBLIC_DB_NAME}.<table>` idiom `stats.py` already uses: the index is written there by Bible-API's CLIs, and a copy in `cep_admin` would be a second source of truth that nothing rebuilds. `vector` is base64 of the stored BLOB, not a list of floats (1024 JSON floats ≈ 11 KB a row, ~44 MB a translation); `dims` travels beside it, so a decoded vector is checkable against `dims * 4` bytes. `limit`/`offset` page over the embeddings only — they are ~5.6 KB a row against ~0.7 KB for a chunk — and the corpus tables come back in full on `offset=0` and as `null` (never `[]`) on later pages. A page therefore has a **byte budget, not just a row count**: the first page carries the whole corpus (5.0–7.8 MiB) on top of its embeddings, so `limit` is clipped there to `FIRST_PAGE_EMBEDDINGS = 600` and elsewhere to `PAGE_EMBEDDINGS_MAX = 2000`, keeping every page ≈ 11 MiB against the 12 MiB the importer's 2–4 GB VM can afford to parse (uncapped, `limit=2000&offset=0` measured 18.5 MiB for `syn`). The clip is reported, not silent: `limit` echoes the request, `limit_applied` is what the page may carry, and `next_offset = offset + limit_applied`, so a walk driven by `next_offset` never skips a row (measured 2026-09-05: syn 10.96 / bsb 8.63 / ubh 10.76 MiB first page, three pages and 3963 unique embeddings for a full syn walk). Both constants are arithmetic over measured row sizes — raising one without re-measuring fails `test_the_page_caps_stay_inside_the_twelve_mebibyte_budget`. Refusals are named before any selection: unknown alias 404, unknown `embedding_version`/`chunking_version`/`mapping_version` 409 with `available_versions`, unreachable index database 500 naming the database *and* `PUBLIC_DB_NAME`. The manifest's `index` block reports the same facts as aggregates (versions, per-translation counts, and `chunks_digest` — an order-independent `BIT_XOR` of per-chunk MD5s, so the importer can compare corpora in one number instead of downloading 3963 chunks); when `cep_public` is unreadable the block carries `error` and the rest of the manifest still answers, because the text resync must not stop because the index is not there yet. BM25 is deliberately not exported: it is rebuilt in-process from the chunks. One documented exception to the `MANIFEST_COUNT_SQL` mirror rule lives here: the `index` counts and `chunks_digest` do **not** narrow by `chunking_version`/`mapping_version`, because the manifest speaks for every version at once — they equal what the export ships on its default path (no version pinned), which is the path the importer uses. `index.chunking_version` / `index.mapping_version` are `null` whenever more than one version exists, and that `null` is the signal that comparing a count against a *pinned* export is no longer meaningful. Today both are single-valued (chunking 3, mapping 1), so count and export coincide exactly. `PUBLIC_DB_NAME` is the one value interpolated into SQL rather than parameterised; `config.py` validates it as a bare SQL identifier at startup and refuses to start otherwise.

**The `chunks_digest` expression (Bible-API 9b must run this verbatim over its own `cep_public`):**

```sql
SELECT translation AS t,
       BIT_XOR(CONV(SUBSTRING(MD5(CONCAT_WS('\n', canonical_id, char_count,
                    COALESCE(title,''), text)),1,16),16,10)) AS digest
FROM <db>.translation_chunks
WHERE translation IN (...)
GROUP BY translation
```

XOR is commutative, so row order and the AUTO_INCREMENT `code` (which a rebuild renumbers) cannot affect it; `COALESCE(title,'')` keeps the field position that `CONCAT_WS` would otherwise drop for a NULL title, which also means a NULL title and an empty one digest alike — deliberate, since the importer may store either. Verified on MySQL 8.4.8: the string `CONV` returns is XOR'd exactly (identical to `CAST(... AS UNSIGNED)`), and the value is independent of the connection charset (same digest from a `latin1` CLI session and the app's `utf8mb4` one). The result is an **unsigned 64-bit** number and routinely exceeds 2^63 (`syn` = 18030424974330788968), so it must not be read into a signed 64-bit type.
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
Machine-to-machine (API Key): data export (`GET /api/data`), resync plan (`GET /api/data/manifest`) and RAG index export (`GET /api/data/index`) for Bible-API import.
