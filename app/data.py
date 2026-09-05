"""
Data export for Bible-API

GET /api/data[?translation=alias] — returns finalized data as JSON
GET /api/data/manifest — the small plan of a full resync (ClickUp 86cbbq5zp)
GET /api/data/index — the RAG index of one translation (ClickUp 86cbegwqg)
"""

import base64
from typing import Optional
from decimal import Decimal
from fastapi import APIRouter, HTTPException, Query
from database import create_connection
from config import PUBLIC_DB_NAME
from auth import RequireAPIKey

router = APIRouter()


def decimal_to_float(rows: list[dict]) -> list[dict]:
    """Convert Decimal fields to float for JSON serialization"""
    for row in rows:
        for key, value in row.items():
            if isinstance(value, Decimal):
                row[key] = float(value)
    return rows


# Tables the manifest counts per translation, in the order Bible-API inserts
# them. The predicates below mirror `get_data` exactly — a manifest count that
# disagreed with what the export ships would turn the importer's post-import
# verification into noise.
MANIFEST_COUNT_SQL = {
    'translation_books': """
        SELECT translation AS t, COUNT(*) AS n
        FROM translation_books
        WHERE translation IN ({ph})
        GROUP BY translation
    """,
    'translation_verses': """
        SELECT translation AS t, COUNT(*) AS n
        FROM translation_verses
        WHERE translation IN ({ph})
        GROUP BY translation
    """,
    'translation_titles': """
        SELECT tv.translation AS t, COUNT(*) AS n
        FROM translation_titles tt
        INNER JOIN translation_verses tv ON tt.before_translation_verse = tv.code
        WHERE tv.translation IN ({ph})
        GROUP BY tv.translation
    """,
    'translation_notes': """
        SELECT COALESCE(tv.translation, tv2.translation) AS t, COUNT(*) AS n
        FROM translation_notes tn
        LEFT JOIN translation_verses tv ON tn.translation_verse = tv.code
        LEFT JOIN translation_titles tt ON tn.translation_title = tt.code
        LEFT JOIN translation_verses tv2 ON tt.before_translation_verse = tv2.code
        WHERE tv.translation IN ({ph}) OR tv2.translation IN ({ph})
        GROUP BY COALESCE(tv.translation, tv2.translation)
    """,
    'voices': """
        SELECT translation AS t, COUNT(*) AS n
        FROM voices
        WHERE translation IN ({ph}) AND active = 1
        GROUP BY translation
    """,
    'voice_alignments': """
        SELECT v.translation AS t, COUNT(*) AS n
        FROM voice_alignments va
        INNER JOIN voices v ON va.voice = v.code
        WHERE v.translation IN ({ph}) AND v.active = 1
        GROUP BY v.translation
    """,
}

# The notes query is the only one whose WHERE names the code list twice.
MANIFEST_DOUBLE_PARAMS = ('translation_notes',)


# ---------------------------------------------------------------------------
# RAG index export (ClickUp 86cbegwqg)
# ---------------------------------------------------------------------------
#
# `translation_chunks`, `chunk_embeddings` and `psalm_verse_mappings` are built
# by Bible-API's own CLIs into the LOCAL `cep_public` — they never existed in
# `cep_admin` and are not copied into it here. This endpoint reads them across
# databases with the `{PUBLIC_DB_NAME}.<table>` idiom `app/stats.py` already
# uses, so the export stays a read of the one place the index is written.
#
# In all three tables the column `translation` holds the numeric translation
# **code** (`translations.code`, an INT — see the DDL in Bible-API's
# `app/chunk_cli.py`, `app/vector_index.py`, `app/versification_cli.py`), not
# the alias. The endpoint takes an alias, as `/api/data` does, resolves it to a
# code and ships the rows as stored; the response echoes both.
#
# BM25 is deliberately absent: it is rebuilt in-process from the chunks.

INDEX_TABLES = ('translation_chunks', 'psalm_verse_mappings', 'chunk_embeddings')

# One statement that touches every index table without reading a row: an empty
# table answers NULL, a missing or unreadable one raises. Built from the tuple
# above so a fourth index table cannot be added without being probed.
INDEX_PROBE_SQL = 'SELECT ' + ', '.join(
    f'(SELECT 1 FROM {{db}}.{table} LIMIT 1) AS {table}' for table in INDEX_TABLES
)

# A page has a byte budget, not just a row count. The importer parses this body
# into Python on a 2–4 GB production VM, where a parsed document costs several
# times its wire size, so the ticket caps a page at 12 MiB. Measured on the
# local index, 2026-09-05: an embedding row is ~5.6 KB on the wire (base64 of a
# 4 KiB vector plus its columns) and a translation's whole corpus — every chunk
# and Psalm mapping — is 5.0–7.8 MiB (`syn` the largest). Two caps follow:
#
#   * any page: 2000 embeddings ≈ 11.0 MiB;
#   * the first page, which also carries the corpus: 600 embeddings,
#     7.8 MiB + 600 × 5.6 KB ≈ 11.0 MiB.
#
# Without the first-page cap `limit=2000` at `offset=0` weighs 18.5 MiB — the
# measurement that produced these numbers. `limit` stays what the caller asked
# for and `limit_applied` says what the page may carry, so the clipping is
# visible rather than silent; `next_offset` is built from `limit_applied`, so a
# caller that walks by `next_offset` never skips a row.
#
# Re-measure both numbers when the corpus grows (another indexed translation, a
# new chunking version, a wider embedding model).
PAGE_EMBEDDINGS_MAX = 2000
FIRST_PAGE_EMBEDDINGS = 600

# The export statements. The manifest counts below mirror them WHERE for WHERE
# (the `MANIFEST_COUNT_SQL` rule): a count that disagreed with what the export
# ships would turn the importer's post-import verification into noise. Both
# sides are formatted with the same `{db}`; the export narrows `{ph}` to a
# single translation while the manifest names them all.
INDEX_EXPORT_SQL = {
    'translation_chunks': """
        SELECT * FROM {db}.translation_chunks
        WHERE translation IN ({ph})
    """,
    'psalm_verse_mappings': """
        SELECT * FROM {db}.psalm_verse_mappings
        WHERE translation IN ({ph})
    """,
    'chunk_embeddings': """
        SELECT * FROM {db}.chunk_embeddings
        WHERE translation IN ({ph}) AND embedding_version = %s
    """,
}

# Deterministic order, so a page boundary means the same thing on two calls.
# Natural keys, never the AUTO_INCREMENT `code`: a rebuild of the index
# renumbers `code` while `canonical_id` and the verse coordinates survive it.
INDEX_EXPORT_ORDER = {
    'translation_chunks': ' ORDER BY canonical_id',
    'psalm_verse_mappings': ' ORDER BY book_number, chapter_number, verse_number',
    'chunk_embeddings': ' ORDER BY canonical_id',
}

# One caveat to the mirror rule, and it is the only one: these counts do NOT
# narrow by `chunking_version` / `mapping_version`, because the manifest speaks
# for every version at once. They therefore equal what the export ships on its
# **default** path (no version pinned), which is the path the importer uses. If
# a caller pins `chunking_version=N` while the corpus holds more than one
# chunking version, the export ships a subset of what the manifest counted —
# and the manifest says so out loud: `index.chunking_version` is `null`
# whenever `chunking_versions` holds more than one value, so a comparison of
# the two numbers is only meaningful while that field is a number. Today it is
# 3 everywhere and 1 for mappings, i.e. count and export coincide exactly.
INDEX_COUNT_SQL = {
    'translation_chunks': """
        SELECT translation AS t, COUNT(*) AS n
        FROM {db}.translation_chunks
        WHERE translation IN ({ph})
        GROUP BY translation
    """,
    'psalm_verse_mappings': """
        SELECT translation AS t, COUNT(*) AS n
        FROM {db}.psalm_verse_mappings
        WHERE translation IN ({ph})
        GROUP BY translation
    """,
    # One row per version: the manifest reports every stored version, the
    # export ships exactly one of them (`AND embedding_version = %s` above).
    'chunk_embeddings': """
        SELECT translation AS t, embedding_version AS v, COUNT(*) AS n
        FROM {db}.chunk_embeddings
        WHERE translation IN ({ph})
        GROUP BY translation, embedding_version
    """,
}

# Order-independent digest of a translation's chunk set: XOR of the first 64
# bits of an MD5 per chunk. Two corpora agree iff every chunk agrees, whatever
# order the rows come back in and whatever the AUTO_INCREMENT codes are — so
# the importer can compare its `cep_public` with this one in a single number
# instead of downloading 3963 chunks to diff them.
INDEX_CHUNKS_DIGEST_SQL = """
    SELECT translation AS t,
           BIT_XOR(CONV(SUBSTRING(MD5(CONCAT_WS('\\n', canonical_id, char_count,
                        COALESCE(title,''), text)),1,16),16,10)) AS digest
    FROM {db}.translation_chunks
    WHERE translation IN ({ph})
    GROUP BY translation
"""

INDEX_VERSION_SQL = {
    'embedding_version': "SELECT DISTINCT embedding_version AS v FROM {db}.chunk_embeddings",
    'chunking_version': "SELECT DISTINCT chunking_version AS v FROM {db}.translation_chunks",
    'mapping_version': "SELECT DISTINCT mapping_version AS v FROM {db}.psalm_verse_mappings",
}


def _index_versions(cursor, column: str) -> list:
    """Every version value stored in the index, sorted. The refusals below and
    the manifest both answer from this list — never from a value in code."""
    cursor.execute(INDEX_VERSION_SQL[column].format(db=PUBLIC_DB_NAME))
    return sorted(row['v'] for row in cursor.fetchall())


def _public_db_error(cursor) -> Optional[str]:
    """Why the index database cannot be read, naming it and its variable.

    The index lives in another schema than the one this service connects to.
    When that schema is missing, or the DB user may not read it, the operator
    needs to know *which* schema and *which* variable configures it — the raw
    `1146 Table 'cep_public.translation_chunks' doesn't exist` says the first
    but not the second, and a permission error says neither clearly.

    All three tables are probed, in one statement: a deployment that has the
    chunks but not the Psalm map is exactly as unusable, and finding that out
    here is the difference between a named 500 and a raw `1146` from the
    middle of an export.
    """
    try:
        cursor.execute(INDEX_PROBE_SQL.format(db=PUBLIC_DB_NAME))
        cursor.fetchall()
        return None
    except Exception as e:
        return (
            f"RAG index database '{PUBLIC_DB_NAME}' (environment variable "
            f"PUBLIC_DB_NAME) is not readable by this service: {e}"
        )


def _require_public_db(cursor) -> None:
    """Same check, as the index endpoint's named 500."""
    problem = _public_db_error(cursor)
    if problem:
        raise HTTPException(status_code=500, detail=problem)


def _require_known_version(cursor, column: str, requested, param_name: str) -> None:
    """409 naming the versions that do exist — before any row is selected.

    Asking for a version nobody ever wrote is a configuration mistake on the
    caller's side, and answering it with an empty page would look exactly like
    a translation that is simply not indexed yet.
    """
    if requested is None:
        return
    available = _index_versions(cursor, column)
    if requested not in available:
        raise HTTPException(
            status_code=409,
            detail={
                'message': f"Unknown {param_name} '{requested}'",
                'parameter': param_name,
                'requested': requested,
                'available_versions': available,
            },
        )


def _single(values: list):
    """The one version in use, or None when there is no single answer.

    Zero values means an empty corpus; more than one means a migration is half
    done. Reporting either as a number would be inventing one — the full list
    travels beside this field so the caller can see which case it is.
    """
    return values[0] if len(values) == 1 else None


def _manifest_index_block(cursor, translations: list[dict]) -> dict:
    """The `index` block of the manifest: what the RAG export would ship.

    Facts only — versions, per-translation row counts and a per-translation
    digest of the chunk set. What to do about them (import, skip, refuse) is
    the importer's decision, exactly as with the text counts above.

    A deployment whose `cep_public` has no index tables at all (production
    before this feature ships) reports `error` here and **still returns the
    rest of the manifest**: the text resync must not stop because the index
    is not there yet. That is a named refusal in a field, not a silent
    default — the caller sees why the block is empty.
    """
    block = {
        'chunking_version': None,
        'chunking_versions': [],
        'mapping_version': None,
        'mapping_versions': [],
        'available_versions': [],
        'counts': {'per_translation': {}},
        'chunks_digest': {},
        'error': None,
    }

    problem = _public_db_error(cursor)
    if problem:
        block['error'] = problem
        return block

    try:
        db = PUBLIC_DB_NAME
        block['available_versions'] = _index_versions(cursor, 'embedding_version')
        block['chunking_versions'] = _index_versions(cursor, 'chunking_version')
        block['mapping_versions'] = _index_versions(cursor, 'mapping_version')
        block['chunking_version'] = _single(block['chunking_versions'])
        block['mapping_version'] = _single(block['mapping_versions'])

        if not translations:
            return block

        per_translation = {
            t['alias']: {
                'translation_chunks': 0,
                'psalm_verse_mappings': 0,
                # Zero, not missing: the importer must be able to tell "this
                # version holds no rows for this translation" from "this
                # manifest forgot to mention it".
                'chunk_embeddings': {v: 0 for v in block['available_versions']},
            }
            for t in translations
        }
        chunks_digest = {t['alias']: None for t in translations}

        codes = [t['code'] for t in translations]
        alias_by_code = {t['code']: t['alias'] for t in translations}
        placeholders = ','.join(['%s'] * len(codes))

        for table in ('translation_chunks', 'psalm_verse_mappings'):
            cursor.execute(INDEX_COUNT_SQL[table].format(db=db, ph=placeholders), codes)
            for row in cursor.fetchall():
                alias = alias_by_code.get(row['t'])
                if alias is not None:
                    per_translation[alias][table] = int(row['n'])

        cursor.execute(
            INDEX_COUNT_SQL['chunk_embeddings'].format(db=db, ph=placeholders), codes
        )
        for row in cursor.fetchall():
            alias = alias_by_code.get(row['t'])
            if alias is not None:
                per_translation[alias]['chunk_embeddings'][row['v']] = int(row['n'])

        cursor.execute(INDEX_CHUNKS_DIGEST_SQL.format(db=db, ph=placeholders), codes)
        for row in cursor.fetchall():
            alias = alias_by_code.get(row['t'])
            if alias is not None and row['digest'] is not None:
                chunks_digest[alias] = int(row['digest'])

        block['counts']['per_translation'] = per_translation
        block['chunks_digest'] = chunks_digest
        return block

    except Exception as e:
        block['error'] = f"RAG index manifest failed: {e}"
        return block


@router.get('/data/manifest', operation_id="getDataManifest", tags=["Data"])
def get_data_manifest(api_key: bool = RequireAPIKey):
    """
    The plan of a full resync, small enough to hold in memory.

    Bible-API's full import used to be one 147 MB `GET /api/data`; on
    2026-08-30 parsing it OOM-killed the production VM. The import now walks
    the translations one at a time, and this endpoint is what tells it which
    ones there are and how many rows each must end up with:

    - `languages` / `bible_books` — the reference tables in full (69 rows),
      so the importer can write them before any translation;
    - `translations` — `code` and `alias` of every ACTIVE translation, the
      work list (and, by omission, the list of translations to drop);
    - `counts.per_translation` / `counts.totals` — expected row counts per
      table, the input of the importer's post-import verification.
    - `index` (ClickUp 86cbegwqg) — the same report for the RAG index that
      `GET /api/data/index` ships: the versions in use, row counts per
      translation (embeddings broken down by version) and `chunks_digest`,
      an order-independent digest of each translation's chunk set. It lets
      the importer see, before downloading ~29 MiB a translation, which
      translations are indexed at all and which ones its own `cep_public`
      already holds unchanged.

    Counting is cheap (aggregates over indexed columns, no row transfer);
    the response is a few kilobytes.
    """
    connection = create_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        cursor.execute("SELECT * FROM languages")
        languages = decimal_to_float(cursor.fetchall())

        cursor.execute("SELECT * FROM bible_books")
        bible_books = decimal_to_float(cursor.fetchall())

        cursor.execute("SELECT code, alias FROM translations WHERE active = 1 ORDER BY code")
        translations = cursor.fetchall()

        per_translation = {
            t['alias']: {'translations': 1} for t in translations
        }
        totals = {
            'languages': len(languages),
            'bible_books': len(bible_books),
            'translations': len(translations),
        }
        for table in MANIFEST_COUNT_SQL:
            totals[table] = 0
            for alias_counts in per_translation.values():
                alias_counts[table] = 0

        if translations:
            codes = [t['code'] for t in translations]
            alias_by_code = {t['code']: t['alias'] for t in translations}
            placeholders = ','.join(['%s'] * len(codes))

            for table, sql in MANIFEST_COUNT_SQL.items():
                params = codes * 2 if table in MANIFEST_DOUBLE_PARAMS else codes
                cursor.execute(sql.format(ph=placeholders), params)
                for row in cursor.fetchall():
                    alias = alias_by_code.get(row['t'])
                    if alias is None:
                        # Can only happen if a row's translation vanished
                        # between the two queries; counting it in the totals
                        # but not per translation would make the importer's
                        # verification fail with no way to see why.
                        continue
                    per_translation[alias][table] = int(row['n'])
                    totals[table] += int(row['n'])

        return {
            'languages': languages,
            'bible_books': bible_books,
            'translations': translations,
            'counts': {
                'per_translation': per_translation,
                'totals': totals,
            },
            'index': _manifest_index_block(cursor, translations),
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Data manifest failed: {str(e)}")
    finally:
        cursor.close()
        connection.close()


@router.get('/data/index', operation_id="getDataIndex", tags=["Data"])
def get_data_index(
    translation: str = Query(..., description="Translation alias (required)"),
    embedding_version: str = Query(
        ...,
        description="Index version of the embeddings to ship, e.g. "
                    "'c3:BAAI/bge-m3@1024'. Required: shipping two versions at "
                    "once would double the body, and picking one in code would "
                    "hide which index production ended up with.",
    ),
    chunking_version: Optional[int] = Query(
        None, description="Pin the chunk set to this chunking version (default: no filter)"
    ),
    mapping_version: Optional[int] = Query(
        None, description="Pin the Psalm map to this mapping version (default: no filter)"
    ),
    limit: int = Query(
        2000, ge=1, le=10000,
        description="Embedding rows per page. Clipped to the page's byte "
                    "budget — 600 on the first page, which also carries the "
                    "corpus, 2000 afterwards; the body reports the effective "
                    "value as `limit_applied` and `next_offset` follows it.",
    ),
    offset: int = Query(0, ge=0, description="Embedding rows to skip"),
    api_key: bool = RequireAPIKey,
):
    """
    The RAG index of one translation, so a production resync carries the
    index along with the text instead of a hand-made dump (ClickUp 86cbegwqg).

    Read straight out of the local `cep_public` (`PUBLIC_DB_NAME`), where
    Bible-API's CLIs write it; nothing is copied into `cep_admin`. BM25 is not
    exported — it is rebuilt in-process from the chunks.

    **Pagination covers `chunk_embeddings` only.** They are ~5.6 KB per row
    (base64 of a 4 KiB vector) against ~0.7 KB for a chunk and ~0.1 KB for a
    Psalm mapping, so they alone decide the body size: one translation is
    ~29 MiB whole. `translation_chunks` and `psalm_verse_mappings` come back
    **in full on the first page (`offset=0`) and as `null` on every later
    page** — `null`, not `[]`, so that "this page does not carry them" cannot
    be read as "this translation has none". The alternative (an offset per
    table) would make the caller track three cursors to save one round of
    ~3 MB.

    Because the first page carries that corpus (5.0–7.8 MiB) on top of its
    embeddings, `limit` is clipped there to 600 rows and elsewhere to 2000, so
    that **no page exceeds ~11 MiB** on a production VM that parses it into
    Python. The clipping is in the body, not silent: `limit` echoes what was
    asked, `limit_applied` is what the page may carry, and `next_offset`
    follows `limit_applied` — walking by `next_offset` never skips a row.

    `vector` is the stored BLOB in **base64**, not a list of floats: the bytes
    are what the reader mmaps, base64 costs 33% against ~600% for JSON floats,
    and a float round-trip could not promise byte-identity. `dims` travels in
    the same row, so a decoded vector can be checked against `dims * 4` bytes.

    Refusals name their reason before anything is selected: unknown alias 404,
    unknown version 409 with `available_versions`, unreachable index database
    500 naming the database and `PUBLIC_DB_NAME`.
    """
    connection = create_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        db = PUBLIC_DB_NAME
        _require_public_db(cursor)

        # Same predicate as `/api/data`: an inactive translation is not
        # exported, and its index would have nowhere to land.
        cursor.execute(
            "SELECT code, alias FROM translations WHERE alias = %s AND active = 1",
            (translation,),
        )
        rows = cursor.fetchall()
        if not rows:
            raise HTTPException(
                status_code=404,
                detail=f"Translation '{translation}' not found or not active",
            )
        translation_code = rows[0]['code']

        limit_applied = min(
            limit,
            FIRST_PAGE_EMBEDDINGS if offset == 0 else PAGE_EMBEDDINGS_MAX,
        )

        _require_known_version(cursor, 'embedding_version', embedding_version,
                               'embedding_version')
        _require_known_version(cursor, 'chunking_version', chunking_version,
                               'chunking_version')
        _require_known_version(cursor, 'mapping_version', mapping_version,
                               'mapping_version')

        result = {
            'translation': translation,
            'translation_code': translation_code,
            'embedding_version': embedding_version,
            'chunking_version': chunking_version,
            'mapping_version': mapping_version,
            'limit': limit,
            # What this page may actually carry: the first page spends most of
            # its budget on the corpus (see PAGE_EMBEDDINGS_MAX above), so a
            # `limit` bigger than the cap is clipped — visibly, in the body.
            'limit_applied': limit_applied,
            'offset': offset,
        }

        # How many embedding rows this page is a window into — so `next_offset`
        # is exact and the caller never fetches an empty last page. This is the
        # manifest's own count statement, narrowed to the requested version:
        # the number the importer verifies against is the number that decided
        # the paging, by construction.
        cursor.execute(
            INDEX_COUNT_SQL['chunk_embeddings'].format(db=db, ph='%s')
            + ' HAVING v = %s',
            (translation_code, embedding_version),
        )
        counted = cursor.fetchall()
        total = int(counted[0]['n']) if counted else 0
        result['chunk_embeddings_total'] = total

        if offset == 0:
            sql = INDEX_EXPORT_SQL['translation_chunks'].format(db=db, ph='%s')
            params = [translation_code]
            if chunking_version is not None:
                sql += ' AND chunking_version = %s'
                params.append(chunking_version)
            cursor.execute(sql + INDEX_EXPORT_ORDER['translation_chunks'], params)
            result['translation_chunks'] = decimal_to_float(cursor.fetchall())

            sql = INDEX_EXPORT_SQL['psalm_verse_mappings'].format(db=db, ph='%s')
            params = [translation_code]
            if mapping_version is not None:
                sql += ' AND mapping_version = %s'
                params.append(mapping_version)
            cursor.execute(sql + INDEX_EXPORT_ORDER['psalm_verse_mappings'], params)
            result['psalm_verse_mappings'] = decimal_to_float(cursor.fetchall())
        else:
            result['translation_chunks'] = None
            result['psalm_verse_mappings'] = None

        cursor.execute(
            INDEX_EXPORT_SQL['chunk_embeddings'].format(db=db, ph='%s')
            + INDEX_EXPORT_ORDER['chunk_embeddings']
            + ' LIMIT %s OFFSET %s',
            (translation_code, embedding_version, limit_applied, offset),
        )
        embeddings = cursor.fetchall()
        for row in embeddings:
            row['vector'] = base64.b64encode(row['vector']).decode('ascii')
        result['chunk_embeddings'] = embeddings

        next_offset = offset + limit_applied
        result['next_offset'] = next_offset if next_offset < total else None

        return result

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Index export failed: {str(e)}")
    finally:
        cursor.close()
        connection.close()


@router.get('/data', operation_id="getData", tags=["Data"])
def get_data(
    translation: Optional[str] = Query(None, description="Translation alias (optional)"),
    api_key: bool = RequireAPIKey
):
    """
    Data export for Bible-API

    Without parameter: all active translations + voices + voice_alignments with COALESCE
    With parameter: data for a single translation
    """
    connection = create_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        result = {}

        # Reference tables — always all
        cursor.execute("SELECT * FROM languages")
        result['languages'] = cursor.fetchall()

        cursor.execute("SELECT * FROM bible_books")
        result['bible_books'] = cursor.fetchall()

        if translation:
            # Single translation data
            cursor.execute(
                "SELECT * FROM translations WHERE alias = %s AND active = 1",
                (translation,)
            )
            translations = cursor.fetchall()
            if not translations:
                raise HTTPException(status_code=404, detail=f"Translation '{translation}' not found or not active")

            translation_code = translations[0]['code']
            result['translations'] = translations

            # translation_books
            cursor.execute(
                "SELECT * FROM translation_books WHERE translation = %s",
                (translation_code,)
            )
            result['translation_books'] = cursor.fetchall()

            # translation_verses
            cursor.execute(
                "SELECT * FROM translation_verses WHERE translation = %s",
                (translation_code,)
            )
            result['translation_verses'] = cursor.fetchall()

            # translation_titles — via verse codes
            cursor.execute("""
                SELECT tt.* FROM translation_titles tt
                INNER JOIN translation_verses tv ON tt.before_translation_verse = tv.code
                WHERE tv.translation = %s
            """, (translation_code,))
            result['translation_titles'] = cursor.fetchall()

            # translation_notes — via verse codes and title codes
            cursor.execute("""
                SELECT tn.* FROM translation_notes tn
                LEFT JOIN translation_verses tv ON tn.translation_verse = tv.code
                LEFT JOIN translation_titles tt ON tn.translation_title = tt.code
                LEFT JOIN translation_verses tv2 ON tt.before_translation_verse = tv2.code
                WHERE tv.translation = %s OR tv2.translation = %s
            """, (translation_code, translation_code))
            result['translation_notes'] = cursor.fetchall()

            # voices — active for this translation
            cursor.execute(
                "SELECT * FROM voices WHERE translation = %s AND active = 1",
                (translation_code,)
            )
            result['voices'] = cursor.fetchall()

            # voice_alignments with COALESCE (manual fixes applied)
            cursor.execute("""
                SELECT
                    va.code, va.voice, va.translation_verse, va.book_number,
                    va.chapter_number, va.verse_number,
                    COALESCE(vmf.begin, va.begin) AS `begin`,
                    COALESCE(vmf.end, va.end) AS `end`,
                    va.is_correct
                FROM voice_alignments va
                INNER JOIN voices v ON va.voice = v.code
                LEFT JOIN voice_manual_fixes vmf ON (
                    vmf.voice = va.voice AND
                    vmf.book_number = va.book_number AND
                    vmf.chapter_number = va.chapter_number AND
                    vmf.verse_number = va.verse_number
                )
                WHERE v.translation = %s AND v.active = 1
            """, (translation_code,))
            result['voice_alignments'] = decimal_to_float(cursor.fetchall())

        else:
            # All active data

            # translations — active only
            cursor.execute("SELECT * FROM translations WHERE active = 1")
            result['translations'] = cursor.fetchall()

            translation_codes = [t['code'] for t in result['translations']]
            if not translation_codes:
                result['translation_books'] = []
                result['translation_verses'] = []
                result['translation_titles'] = []
                result['translation_notes'] = []
                result['voices'] = []
                result['voice_alignments'] = []
                return result

            placeholders = ','.join(['%s'] * len(translation_codes))

            # translation_books
            cursor.execute(
                f"SELECT * FROM translation_books WHERE translation IN ({placeholders})",
                translation_codes
            )
            result['translation_books'] = cursor.fetchall()

            # translation_verses
            cursor.execute(
                f"SELECT * FROM translation_verses WHERE translation IN ({placeholders})",
                translation_codes
            )
            result['translation_verses'] = cursor.fetchall()

            # translation_titles
            cursor.execute(f"""
                SELECT tt.* FROM translation_titles tt
                INNER JOIN translation_verses tv ON tt.before_translation_verse = tv.code
                WHERE tv.translation IN ({placeholders})
            """, translation_codes)
            result['translation_titles'] = cursor.fetchall()

            # translation_notes
            cursor.execute(f"""
                SELECT tn.* FROM translation_notes tn
                LEFT JOIN translation_verses tv ON tn.translation_verse = tv.code
                LEFT JOIN translation_titles tt ON tn.translation_title = tt.code
                LEFT JOIN translation_verses tv2 ON tt.before_translation_verse = tv2.code
                WHERE tv.translation IN ({placeholders}) OR tv2.translation IN ({placeholders})
            """, translation_codes + translation_codes)
            result['translation_notes'] = cursor.fetchall()

            # voices — active
            cursor.execute(
                f"SELECT * FROM voices WHERE translation IN ({placeholders}) AND active = 1",
                translation_codes
            )
            result['voices'] = cursor.fetchall()

            # voice_alignments with COALESCE
            cursor.execute(f"""
                SELECT
                    va.code, va.voice, va.translation_verse, va.book_number,
                    va.chapter_number, va.verse_number,
                    COALESCE(vmf.begin, va.begin) AS `begin`,
                    COALESCE(vmf.end, va.end) AS `end`,
                    va.is_correct
                FROM voice_alignments va
                INNER JOIN voices v ON va.voice = v.code
                LEFT JOIN voice_manual_fixes vmf ON (
                    vmf.voice = va.voice AND
                    vmf.book_number = va.book_number AND
                    vmf.chapter_number = va.chapter_number AND
                    vmf.verse_number = va.verse_number
                )
                WHERE v.translation IN ({placeholders}) AND v.active = 1
            """, translation_codes)
            result['voice_alignments'] = decimal_to_float(cursor.fetchall())

        # Convert Decimal in all tables
        for key in result:
            if key != 'voice_alignments':  # already converted
                result[key] = decimal_to_float(result[key])

        return result

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Data export failed: {str(e)}")
    finally:
        cursor.close()
        connection.close()
