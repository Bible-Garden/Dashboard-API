"""
Data export for Bible-API

GET /api/data[?translation=alias] — returns finalized data as JSON
GET /api/data/manifest — the small plan of a full resync (ClickUp 86cbbq5zp)
"""

from typing import Optional
from decimal import Decimal
from fastapi import APIRouter, HTTPException, Query
from database import create_connection
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
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Data manifest failed: {str(e)}")
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
