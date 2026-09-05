from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from typing import Optional
from database import create_connection
import re
import os
from pathlib import Path
from functools import lru_cache
from config import MP3_FILES_PATH, AUDIO_BASE_URL
from models import *
from auth import RequireAPIKey

router = APIRouter()

def get_translation_name(cursor, translation: int) -> str:
    query = '''
        SELECT name
        FROM translations
        WHERE code = %s
          AND active=1
    '''
    cursor.execute(query, (translation,))
    result = cursor.fetchone()
    if not result:
        raise HTTPException(
            status_code=422, 
            detail=f"Translation {translation} not found."
        )
    
    return result['name']
    
def get_voice_info(cursor, voice: int, translation: int) -> dict:
    query = '''
        SELECT v.name, v.link_template, v.alias as voice_alias, t.alias as translation_alias
        FROM voices v
        JOIN translations t ON v.translation = t.code
        WHERE v.code = %s
          AND v.translation = %s
          AND v.active=1
          AND t.active=1
    '''
    cursor.execute(query, (voice, translation,))
    result = cursor.fetchone()
    if not result:
        raise HTTPException(
            status_code=422, 
            detail=f"Voice {voice} not found for translation {translation}."
        )
    
    return result


@lru_cache(maxsize=20)  # Cache for translation+voice combinations
def get_all_existing_audio_chapters(translation_alias: str, voice_alias: str) -> dict:
    """
    Get all existing audio chapters for all books (cached)

    Args:
        translation_alias: Translation alias
        voice_alias: Voice alias

    Returns:
        Dict {book_number: set(chapter_numbers)}
    """
    base_path = Path(MP3_FILES_PATH) / translation_alias / voice_alias / "mp3"
    
    if not base_path.exists():
        return {}
    
    result = {}
    try:
        # Scan all book directories
        for book_entry in os.scandir(base_path):
            if book_entry.is_dir():
                try:
                    book_number = int(book_entry.name)
                except ValueError:
                    continue
                
                chapters = set()
                # Scan all chapter files in this book
                for chapter_entry in os.scandir(book_entry.path):
                    if chapter_entry.is_file() and chapter_entry.name.endswith('.mp3'):
                        chapter_str = chapter_entry.name[:-4]  # Remove .mp3
                        try:
                            chapters.add(int(chapter_str))
                        except ValueError:
                            pass
                
                if chapters:
                    result[book_number] = chapters
    except OSError:
        pass
    
    return result


@lru_cache(maxsize=100)  # Cache directory scans
def get_existing_audio_chapters(translation_alias: str, voice_alias: str, book_number: int) -> set:
    """
    Get all existing audio chapters for a book (cached)

    Args:
        translation_alias: Translation alias
        voice_alias: Voice alias
        book_number: Book number

    Returns:
        Set of chapter numbers that have audio files
    """
    # Use the batch function for better performance
    all_chapters = get_all_existing_audio_chapters(translation_alias, voice_alias)
    return all_chapters.get(book_number, set())


@lru_cache(maxsize=10000)  # Cache up to 10000 file checks
def check_audio_file_exists(translation_alias: str, voice_alias: str, book_number: int, chapter_number: int) -> bool:
    """
    Check if audio file exists in the audio directory (cached)

    Args:
        translation_alias: Translation alias (e.g.: syn, rst, bsb)
        voice_alias: Voice alias (e.g.: bondarenko, barry_hays)
        book_number: Book number
        chapter_number: Chapter number

    Returns:
        True if file exists, False otherwise
    """
    # Use batch check for better performance
    existing_chapters = get_existing_audio_chapters(translation_alias, voice_alias, book_number)
    return chapter_number in existing_chapters


def get_book_number(cursor: int, book_alias: str) -> str:
    query = '''
        SELECT number 
        FROM bible_books 
        WHERE code1 = %s 
    '''
    cursor.execute(query, (book_alias,))
    result = cursor.fetchone()
    
    if not result:
        raise HTTPException(
            status_code=422, 
            detail=f"Book '{book_alias}' not found."
        )
    
    return str(result['number'])
def get_book_alias(cursor: int, book_number: str) -> str:
    query = '''
        SELECT code1
        FROM bible_books 
        WHERE number = %s
    '''
    cursor.execute(query, (book_number,))
    result = cursor.fetchone()
    
    if not result:
        raise HTTPException(
            status_code=422, 
            detail=f"Book '{book_number}' not found."
        )
    
    return result['code1']


def get_chapter_data(cursor, translation: int, book_info: dict, chapter_number: int, voice: Optional[int] = None, voice_info: Optional[dict] = None, start_verse: Optional[int] = None, end_verse: Optional[int] = None) -> dict:
    """
    Get chapter data: verses, titles, notes, audio link

    Args:
        cursor: Database cursor
        translation: Translation code
        book_info: Book information (must contain 'number', 'name', 'alias', etc.)
        chapter_number: Chapter number
        voice: Voice code (optional)
        voice_info: Voice information (optional)
        start_verse: Start verse (optional, for ranges)
        end_verse: End verse (optional, for ranges)

    Returns:
        dict: Dictionary with chapter data
    """
    # Build SQL query for verses with manual fix overrides
    verses_query = '''
        SELECT 
            v.code, v.verse_number, v.verse_number_join, v.html, v.text, v.start_paragraph,
            COALESCE(vmf.begin, a.begin) as begin,
            COALESCE(vmf.end, a.end) as end
        FROM translation_verses AS v
            LEFT JOIN voice_alignments a ON (
                a.voice = %(voice)s AND 
                a.book_number = %(book_number)s AND 
                a.chapter_number = %(chapter_number)s AND 
                a.verse_number = v.verse_number
            )
            LEFT JOIN voice_manual_fixes vmf ON (
                vmf.voice = %(voice)s AND 
                vmf.book_number = %(book_number)s AND 
                vmf.chapter_number = %(chapter_number)s AND 
                vmf.verse_number = v.verse_number
            )
        WHERE v.translation = %(translation)s
            AND v.book_number = %(book_number)s
            AND v.chapter_number = %(chapter_number)s
    '''
    
    params = {
        'voice': voice,
        'translation': translation,
        'book_number': book_info['number'],
        'chapter_number': chapter_number,
    }
    
    # Add verse range filter if specified
    if start_verse is not None:
        params['start_verse'] = start_verse
        params['end_verse'] = end_verse if end_verse else start_verse
        if start_verse == end_verse:
            verses_query += '''
                AND v.verse_number = %(start_verse)s
            '''
        else:
            verses_query += '''
                AND v.verse_number BETWEEN %(start_verse)s AND %(end_verse)s
            '''
    
    verses_query += '''
        ORDER BY v.verse_number
    '''
    
    cursor.execute(verses_query, params)
    verses_results = cursor.fetchall()

    if not verses_results:
        raise HTTPException(
            status_code=422, 
            detail=f"No verses found for book {book_info['number']}, chapter {chapter_number}."
        )

    verses = []
    for verse in verses_results:
        if verse['begin'] is None or verse['end'] is None:
            verse['begin'] = 0
            verse['end'] = 0
        
        verse_model = VerseWithAlignmentModel(
            code=verse['code'],
            number=verse['verse_number'],
            join=verse['verse_number_join'],
            html=verse['html'],
            text=verse['text'],
            begin=verse['begin'],
            end=verse['end'] if verse['end'] is not None else 0.0,
            start_paragraph=verse['start_paragraph']
        )
        verses.append(verse_model)

    # Audio file link
    audio_link = ''
    if voice_info:
        # Check if audio file exists in the audio directory
        if check_audio_file_exists(
            voice_info['translation_alias'], 
            voice_info['voice_alias'], 
            book_info['number'], 
            chapter_number
        ):
            # If file exists, build link to internal endpoint
            book_str = str(book_info['number']).zfill(2)
            chapter_str = str(chapter_number).zfill(2)
            audio_link = f"{AUDIO_BASE_URL}/audio/{voice_info['translation_alias']}/{voice_info['voice_alias']}/{book_str}/{chapter_str}.mp3"

    codes = ", ".join(str(verse.code) for verse in verses)
    
    # Titles
    titles_query = '''
        SELECT code, text, before_translation_verse, metadata, reference, subtitle, position_text, position_html
        FROM translation_titles
        WHERE before_translation_verse IN (%s)
    ''' % codes
    cursor.execute(titles_query)
    titles_results = cursor.fetchall()
    titles = []
    title_codes = []
    for title in titles_results:
        title_model = TitleModel(
            code=title['code'],
            text=title['text'],
            before_verse_code=title['before_translation_verse'],
            metadata=title['metadata'],
            reference=title['reference'],
            subtitle=bool(title['subtitle']),
            position_text=title['position_text'],
            position_html=title['position_html']
        )
        titles.append(title_model)
        title_codes.append(str(title['code']))

    # Notes for verses and titles
    notes_query = '''
        SELECT code, note_number, text, translation_verse, translation_title, position_text, position_html
        FROM translation_notes
        WHERE translation_verse IN (%s)
    ''' % codes
    
    if title_codes:
        title_codes_str = ", ".join(title_codes)
        notes_query += ''' OR translation_title IN (%s)''' % title_codes_str
    
    cursor.execute(notes_query)
    notes_results = cursor.fetchall()
    notes = []
    for note in notes_results:
        note_model = NoteModel(
            code=note['code'],
            number=note['note_number'],
            text=note['text'],
            verse_code=note['translation_verse'],
            title_code=note['translation_title'],
            position_text=note['position_text'],
            position_html=note['position_html']
        )
        notes.append(note_model)

    return {
        'verses': verses,
        'titles': titles,
        'notes': notes,
        'audio_link': audio_link
    }

"""
def get_book_name(cursor: int, translation: int, book_number: str) -> str:
    query = '''
        SELECT name 
        FROM translation_books 
        WHERE translation = %s
            AND book_number = %s
    '''
    cursor.execute(query, (translation,book_number))
    result = cursor.fetchone()
    
    if not result:
        raise HTTPException(
            status_code=422, 
            detail=f"Book '{book_number}' not found in translation '{translation}'."
        )
    
    return result['name']
"""

# Simple error response model
class SimpleErrorResponse(BaseModel):
    detail: str

@router.get('/chapter_with_alignment', response_model=ExcerptWithAlignmentModel, operation_id="get_chapter_with_alignment", responses={422: {"model": SimpleErrorResponse}}, tags=["Excerpts"])
async def get_chapter_with_alignment(translation: int, book_number: int, chapter_number: int, voice: Optional[int] = None, api_key: bool = RequireAPIKey):
    """
    Get chapter with alignment by book and chapter number

    Args:
        translation: Translation code
        book_number: Book number (1-66)
        chapter_number: Chapter number
        voice: Voice code (optional)

    Returns:
        ExcerptWithAlignmentModel: Chapter data with alignment
    """
    connection = create_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        # Validate input parameters
        if book_number < 1 or book_number > 66:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid book number ({book_number}). Must be between 1 and 66."
            )
        
        if chapter_number < 1:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid chapter number ({chapter_number}). Must be greater than 0."
            )
        
        translation_name = get_translation_name(cursor, translation)
        voice_info = get_voice_info(cursor, voice, translation) if voice else None

        # Get book info by number
        cursor.execute('''
            SELECT tb.code, tb.book_number AS number, tb.name, bb.code1 AS alias, bb.code2, bb.code3, bb.code4, bb.code5, bb.code6, bb.code7, bb.code8, bb.code9,
                   (SELECT max(chapter_number) FROM translation_verses WHERE book_number = tb.book_number) AS chapters_count
            FROM translation_books AS tb
            LEFT JOIN bible_books AS bb ON bb.number = tb.book_number
            WHERE tb.translation = %s AND tb.book_number = %s
        ''', (translation, book_number))
        
        book_info = cursor.fetchone()
        if not book_info:
            raise HTTPException(
                status_code=422, 
                detail=f"Book {book_number} not found for translation {translation}."
            )
        
        # Check that the chapter exists
        if chapter_number > book_info['chapters_count']:
            raise HTTPException(
                status_code=422,
                detail=f"Chapter {chapter_number} not found. Book {book_number} has only {book_info['chapters_count']} chapters."
            )

        # Get chapter data via shared function
        chapter_data = get_chapter_data(cursor, translation, book_info, chapter_number, voice, voice_info)

        part = PartsWithAlignmentModel(
            book=book_info,
            prev_excerpt=get_prev_excerpt(cursor, translation, book_info, chapter_number),
            next_excerpt=get_next_excerpt(cursor, translation, book_info, chapter_number),
            chapter_number=chapter_number,
            audio_link=chapter_data['audio_link'],
            verses=chapter_data['verses'],
            notes=chapter_data['notes'],
            titles=chapter_data['titles']
        )

        title = f"{book_info['name']} {chapter_number}"

        return ExcerptWithAlignmentModel(
            title=title, 
            is_single_chapter=True,
            parts=[part]
        )
    
    except HTTPException as e:
        raise e
    finally:
        cursor.close()
        connection.close()


# Grammar of the `excerpt` parameter: a book alias, a chapter and an optional
# verse or verse range — `gen 1`, `gen 1:1`, `gen 1:1-3`. Several references may
# be listed in one string, so the pattern is *scanned* for rather than matched
# against the whole value.
#
# The book token is Latin letters and digits taken as a WHOLE token: the
# look-behind stops a match from starting in the middle of a word. Until
# 2026-09-05 the token was `[0-9a-z]+` with no boundary, so `Gen 1:1` matched
# the substring `en` and answered "Book with alias 'en' not found"
# (ClickUp 86cbehfqx). Case is folded in Python before the lookup rather than
# left to the database collation, so `Gen`, `GEN` and `gen` are one alias
# whatever `bible_books` happens to be collated as.
#
# A book name in any other script (`Бут 1:1`) matches nothing and is a 422 that
# names the expected format: by Maria's decision of 2026-09-05 the contract is
# the Latin alias from the books catalogue only — the client never lets a human
# type this value, it copies it from `GET /api/translations/{code}/books`.
# Mirrors Bible-API `app/excerpt.py`.
EXCERPT_PATTERN = re.compile(
    r'(?<![A-Za-z0-9])(?P<book>[A-Za-z0-9]+) (?P<chapter>\d+)'
    r'(:(?P<start_verse>\d+)(?:-(?P<end_verse>\d+))?)?'
)

EXCERPT_FORMAT_HINT = (
    "Expected '<book alias> <chapter>[:<verse>[-<verse>]]', e.g. 'gen 1:1-3'. "
    "The book alias is the Latin `alias` of "
    "GET /api/translations/{code}/books; letter case does not matter."
)


@router.get('/excerpt_with_alignment', response_model=ExcerptWithAlignmentModel, operation_id="get_excerpt_with_alignment", responses={404: {"model": SimpleErrorResponse}, 422: {"model": SimpleErrorResponse}}, tags=["Excerpts"])
async def get_excerpt_with_alignment(translation: int, excerpt: str, voice: Optional[int] = None, api_key: bool = RequireAPIKey):
    """
    Text of one or more references with word-level audio timing.

    `excerpt` is `<book alias> <chapter>[:<verse>[-<verse>]]`, e.g. `gen 1`,
    `gen 1:1` or `gen 1:1-3`; several references may be listed in one value.

    The book is addressed by its **alias as returned by
    `GET /api/translations/{code}/books`** — Latin letters and digits,
    **case-insensitive** (`gen`, `Gen` and `GEN` are the same book). A book
    name written in any other script, or in any other spelling than the
    catalogue's aliases, is not part of the contract: a value that does not
    parse is a `422` naming the expected format, and an alias that parses but
    belongs to no book of this translation is a `404`.
    """
    connection = create_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        translation_name = get_translation_name(cursor, translation)
        voice_info = get_voice_info(cursor, voice, translation) if voice else None

        is_single_chapter = True
        book_name = ''

        matches = list(EXCERPT_PATTERN.finditer(excerpt))

        if not matches:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid excerpt format ({excerpt}). {EXCERPT_FORMAT_HINT}"
            )

        parts = []

        for match in matches:
            # One alias, whatever case it was written in.
            book_alias = match.group('book').casefold()
            chapter_number = int(match.group('chapter'))
            start_verse = match.group('start_verse')
            end_verse = match.group('end_verse')

            # Get book code by alias
            books_info_list = get_books_info(cursor, translation, book_alias)
            if not books_info_list:
                raise HTTPException(
                    status_code=404,
                    detail=f"Book with alias '{book_alias}' not found for translation {translation}."
                )
            book_info = books_info_list[0]
            #book_number = book_info['number'] # get_book_number(cursor, book_alias)
            #book_name = book_info['name'] # get_book_name(cursor, translation, book_number)

            # Handle verse ranges
            if start_verse is not None:
                is_single_chapter = False
                start_verse_int = int(start_verse)
                end_verse_int = int(end_verse) if end_verse else start_verse_int
            else:
                start_verse_int = None
                end_verse_int = None

            # Get chapter data via shared function
            try:
                chapter_data = get_chapter_data(cursor, translation, book_info, chapter_number, voice, voice_info, start_verse_int, end_verse_int)
                verses = chapter_data['verses']
                titles = chapter_data['titles']
                notes = chapter_data['notes']
                audio_link = chapter_data['audio_link']
            except HTTPException as e:
                # Catch and adapt error messages for excerpt format
                if start_verse is None:
                    raise HTTPException(
                        status_code=422, 
                        detail=f"No verses found for {book_alias} {chapter_number}."
                    )
                else:
                    verse_range = f"{start_verse}" if start_verse == end_verse or end_verse is None else f"{start_verse}-{end_verse}"
                    raise HTTPException(
                        status_code=422, 
                        detail=f"No verses found for {book_alias} {chapter_number}:{verse_range}."
                    )

            part = PartsWithAlignmentModel(
                book=book_info,
                prev_excerpt=get_prev_excerpt(cursor, translation, book_info, chapter_number),
                next_excerpt=get_next_excerpt(cursor, translation, book_info, chapter_number),
                chapter_number=chapter_number,
                audio_link=audio_link,
                verses=verses,
                notes=notes,
                titles=titles
            )

            parts.append(part)
        
        if len(parts) == 1:
            title = f"{book_info['name']} {chapter_number}"
        elif len(parts) > 1:
            is_single_chapter = False
            title = f"Excerpt {excerpt}"
        else:
            title = ''

        return ExcerptWithAlignmentModel(
            title=title, 
            is_single_chapter=is_single_chapter,
            parts=parts
        )
    
    except HTTPException as e:
        raise e  # Let HTTPException propagate with the correct status code
    #except Exception as e:
    #    raise HTTPException(status_code=500, detail="Internal Server Error")  # For any unexpected errors
    finally:
        cursor.close()
        connection.close()


def get_books_info(cursor: any, translation: int, alias: str=None):
    params = { 'translation': translation }
    sql = '''
        SELECT 
            tb.code, tb.book_number AS number, tb.name, bb.code1 AS alias, bb.code2, bb.code3, bb.code4, bb.code5, bb.code6, bb.code7, bb.code8, bb.code9,
            (SELECT max(chapter_number) FROM translation_verses WHERE book_number = tb.book_number) AS chapters_count
        FROM translation_books AS tb
        LEFT JOIN bible_books AS bb ON bb.number = tb.book_number
        WHERE tb.translation = %(translation)s
    '''
    if alias:
        # The catalogue aliases only. `bb.short_name_en` / `bb.short_name_ru`
        # were matched here as well until 2026-09-05 (ClickUp 86cbehfqx): the
        # Russian one is unreachable — the grammar never lets a Cyrillic token
        # through — and the English one made 14 books answer to a second,
        # undocumented name (`mt`, `jn`, `ex`, `1kings`, …) that
        # `GET /api/translations/{code}/books` does not publish. Verified on the
        # live `cep_admin` and `cep_public` before removing them: no value of
        # `code1..code5` belongs to two different books, case-insensitively.
        sql += ''' AND (bb.code1 = %(alias)s OR bb.code2 = %(alias)s OR bb.code3 = %(alias)s OR bb.code4 = %(alias)s OR bb.code5 = %(alias)s)
        '''
        params['alias'] = alias
    cursor.execute(sql, params)
    return cursor.fetchall()

def get_prev_excerpt(cursor: any, translation: int, book: BookInfoModel, chapter_number: int):
    if chapter_number > 1:
        return "%s %s" % (book['alias'], chapter_number-1)
    else:
        current_book_number = int(get_book_number(cursor, book['alias']))
        if current_book_number == 1:
            return '' # first book, first chapter
        else:
            prev_book_alias = get_book_alias(cursor, current_book_number-1)
            prev_book_info_list = get_books_info(cursor, translation, prev_book_alias)
            if prev_book_info_list:
                prev_book_info = prev_book_info_list[0]
                return "%s %s" % (prev_book_alias, prev_book_info['chapters_count'])
            else:
                return '' # previous book not found

    return ''

def get_next_excerpt(cursor: any, translation: int, book: BookInfoModel, chapter_number: int): 
    if chapter_number < book['chapters_count']:
        return "%s %s" % (book['alias'], chapter_number+1)
    else:
        current_book_number = int(get_book_number(cursor, book['alias']))
        if current_book_number == 66:
            return '' # last book, last chapter
        else:
            next_book_alias = get_book_alias(cursor, current_book_number+1)
            # Check that the next book exists
            if next_book_alias:
                return "%s 1" % next_book_alias
            else:
                return '' # next book not found

    return ''