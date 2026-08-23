from typing import Union, Optional
from datetime import timedelta, datetime
from functools import wraps
import hashlib
import json

from fastapi import FastAPI, HTTPException, status, APIRouter
from database import create_connection
from models import *

from fastapi.routing import APIRoute

from excerpt import router as excerpt_router
from excerpt import get_books_info, check_audio_file_exists
from checks import router as checks_router
from audio import router as audio_router
from data import router as data_router
from stats import router as stats_router
from auth import (
    Token, LoginRequest, authenticate_user, create_access_token,
    RequireAPIKey, RequireJWT
)
from config import JWT_EXPIRE_HOURS

# Simple in-memory cache with TTL
_cache = {}
_cache_timestamps = {}

def timed_cache(seconds: int = 3600):
    """Decorator for caching function results with TTL"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Create cache key from function name and arguments
            cache_key = f"{func.__name__}:{hashlib.md5(json.dumps([args, kwargs], sort_keys=True, default=str).encode()).hexdigest()}"
            
            # Check if cached value exists and is not expired
            if cache_key in _cache:
                timestamp = _cache_timestamps.get(cache_key)
                if timestamp and (datetime.now() - timestamp).total_seconds() < seconds:
                    return _cache[cache_key]
            
            # Call function and cache result
            result = func(*args, **kwargs)
            _cache[cache_key] = result
            _cache_timestamps[cache_key] = datetime.now()
            
            return result
        return wrapper
    return decorator

# Tags metadata for controlling order in Swagger UI
tags_metadata = [
    {
        "name": "Auth",
        "description": "Authorization and authentication",
    },
    {
        "name": "Languages",
        "description": "",
    },
    {
        "name": "Translations",
        "description": "",
    },
    {
        "name": "Voices",
        "description": "",
    },
    {
        "name": "Excerpts",
        "description": "",
    },
    {
        "name": "Audio",
        "description": "Streaming & Download mp3",
    },
    {
        "name": "Data",
        "description": "Data export for Bible-API",
    },
    {
        "name": "Statistics",
        "description": "API usage statistics from Bible-API",
    },
    {
        "name": "Admin",
        "description": "Administrative operations (JWT token required)",
    },
]

app = FastAPI(
    openapi_tags=tags_metadata,
    title="Admin API",
    description="Admin API for Bible Garden data management",
    version="0.1.0",
    swagger_ui_parameters={
        "deepLinking": True,
        "displayRequestDuration": True,
        "defaultModelsExpandDepth": 0,
        "tryItOutEnabled": True,
    }
)

# Create main router with /api prefix
api_router = APIRouter(prefix="/api")

# Include routers to main router
api_router.include_router(excerpt_router)
api_router.include_router(checks_router)
api_router.include_router(audio_router)
api_router.include_router(data_router)
api_router.include_router(stats_router)


@api_router.post('/auth/login', response_model=Token, operation_id="login", tags=["Auth"])
def login(credentials: LoginRequest):
    """
    Get JWT token for access to administrative endpoints
    
    Token is valid for 24 hours and must be passed in the header:
    Authorization: Bearer <token>
    """
    if not authenticate_user(credentials.username, credentials.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    access_token_expires = timedelta(hours=JWT_EXPIRE_HOURS)
    access_token = create_access_token(
        data={"sub": credentials.username}, 
        expires_delta=access_token_expires
    )
    
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": JWT_EXPIRE_HOURS * 3600  # in seconds
    }


@api_router.get('/languages', response_model=list[LanguageModel], operation_id="get_languages", tags=["Languages"])
def get_languages(api_key: bool = RequireAPIKey):
    connection = create_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute('''
			SELECT alias, name_en, name_national 
			FROM languages
		''')
        result = cursor.fetchall()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        connection.close()
    return result


@api_router.get('/translations', response_model=list[TranslationModel], operation_id="get_translations", tags=["Translations"])
def get_translations(language: Optional[str] = None, only_active: int = 1, api_key: bool = RequireAPIKey):
    connection = create_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        params = []
        sql = '''
            SELECT 
				t.code        AS translation_code,
				t.alias       AS translation_alias,
				t.name        AS translation_name,
				t.description AS translation_description,
				t.language    AS translation_language,
				t.active      AS translation_active,
				
				a.code        AS audio_code,
				a.alias       AS audio_alias,
				a.name        AS audio_name,
				a.description AS audio_description,
				a.is_music    AS audio_is_music,
				a.active      AS audio_active,
				COALESCE(va.anomalies_count, 0) AS audio_anomalies_count,
				COALESCE(va_open.anomalies_open_count, 0) AS audio_anomalies_open_count
            FROM translations AS t
              LEFT JOIN voices AS a ON a.translation = t.code
              LEFT JOIN (
                  SELECT voice, COUNT(*) as anomalies_count
                  FROM voice_anomalies
                  GROUP BY voice
              ) AS va ON va.voice = a.code
              LEFT JOIN (
                  SELECT voice, COUNT(*) as anomalies_open_count
                  FROM voice_anomalies
                  WHERE status IN ('detected','confirmed')
                  GROUP BY voice
              ) AS va_open ON va_open.voice = a.code'''
        
        # Add active filter conditions based on only_active parameter
        if only_active == 1:
            sql += " WHERE t.active=1 AND (a.active=1 OR a.active IS NULL)"
        else:
            sql += " WHERE 1=1"
        if language:
            sql += " AND t.language = %s "
            params.append(language)
        cursor.execute(sql, params)
        rows = cursor.fetchall()
		
        translations = {}
        for row in rows:
            translation_code = row['translation_code']
            if translation_code not in translations:
                translations[translation_code] = {
                    'code'        : translation_code,
                    'alias'       : row['translation_alias'],
                    'name'        : row['translation_name'],
                    'description' : row['translation_description'],
                    'language'    : row['translation_language'],
                    'active'      : row['translation_active'],
                    'voices'      : [],
                }
            if row['audio_code']:
                translations[translation_code]['voices'].append({
                    'code'        : row['audio_code'],
                    'alias'       : row['audio_alias'],
                    'name'        : row['audio_name'],
                    'description' : row['audio_description'],
                    'is_music'    : row['audio_is_music'],
                    'active'      : row['audio_active'],
                    'anomalies_count': row['audio_anomalies_count'],
                    'anomalies_open_count': row['audio_anomalies_open_count'],
                })
        
        result = list(translations.values())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        connection.close()
    return result

@api_router.get('/translation_info', response_model=TranslationInfoModel, operation_id="get_translation_info", tags=["Translations"])
def get_translation_info(translation: int, api_key: bool = RequireAPIKey):
    connection = create_connection()
    cursor = connection.cursor(dictionary=True)
    result = []
    try:
        sql = '''
            SELECT code, alias, name, description, language
            FROM translations
            WHERE code = %(translation)s
              AND active=1
        '''
        cursor.execute(sql, { 'translation': translation })

        result = cursor.fetchone()
        if not result:
            raise HTTPException(
                status_code=422, 
                detail=f"Translation {translation} not found."
            )
		
        result['books_info'] = get_books_info(cursor, translation)
        
    except Exception as e:
        #raise HTTPException(status_code=500, detail=str(e))
        raise e
    finally:
        cursor.close()
        connection.close()
    return result


@timed_cache(seconds=3600)  # Cache for 1 hour
def get_chapters_by_book(translation_code: int) -> dict:
    """Get all chapters for all books in a translation (cached)"""
    connection = create_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        # Get all book numbers for this translation
        cursor.execute('''
            SELECT book_number FROM translation_books WHERE translation = %s
        ''', (translation_code,))
        book_numbers = [row['book_number'] for row in cursor.fetchall()]
        
        if not book_numbers:
            return {}
        
        # Get all chapters in one query
        placeholders = ','.join(['%s'] * len(book_numbers))
        cursor.execute(f'''
            SELECT book_number, chapter_number
            FROM translation_verses
            WHERE book_number IN ({placeholders})
            GROUP BY book_number, chapter_number
        ''', book_numbers)
        
        # Build map
        chapters_by_book = {}
        for row in cursor.fetchall():
            book_number = row['book_number']
            if book_number not in chapters_by_book:
                chapters_by_book[book_number] = set()
            chapters_by_book[book_number].add(row['chapter_number'])
        
        return chapters_by_book
    finally:
        cursor.close()
        connection.close()


@api_router.get('/translations/{translation_code}/books', response_model=list[TranslationBookModel], operation_id="get_translation_books", tags=["Translations"])
def get_translation_books(translation_code: int, voice_code: Optional[int] = None, api_key: bool = RequireAPIKey):
    connection = create_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        # Check if translation exists and get alias
        cursor.execute("SELECT code, alias FROM translations WHERE code = %s AND active = 1", (translation_code,))
        translation = cursor.fetchone()
        if not translation:
            raise HTTPException(status_code=404, detail=f"Translation {translation_code} not found")
        
        translation_alias = translation['alias']
        voice_alias = None
        
        # If voice_code is provided, check if voice exists and get alias
        if voice_code:
            cursor.execute("SELECT code, alias FROM voices WHERE code = %s", (voice_code,))
            voice = cursor.fetchone()
            if not voice:
                raise HTTPException(status_code=404, detail=f"Voice {voice_code} not found")
            voice_alias = voice['alias']
        
        # Get books for this translation with alias from bible_books
        if voice_code:
            # Include anomalies count when voice_code is provided
            cursor.execute('''
                SELECT 
                    tb.code, tb.book_number, tb.name, bb.code1 AS alias,
                    (SELECT max(chapter_number) FROM translation_verses WHERE book_number = tb.book_number) AS chapters_count,
                    COALESCE(
                        (SELECT COUNT(*) FROM voice_anomalies va WHERE va.book_number = tb.book_number AND va.voice = %s), 
                        0
                    ) AS anomalies_count,
                    COALESCE(
                        (SELECT COUNT(*) FROM voice_anomalies va2 
                         WHERE va2.book_number = tb.book_number 
                           AND va2.voice = %s 
                           AND va2.status IN ('detected','confirmed')
                        ), 0
                    ) AS anomalies_open_count
                FROM translation_books AS tb
                LEFT JOIN bible_books AS bb ON bb.number = tb.book_number
                WHERE tb.translation = %s
                ORDER BY tb.book_number
            ''', (voice_code, voice_code, translation_code))
        else:
            # Original query without anomalies count
            cursor.execute('''
                SELECT 
                    tb.code, tb.book_number, tb.name, bb.code1 AS alias,
                    (SELECT max(chapter_number) FROM translation_verses WHERE book_number = tb.book_number) AS chapters_count
                FROM translation_books AS tb
                LEFT JOIN bible_books AS bb ON bb.number = tb.book_number
                WHERE tb.translation = %s
                ORDER BY tb.book_number
            ''', (translation_code,))
        
        books = cursor.fetchall()
        
        # Get all existing chapters from cache
        chapters_by_book = get_chapters_by_book(translation_code)
        
        # Check for chapters without text and audio
        for book in books:
            book_number = book['book_number']
            book_code = book['code']
            chapters_count = book['chapters_count'] or 0
            
            # Get existing chapters from pre-loaded data
            existing_chapters = chapters_by_book.get(book_number, set())
            
            # Find chapters without text (missing in translation_verses)
            if chapters_count > 0:
                expected_chapters = set(range(1, chapters_count + 1))
                chapters_without_text = sorted(expected_chapters - existing_chapters)
            else:
                chapters_without_text = []
            
            book['chapters_without_text'] = chapters_without_text
            
            # If voice_code is provided, check for chapters without audio
            if voice_code and voice_alias and translation_alias:
                chapters_without_audio = []
                
                # Check each existing chapter for audio file existence
                for chapter_number in existing_chapters:
                    if not check_audio_file_exists(translation_alias, voice_alias, book_number, chapter_number):
                        chapters_without_audio.append(chapter_number)
                
                book['chapters_without_audio'] = sorted(chapters_without_audio)
        
        return books
        
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        connection.close()


@api_router.post('/cache/clear', operation_id="clear_cache", tags=["Admin"])
def clear_cache(username: str = RequireJWT):
    """Clear all cached data (requires JWT authentication)"""
    from excerpt import get_all_existing_audio_chapters, get_existing_audio_chapters, check_audio_file_exists
    
    global _cache, _cache_timestamps
    cache_size = len(_cache)
    _cache.clear()
    _cache_timestamps.clear()
    
    # Clear LRU caches from excerpt.py
    get_all_existing_audio_chapters.cache_clear()
    get_existing_audio_chapters.cache_clear()
    check_audio_file_exists.cache_clear()
    
    return {
        "message": f"All caches cleared successfully", 
        "items_cleared": cache_size,
        "lru_caches_cleared": ["get_all_existing_audio_chapters", "get_existing_audio_chapters", "check_audio_file_exists"]
    }


@api_router.put('/translations/{translation_code}', response_model=TranslationModel, operation_id="update_translation", tags=["Translations"])
def update_translation(translation_code: int, update_data: TranslationUpdateModel, username: str = RequireJWT):
    connection = create_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        # Check if translation exists
        cursor.execute("SELECT * FROM translations WHERE code = %s", (translation_code,))
        existing = cursor.fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail=f"Translation {translation_code} not found")
        
        # Build update query dynamically
        update_fields = []
        params = []
        
        if update_data.alias is not None:
            update_fields.append("alias = %s")
            params.append(update_data.alias)
        if update_data.name is not None:
            update_fields.append("name = %s")
            params.append(update_data.name)
        if update_data.description is not None:
            update_fields.append("description = %s")
            params.append(update_data.description)
        if update_data.language is not None:
            update_fields.append("language = %s")
            params.append(update_data.language)
        if update_data.active is not None:
            update_fields.append("active = %s")
            params.append(update_data.active)
        
        if not update_fields:
            raise HTTPException(status_code=400, detail="No fields to update")
        
        # Execute update
        params.append(translation_code)
        update_sql = f"UPDATE translations SET {', '.join(update_fields)} WHERE code = %s"
        cursor.execute(update_sql, params)
        connection.commit()
        
        # Return updated translation
        cursor.execute('''
            SELECT 
                t.code, t.alias, t.name, t.description, t.language, t.active,
                v.code as voice_code, v.alias as voice_alias, v.name as voice_name, 
                v.description as voice_description, v.is_music, v.active as voice_active
            FROM translations t
            LEFT JOIN voices v ON v.translation = t.code
            WHERE t.code = %s
        ''', (translation_code,))
        rows = cursor.fetchall()
        
        if not rows:
            raise HTTPException(status_code=404, detail=f"Translation {translation_code} not found")
        
        # Build response
        translation_data = {
            'code': rows[0]['code'],
            'alias': rows[0]['alias'],
            'name': rows[0]['name'],
            'description': rows[0]['description'],
            'language': rows[0]['language'],
            'active': rows[0]['active'],
            'voices': []
        }
        
        for row in rows:
            if row['voice_code']:
                translation_data['voices'].append({
                    'code': row['voice_code'],
                    'alias': row['voice_alias'],
                    'name': row['voice_name'],
                    'description': row['voice_description'],
                    'is_music': row['is_music'],
                    'active': row['voice_active']
                })
        
        return translation_data
        
    except Exception as e:
        connection.rollback()
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        connection.close()


@api_router.put('/voices/{voice_code}', response_model=VoiceModel, operation_id="update_voice", tags=["Voices"])
def update_voice(voice_code: int, update_data: VoiceUpdateModel, username: str = RequireJWT):
    connection = create_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        # Check if voice exists
        cursor.execute("SELECT * FROM voices WHERE code = %s", (voice_code,))
        existing = cursor.fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail=f"Voice {voice_code} not found")
        
        # Build update query dynamically
        update_fields = []
        params = []
        
        if update_data.alias is not None:
            update_fields.append("alias = %s")
            params.append(update_data.alias)
        if update_data.name is not None:
            update_fields.append("name = %s")
            params.append(update_data.name)
        if update_data.description is not None:
            update_fields.append("description = %s")
            params.append(update_data.description)
        if update_data.is_music is not None:
            update_fields.append("is_music = %s")
            params.append(update_data.is_music)
        if update_data.active is not None:
            update_fields.append("active = %s")
            params.append(update_data.active)
        
        if not update_fields:
            raise HTTPException(status_code=400, detail="No fields to update")
        
        # Execute update
        params.append(voice_code)
        update_sql = f"UPDATE voices SET {', '.join(update_fields)} WHERE code = %s"
        cursor.execute(update_sql, params)
        connection.commit()
        
        # Return updated voice
        cursor.execute('''
            SELECT code, alias, name, description, is_music, active
            FROM voices
            WHERE code = %s
        ''', (voice_code,))
        result = cursor.fetchone()
        
        if not result:
            raise HTTPException(status_code=404, detail=f"Voice {voice_code} not found")
        
        return result
        
    except Exception as e:
        connection.rollback()
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        connection.close()


@api_router.get('/voices/{voice_code}/anomalies', response_model=VoiceAnomaliesResponseModel, operation_id="get_voice_anomalies", tags=["Voices"])
def get_voice_anomalies(voice_code: int, page: int = 1, limit: int = 50, anomaly_type: Optional[str] = None, book_number: Optional[int] = None, status: Optional[str] = None, sort_by: Optional[str] = None, sort_order: Optional[str] = None, username: str = RequireJWT):
    connection = create_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        # Validate pagination parameters
        if page < 1:
            raise HTTPException(status_code=400, detail="Page must be >= 1")
        if limit < 1 or limit > 1000:
            raise HTTPException(status_code=400, detail="Limit must be between 1 and 1000")
        
        # Validate sort_by parameter
        valid_sort_options = ["address", "type", "ratio"]
        if sort_by and sort_by not in valid_sort_options:
            raise HTTPException(status_code=400, detail=f"Invalid sort_by value. Must be one of: {', '.join(valid_sort_options)}")
        
        # Validate sort_order parameter
        valid_sort_orders = ["asc", "desc"]
        if sort_order and sort_order.lower() not in valid_sort_orders:
            raise HTTPException(status_code=400, detail=f"Invalid sort_order value. Must be one of: {', '.join(valid_sort_orders)}")
        
        # Check if voice exists
        cursor.execute("SELECT code FROM voices WHERE code = %s", (voice_code,))
        voice = cursor.fetchone()
        if not voice:
            raise HTTPException(status_code=404, detail=f"Voice {voice_code} not found")
        
        # Calculate offset
        offset = (page - 1) * limit
        
        # Build query parameters
        query_params = [voice_code]
        where_clause = "WHERE va.voice = %s"
        
        # Add anomaly_type filter if provided
        if anomaly_type:
            where_clause += " AND va.anomaly_type = %s"
            query_params.append(anomaly_type)
        
        # Add book_number filter if provided
        if book_number:
            where_clause += " AND va.book_number = %s"
            query_params.append(book_number)
        
        # Add status filter if provided
        if status:
            # Validate status value
            valid_statuses = ["detected", "confirmed", "disproved", "corrected", "already_resolved", "disproved_whisper"]
            if status not in valid_statuses:
                raise HTTPException(status_code=400, detail=f"Invalid status value. Must be one of: {', '.join(valid_statuses)}")
            where_clause += " AND va.status = %s"
            query_params.append(status)
        
        # Build ORDER BY clause based on sort_by and sort_order parameters
        sort_direction = "DESC" if sort_order and sort_order.lower() == "desc" else "ASC"
        
        if sort_by == "address":
            order_by = f"ORDER BY va.book_number {sort_direction}, chapter_number {sort_direction}, verse_number {sort_direction}, position_in_verse {sort_direction}"
        elif sort_by == "type":
            order_by = f"ORDER BY va.anomaly_type {sort_direction}, book_number ASC, chapter_number ASC, verse_number ASC, position_in_verse ASC"
        elif sort_by == "ratio":
            order_by = f"ORDER BY va.ratio {sort_direction}, book_number ASC, chapter_number ASC, verse_number ASC, position_in_verse ASC"
        else:
            # Default sorting by ratio DESC
            order_by = "ORDER BY ratio DESC, book_number, chapter_number, verse_number, position_in_verse"
        
        # Get total count first
        count_sql = f"SELECT COUNT(*) as total FROM voice_anomalies AS va {where_clause}"
        cursor.execute(count_sql, query_params)
        total_count = cursor.fetchone()['total']
        
        # Get anomalies for this voice with pagination and filtering
        query_params.extend([limit, offset])
        sql = f'''
            SELECT va.code, va.voice, va.translation, va.book_number, va.chapter_number, 
                   va.verse_number, va.word, va.position_in_verse, va.position_from_end,
                   va.duration, va.speed, va.ratio, va.anomaly_type, va.status,
                   tv.text AS verse_text, va.updated_at
            FROM voice_anomalies AS va
            LEFT JOIN translation_verses tv ON (
                tv.code = va.translation_verse_id
            )
            {where_clause}
            {order_by}
            LIMIT %s OFFSET %s
        '''
        cursor.execute(sql, query_params)
        
        anomalies = cursor.fetchall()
        return {
            "items": anomalies,
            "total_count": total_count
        }
        
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        connection.close()


@api_router.post("/voices/anomalies", response_model=VoiceAnomalyModel, operation_id="create_voice_anomaly", tags=["Voices"])
def create_voice_anomaly(anomaly_data: VoiceAnomalyCreateModel, username: str = RequireJWT):
    """Create a new voice anomaly"""
    connection = create_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        # Validate that voice exists
        cursor.execute("SELECT code FROM voices WHERE code = %s", (anomaly_data.voice,))
        voice = cursor.fetchone()
        if not voice:
            raise HTTPException(status_code=404, detail=f"Voice {anomaly_data.voice} not found")
        
        # Validate that translation exists
        cursor.execute("SELECT code FROM translations WHERE code = %s", (anomaly_data.translation,))
        translation = cursor.fetchone()
        if not translation:
            raise HTTPException(status_code=404, detail=f"Translation {anomaly_data.translation} not found")
        
        # Get translation_verse_id
        cursor.execute(
            """
            SELECT tv.code FROM translation_verses tv
            WHERE tv.translation = %s AND tv.book_number = %s AND tv.chapter_number = %s AND tv.verse_number = %s
            """,
            (anomaly_data.translation, anomaly_data.book_number, anomaly_data.chapter_number, anomaly_data.verse_number)
        )
        verse = cursor.fetchone()
        if not verse:
            raise HTTPException(
                status_code=404, 
                detail=f"Verse not found: translation {anomaly_data.translation}, book {anomaly_data.book_number}, chapter {anomaly_data.chapter_number}, verse {anomaly_data.verse_number}"
            )
        
        translation_verse_id = verse['code']
        
        # Insert new anomaly
        cursor.execute(
            """
            INSERT INTO voice_anomalies 
            (voice, translation, book_number, chapter_number, verse_number, translation_verse_id,
             word, position_in_verse, position_from_end, duration, speed, ratio, anomaly_type, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                anomaly_data.voice, anomaly_data.translation, anomaly_data.book_number,
                anomaly_data.chapter_number, anomaly_data.verse_number, translation_verse_id,
                anomaly_data.word, anomaly_data.position_in_verse, anomaly_data.position_from_end,
                anomaly_data.duration, anomaly_data.speed, anomaly_data.ratio,
                anomaly_data.anomaly_type, anomaly_data.status.value
            )
        )
        
        # Get the created anomaly ID
        anomaly_id = cursor.lastrowid
        
        connection.commit()
        
        # Fetch and return the created anomaly with all fields
        cursor.execute(
            """
            SELECT va.code, va.voice, va.translation, va.book_number, va.chapter_number, 
                   va.verse_number, va.word, va.position_in_verse, va.position_from_end,
                   va.duration, va.speed, va.ratio, va.anomaly_type, va.status,
                   al.begin AS verse_start_time, al.end AS verse_end_time,
                   tv.text AS verse_text
            FROM voice_anomalies AS va
            LEFT JOIN voice_alignments al ON (
                al.voice = va.voice AND 
                al.book_number = va.book_number AND 
                al.chapter_number = va.chapter_number AND 
                al.verse_number = va.verse_number
            )
            LEFT JOIN translation_verses tv ON (
                tv.code = va.translation_verse_id
            )
            WHERE va.code = %s
            """,
            (anomaly_id,)
        )
        
        created_anomaly = cursor.fetchone()
        if not created_anomaly:
            raise HTTPException(status_code=500, detail="Failed to retrieve created anomaly")
        
        return created_anomaly
        
    except Exception as e:
        connection.rollback()
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        connection.close()


@api_router.patch("/voices/anomalies/{anomaly_code}/status", response_model=VoiceAnomalyModel, operation_id="update_anomaly_status", tags=["Voices"])
def update_anomaly_status(anomaly_code: int, update_data: AnomalyStatusUpdateModel, username: str = RequireJWT):
    """Update the status of a voice anomaly"""
    connection = create_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        # Get anomaly details with alignment data
        cursor.execute(
            """
            SELECT va.code, va.voice, va.translation, va.book_number, va.chapter_number, 
                   va.verse_number, va.word, va.position_in_verse, va.position_from_end,
                   va.duration, va.speed, va.ratio, va.anomaly_type, va.status,
                   va.translation_verse_id,
                   al.begin AS verse_start_time, al.end AS verse_end_time,
                   tv.text AS verse_text
            FROM voice_anomalies AS va
            LEFT JOIN voice_alignments al ON (
                al.voice = va.voice AND 
                al.book_number = va.book_number AND 
                al.chapter_number = va.chapter_number AND 
                al.verse_number = va.verse_number
            )
            LEFT JOIN translation_verses tv ON (
                tv.code = va.translation_verse_id
            )
            WHERE va.code = %s
            """,
            (anomaly_code,)
        )
        
        anomaly = cursor.fetchone()
        if not anomaly:
            raise HTTPException(status_code=404, detail=f"Anomaly {anomaly_code} not found")
        
        # Check if trying to change from corrected to confirmed (not allowed)
        if anomaly['status'] == AnomalyStatus.CORRECTED and update_data.status in [
            AnomalyStatus.CONFIRMED, AnomalyStatus.DISPROVED, AnomalyStatus.DISPROVED_WHISPER]:
            raise HTTPException(
                status_code=422,
                detail="Cannot change status from corrected to confirmed, disproved, or disproved_whisper"
            )
        
        # Handle voice_manual_fixes operations based on status
        if update_data.status in [AnomalyStatus.DISPROVED, AnomalyStatus.CORRECTED]:
            # Save to voice_manual_fixes for DISPROVED or CORRECTED status
            if anomaly['verse_start_time'] is not None and anomaly['verse_end_time'] is not None:
                # For CORRECTED status, use provided begin/end values; for others use original timing
                if update_data.status == AnomalyStatus.CORRECTED:
                    begin_time = update_data.begin
                    end_time = update_data.end
                else:
                    begin_time = anomaly['verse_start_time']
                    end_time = anomaly['verse_end_time']
                
                # Check if record already exists
                cursor.execute(
                    """
                    SELECT code FROM voice_manual_fixes 
                    WHERE voice = %s AND book_number = %s AND chapter_number = %s AND verse_number = %s
                    """,
                    (anomaly['voice'], anomaly['book_number'], anomaly['chapter_number'], anomaly['verse_number'])
                )
                existing_fix = cursor.fetchone()
                
                if existing_fix:
                    # Update existing record
                    cursor.execute(
                        """
                        UPDATE voice_manual_fixes 
                        SET begin = %s, end = %s, info = %s
                        WHERE code = %s
                        """,
                        (begin_time, end_time, 
                         f"Status: {update_data.status.value}", existing_fix['code'])
                    )
                else:
                    # Insert new record
                    cursor.execute(
                        """
                        INSERT INTO voice_manual_fixes (voice, book_number, chapter_number, verse_number, begin, end, info)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (anomaly['voice'], anomaly['book_number'], anomaly['chapter_number'], 
                         anomaly['verse_number'], begin_time, end_time,
                         f"Status: {update_data.status.value}")
                    )
        
        elif update_data.status == AnomalyStatus.CONFIRMED:
            # Check existing manual fixes for CONFIRMED status
            if anomaly['verse_start_time'] is not None and anomaly['verse_end_time'] is not None:
                cursor.execute(
                    """
                    SELECT code, begin, end FROM voice_manual_fixes 
                    WHERE voice = %s AND book_number = %s AND chapter_number = %s AND verse_number = %s
                    """,
                    (anomaly['voice'], anomaly['book_number'], anomaly['chapter_number'], anomaly['verse_number'])
                )
                existing_fix = cursor.fetchone()
                
                if existing_fix:
                    # Check if begin and end match
                    # Convert both values to float for comparison
                    existing_begin = float(existing_fix['begin'])
                    existing_end = float(existing_fix['end'])
                    current_begin = float(anomaly['verse_start_time'])
                    current_end = float(anomaly['verse_end_time'])
                    
                    if (abs(existing_begin - current_begin) < 0.001 and 
                        abs(existing_end - current_end) < 0.001):
                        # Times match, delete the record
                        cursor.execute(
                            "DELETE FROM voice_manual_fixes WHERE code = %s",
                            (existing_fix['code'],)
                        )
                    else:
                        # Times don't match, return error
                        raise HTTPException(
                            status_code=422, 
                            detail=f"Cannot confirm anomaly: manual fix exists with different timing. "
                                   f"Manual fix: {existing_begin}-{existing_end}, "
                                   f"Current alignment: {current_begin}-{current_end}"
                        )
        
        if update_data.status in [AnomalyStatus.ALREADY_RESOLVED, AnomalyStatus.DISPROVED_WHISPER]:
            raise HTTPException(status_code=422, detail="Cannot manually set status to already_resolved or disproved_whisper")
        
        # Update the status for all anomalies of the same verse
        cursor.execute(
            """
            UPDATE voice_anomalies 
            SET status = %s, updated_at = CASE WHEN status != %s THEN CURRENT_TIMESTAMP ELSE updated_at END
            WHERE voice = %s AND book_number = %s AND chapter_number = %s AND verse_number = %s
            """,
            (update_data.status.value, update_data.status.value, anomaly['voice'], anomaly['book_number'], 
             anomaly['chapter_number'], anomaly['verse_number'])
        )
        
        connection.commit()
        
        # Return updated anomaly
        cursor.execute(
            """
            SELECT va.code, va.voice, va.translation, va.book_number, va.chapter_number, 
                   va.verse_number, va.word, va.position_in_verse, va.position_from_end,
                   va.duration, va.speed, va.ratio, va.anomaly_type, va.status, va.updated_at,
                   al.begin AS verse_start_time, al.end AS verse_end_time,
                   tv.text AS verse_text
            FROM voice_anomalies AS va
            LEFT JOIN voice_alignments al ON (
                al.voice = va.voice AND 
                al.book_number = va.book_number AND 
                al.chapter_number = va.chapter_number AND 
                al.verse_number = va.verse_number
            )
            LEFT JOIN translation_verses tv ON (
                tv.code = va.translation_verse_id
            )
            WHERE va.code = %s
            """,
            (anomaly_code,)
        )
        
        updated_anomaly = cursor.fetchone()
        if not updated_anomaly:
            raise HTTPException(status_code=404, detail=f"Anomaly {anomaly_code} not found after update")
        
        return updated_anomaly
        
    except Exception as e:
        connection.rollback()
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        connection.close()


@api_router.post("/voices/manual-fixes", response_model=VoiceManualFixModel, operation_id="create_voice_manual_fix", tags=["Voices"])
def create_voice_manual_fix(fix_data: VoiceManualFixCreateModel, username: str = RequireJWT):
    """
    Create manual time correction for verse
    
    Allows setting correct start and end time for verse without linking to anomaly.
    If correction for this verse already exists, it will be updated.
    """
    connection = create_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        # Check that voice exists
        cursor.execute("SELECT code FROM voices WHERE code = %s", (fix_data.voice,))
        voice = cursor.fetchone()
        if not voice:
            raise HTTPException(status_code=404, detail=f"Voice {fix_data.voice} not found")
        
        # Check that verse exists
        cursor.execute(
            """
            SELECT tv.code FROM translation_verses tv
            JOIN voices v ON v.translation = tv.translation
            WHERE v.code = %s AND tv.book_number = %s AND tv.chapter_number = %s AND tv.verse_number = %s
            """,
            (fix_data.voice, fix_data.book_number, fix_data.chapter_number, fix_data.verse_number)
        )
        verse = cursor.fetchone()
        if not verse:
            raise HTTPException(
                status_code=404, 
                detail=f"Verse not found: voice {fix_data.voice}, book {fix_data.book_number}, chapter {fix_data.chapter_number}, verse {fix_data.verse_number}"
            )
        
        # Check if correction for this verse already exists
        cursor.execute(
            """
            SELECT code FROM voice_manual_fixes 
            WHERE voice = %s AND book_number = %s AND chapter_number = %s AND verse_number = %s
            """,
            (fix_data.voice, fix_data.book_number, fix_data.chapter_number, fix_data.verse_number)
        )
        existing_fix = cursor.fetchone()
        
        if existing_fix:
            # Update existing correction
            cursor.execute(
                """
                UPDATE voice_manual_fixes 
                SET begin = %s, end = %s, info = %s
                WHERE code = %s
                """,
                (fix_data.begin, fix_data.end, fix_data.info, existing_fix['code'])
            )
            fix_id = existing_fix['code']
        else:
            # Create new correction
            cursor.execute(
                """
                INSERT INTO voice_manual_fixes (voice, book_number, chapter_number, verse_number, begin, end, info)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (fix_data.voice, fix_data.book_number, fix_data.chapter_number, 
                 fix_data.verse_number, fix_data.begin, fix_data.end, fix_data.info)
            )
            fix_id = cursor.lastrowid
        
        connection.commit()
        
        # Return created/updated correction
        cursor.execute(
            """
            SELECT code, voice, book_number, chapter_number, verse_number, begin, end, info
            FROM voice_manual_fixes
            WHERE code = %s
            """,
            (fix_id,)
        )
        
        result = cursor.fetchone()
        if not result:
            raise HTTPException(status_code=500, detail="Failed to retrieve manual fix")
        
        return result
        
    except Exception as e:
        connection.rollback()
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        connection.close()


# Include main router to application
app.include_router(api_router)
