"""
Data export for Bible-API

GET /api/data[?translation=alias] — returns finalized data as JSON
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
