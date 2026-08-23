#!/usr/bin/env python3
"""Download MP3 audio files for active voices.

This script reads `voices.link_template` for all active voices (and active translations)
from the configured MySQL database and downloads per-chapter mp3 files into the
expected folder structure:

  <output_root>/<translation_alias>/<voice_alias>/mp3/<book_zerofill>/<chapter_zerofill>.mp3

The URL for each chapter is built by replacing `{placeholders}` in `link_template`
(similar to `/root/cep/bible-parser/include.php:get_chapter_audio_url`).

Some sources provide audio as per-book ZIP archives rather than per-chapter URLs.
For such cases, this script contains small built-in handlers.

Designed to be executed inside the `bible-api` container where DB_*/API config is
already available via `.env`.
"""

from __future__ import annotations

import argparse
import atexit
import fcntl
import os
import re
import shutil
import sys
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import mysql.connector
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


@dataclass(frozen=True)
class Voice:
    voice_code: int
    voice_alias: str
    translation_code: int
    translation_alias: str
    link_template: str


@dataclass(frozen=True)
class Book:
    number: int
    code1: str
    code2: str
    code3: str
    code4: str
    code5: str
    code6: str
    code7: str
    code8: str
    code9: str


# Built-in source: NPU (translations.code=21, translations.alias=npu) voice npu_uk (voices.code=131).
# open.bible page: https://open.bible/bibles/ukrainian-biblica-audio-nt/
# Direct downloadable ZIPs are hosted at openbible-api-1.biblica.com.
_OPENBIBLE_NPU_UK_ZIPS: Dict[int, str] = {
    40: "https://openbible-api-1.biblica.com/artifactContent/65cc98c620966e64a5564216",  # Matthew
    41: "https://openbible-api-1.biblica.com/artifactContent/65cc98c620966e64a5564218",  # Mark
    42: "https://openbible-api-1.biblica.com/artifactContent/65cc98c620966e64a5564221",  # Luke
    43: "https://openbible-api-1.biblica.com/artifactContent/65cc98c620966e64a5564219",  # John
    44: "https://openbible-api-1.biblica.com/artifactContent/65cc98c620966e64a556422a",  # Acts
    45: "https://openbible-api-1.biblica.com/artifactContent/65cc98c620966e64a5564229",  # Romans
    46: "https://openbible-api-1.biblica.com/artifactContent/65cc98c620966e64a556421d",  # 1 Corinthians
    47: "https://openbible-api-1.biblica.com/artifactContent/65cc98c620966e64a556421e",  # 2 Corinthians
    48: "https://openbible-api-1.biblica.com/artifactContent/65cc98c620966e64a556422b",  # Galatians
    49: "https://openbible-api-1.biblica.com/artifactContent/65cc98c620966e64a556422f",  # Ephesians
    50: "https://openbible-api-1.biblica.com/artifactContent/65cc98c620966e64a556421c",  # Philippians
    51: "https://openbible-api-1.biblica.com/artifactContent/65cc98c620966e64a5564220",  # Colossians
    52: "https://openbible-api-1.biblica.com/artifactContent/65cc98c620966e64a5564223",  # 1 Thessalonians
    53: "https://openbible-api-1.biblica.com/artifactContent/65cc98c620966e64a5564224",  # 2 Thessalonians
    54: "https://openbible-api-1.biblica.com/artifactContent/65cc98c620966e64a5564225",  # 1 Timothy
    55: "https://openbible-api-1.biblica.com/artifactContent/65cc98c620966e64a5564227",  # 2 Timothy
    56: "https://openbible-api-1.biblica.com/artifactContent/65cc98c620966e64a5564231",  # Titus
    57: "https://openbible-api-1.biblica.com/artifactContent/65cc98c620966e64a556421a",  # Philemon
    58: "https://openbible-api-1.biblica.com/artifactContent/65cc98c620966e64a5564222",  # Hebrews
    59: "https://openbible-api-1.biblica.com/artifactContent/65cc98c620966e64a5564230",  # James
    60: "https://openbible-api-1.biblica.com/artifactContent/65cc98c620966e64a5564226",  # 1 Peter
    61: "https://openbible-api-1.biblica.com/artifactContent/65cc98c620966e64a5564228",  # 2 Peter
    62: "https://openbible-api-1.biblica.com/artifactContent/65cc98c620966e64a556422c",  # 1 John
    63: "https://openbible-api-1.biblica.com/artifactContent/65cc98c620966e64a556422d",  # 2 John
    64: "https://openbible-api-1.biblica.com/artifactContent/65cc98c620966e64a556422e",  # 3 John
    65: "https://openbible-api-1.biblica.com/artifactContent/65cc98c620966e64a556421b",  # Jude
    66: "https://openbible-api-1.biblica.com/artifactContent/65cc98c620966e64a5564217",  # Revelation
    # Open.Bible also exposes Psalms for this product. Uncomment if needed:
    19: "https://openbible-api-1.biblica.com/artifactContent/65cc98c620966e64a556421f",  # Psalms
}


_FAIL_LINE_RE = re.compile(r"^FAIL .*?: (https?://\S+) -> (\S+)\s*$")


def _parse_fail_log(path: Path) -> List[Tuple[str, Path]]:
    jobs: List[Tuple[str, Path]] = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = _FAIL_LINE_RE.match(line)
            if not m:
                continue
            url = m.group(1)
            dest = Path(m.group(2))
            jobs.append((url, dest))
    return jobs


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _mysql_connect():
    host = os.getenv("DB_HOST", "localhost")
    port = _env_int("DB_PORT", 3306)
    user = os.getenv("DB_USER", "root")
    password = os.getenv("DB_PASSWORD", "")
    database = os.getenv("DB_NAME", "cep")

    return mysql.connector.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        autocommit=True,
    )


def _fetch_active_voices(conn) -> List[Voice]:
    cur = conn.cursor(dictionary=True)
    cur.execute(
        """
        SELECT
            v.code AS voice_code,
            v.alias AS voice_alias,
            v.translation AS translation_code,
            t.alias AS translation_alias,
            v.link_template AS link_template
        FROM voices v
        JOIN translations t ON t.code = v.translation
        WHERE v.active = 1 AND t.active = 1
        ORDER BY t.alias, v.alias
        """
    )
    rows = cur.fetchall()
    cur.close()

    voices: List[Voice] = []
    for r in rows:
        tmpl = (r.get("link_template") or "").strip()
        voices.append(
            Voice(
                voice_code=int(r["voice_code"]),
                voice_alias=str(r["voice_alias"]),
                translation_code=int(r["translation_code"]),
                translation_alias=str(r["translation_alias"]),
                link_template=tmpl,
            )
        )
    return voices


def _fetch_books(conn) -> Dict[int, Book]:
    cur = conn.cursor(dictionary=True)
    cur.execute(
        """
        SELECT number, code1, code2, code3, code4, code5, code6, code7, code8, code9
        FROM bible_books
        ORDER BY number
        """
    )
    rows = cur.fetchall()
    cur.close()

    out: Dict[int, Book] = {}
    for r in rows:
        out[int(r["number"])] = Book(
            number=int(r["number"]),
            code1=str(r.get("code1") or ""),
            code2=str(r.get("code2") or ""),
            code3=str(r.get("code3") or ""),
            code4=str(r.get("code4") or ""),
            code5=str(r.get("code5") or ""),
            code6=str(r.get("code6") or ""),
            code7=str(r.get("code7") or ""),
            code8=str(r.get("code8") or ""),
            code9=str(r.get("code9") or ""),
        )
    return out


def _fetch_chapter_counts(conn) -> Dict[int, int]:
    cur = conn.cursor(dictionary=True)
    cur.execute(
        """
        SELECT book_number, MAX(chapter_number) AS chapter_count
        FROM bible_stat
        GROUP BY book_number
        """
    )
    rows = cur.fetchall()
    cur.close()

    out: Dict[int, int] = {}
    for r in rows:
        out[int(r["book_number"])] = int(r["chapter_count"])
    return out


def _simple_template_replace(template: str, variables: Dict[str, Any]) -> str:
    out = template
    for k, v in variables.items():
        out = out.replace("{" + k + "}", str(v))
    return out


def _build_url(voice: Voice, book: Book, chapter: int) -> str:
    book_number = book.number
    book_zerofill = str(book_number).zfill(2)
    chapter_zerofill = str(chapter).zfill(2)

    vars_map = {
        "voice": voice.voice_alias,
        "book": book_number,
        "chapter": chapter,
        "book_zerofill": book_zerofill,
        "chapter_zerofill": chapter_zerofill,
        "chapter_zerofill3": str(chapter).zfill(3),
        "chapter_zerofill_ps3": str(chapter).zfill(3 if book_number == 19 else 2),
        "book_alias": book.code1,
        "translation": voice.translation_alias,
        "book_alias_upper": book.code1.upper(),
        "book_code2": book.code2,
        "book_code3": book.code3,
        "book_code4": book.code4,
        "book_code5": book.code5,
        "book_code6": book.code6,
        "book_code7": book.code7,
        "book_code8": book.code8,
        "book_code9": book.code9,
    }

    return _simple_template_replace(voice.link_template, vars_map)


def _dest_path(output_root: Path, voice: Voice, book_number: int, chapter: int) -> Path:
    book0 = str(book_number).zfill(2)
    chapter0 = str(chapter).zfill(2) if chapter < 100 else str(chapter)
    return output_root / voice.translation_alias / voice.voice_alias / "mp3" / book0 / f"{chapter0}.mp3"


def _acquire_lock(lock_path: Path) -> int:
    # Exclusive advisory lock; auto-released on process exit.
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise

    os.ftruncate(fd, 0)
    os.write(fd, f"pid={os.getpid()}\n".encode("utf-8"))

    def _cleanup() -> None:
        try:
            os.close(fd)
        except Exception:
            pass

    atexit.register(_cleanup)
    return fd


def _requests_session(user_agent: str, retries: int, backoff: float) -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=retries,
        connect=retries,
        read=retries,
        status=retries,
        backoff_factor=backoff,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "HEAD"),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=32, pool_maxsize=32)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    s.headers.update({"User-Agent": user_agent, "Accept": "audio/mpeg,*/*"})
    return s


def _looks_like_mp3(first_bytes: bytes) -> bool:
    # ID3 tag or frame sync 0xFFEx
    return first_bytes.startswith(b"ID3") or (
        len(first_bytes) >= 2 and first_bytes[0] == 0xFF and (first_bytes[1] & 0xE0) == 0xE0
    )


def _download_one(
    session: requests.Session,
    url: str,
    dest: Path,
    timeout: float,
    dry_run: bool,
    skip_existing: bool,
    missing_policy: str,
) -> Tuple[bool, str]:
    if skip_existing and dest.exists() and dest.stat().st_size > 0:
        return True, "exists"

    if dry_run:
        return True, "dry-run"

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")

    r = session.get(url, stream=True, timeout=timeout, allow_redirects=True)
    if r.status_code != 200:
        if missing_policy == "skip" and r.status_code in (404, 410):
            return True, f"missing:{r.status_code}"
        return False, f"http {r.status_code}"

    # Basic sanity check: make sure we are not saving HTML error pages.
    it = r.iter_content(chunk_size=64 * 1024)
    try:
        first = next(it)
    except StopIteration:
        return False, "empty"

    if not _looks_like_mp3(first[:16]):
        ct = (r.headers.get("Content-Type") or "").lower()
        return False, f"not-mp3 content-type={ct!r}"

    with open(tmp, "wb") as f:
        f.write(first)
        for chunk in it:
            if not chunk:
                continue
            f.write(chunk)

    os.replace(tmp, dest)
    return True, "ok"


def _openbible_zip_sources(voice: Voice) -> Optional[Dict[int, str]]:
    if voice.translation_alias == "npu" and voice.voice_alias == "npu_uk":
        return _OPENBIBLE_NPU_UK_ZIPS
    return None


def _download_zip_with_resume(session: requests.Session, url: str, dest: Path, timeout: float) -> Tuple[bool, str]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")

    # Resume if we already have a partial download.
    existing = tmp.stat().st_size if tmp.exists() else 0
    headers: Dict[str, str] = {}
    mode = "wb"
    if existing > 0:
        headers["Range"] = f"bytes={existing}-"
        mode = "ab"

    r = session.get(url, stream=True, timeout=timeout, allow_redirects=True, headers=headers)

    if existing > 0 and r.status_code == 200:
        # Server ignored Range; restart from scratch.
        existing = 0
        mode = "wb"

    if r.status_code not in (200, 206):
        return False, f"http {r.status_code}"

    with open(tmp, mode) as f:
        for chunk in r.iter_content(chunk_size=1024 * 1024):
            if not chunk:
                continue
            f.write(chunk)

    os.replace(tmp, dest)
    return True, "ok"


_CH_FROM_NAME_RE = re.compile(r"(\d{1,3})(?=\.mp3$)", re.IGNORECASE)


def _chapter_from_member_name(member_name: str) -> Optional[int]:
    base = Path(member_name).name
    matches = _CH_FROM_NAME_RE.findall(base)
    if not matches:
        return None
    try:
        ch = int(matches[-1])
    except ValueError:
        return None
    if ch <= 0:
        return None
    return ch


def _extract_zip_mp3(
    zip_path: Path,
    dest_dir: Path,
    book_number: int,
    skip_existing: bool,
) -> Tuple[int, int, int, str]:
    """Extract mp3 entries from a per-book ZIP into <dest_dir>/<chapter>.mp3.

    Returns: (ok, skipped, failed, msg)
    """

    ok = 0
    skipped = 0
    failed = 0

    dest_dir.mkdir(parents=True, exist_ok=True)

    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        return 0, 0, 1, "bad-zip"

    with zf:
        mp3_members = [i for i in zf.infolist() if not i.is_dir() and i.filename.lower().endswith(".mp3")]
        if not mp3_members:
            return 0, 0, 1, "no-mp3"

        for info in mp3_members:
            ch = _chapter_from_member_name(info.filename)
            if ch is None:
                # Unknown naming; keep going.
                failed += 1
                continue

            dest = dest_dir / (f"{str(ch).zfill(2)}.mp3" if ch < 100 else f"{ch}.mp3")
            if skip_existing and dest.exists() and dest.stat().st_size > 0:
                skipped += 1
                continue

            tmp = dest.with_suffix(dest.suffix + ".part")
            try:
                with zf.open(info, "r") as src, open(tmp, "wb") as out:
                    shutil.copyfileobj(src, out, length=1024 * 1024)
                os.replace(tmp, dest)
                ok += 1
            except Exception:
                failed += 1
                try:
                    if tmp.exists():
                        tmp.unlink()
                except Exception:
                    pass

    return ok, skipped, failed, "ok"


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Download mp3 files for all active voices (voices.active=1 AND translations.active=1)."
    )
    ap.add_argument(
        "--output-root",
        default=os.getenv("MP3_FILES_PATH", "/audio"),
        help="Root dir for mp3 storage (default: MP3_FILES_PATH env or /audio).",
    )
    ap.add_argument("--max-workers", type=int, default=8, help="Concurrent downloads (default: 8).")
    ap.add_argument("--timeout", type=float, default=60.0, help="HTTP timeout in seconds (default: 60).")
    ap.add_argument("--retries", type=int, default=5, help="Retry count for network/http errors (default: 5).")
    ap.add_argument("--backoff", type=float, default=0.5, help="Retry backoff factor (default: 0.5).")
    ap.add_argument("--user-agent", default="bible-api-audio-downloader/1.1", help="HTTP User-Agent.")
    ap.add_argument("--translation-alias", action="append", default=[], help="Filter by translation alias (repeatable).")
    ap.add_argument("--voice-alias", action="append", default=[], help="Filter by voice alias (repeatable).")
    ap.add_argument("--book-number", action="append", type=int, default=[], help="Limit to specific book numbers (repeatable).")
    ap.add_argument("--dry-run", action="store_true", help="Do not download, only print planned actions.")
    ap.add_argument("--no-skip-existing", action="store_true", help="Re-download even if destination file exists.")
    ap.add_argument("--yes", action="store_true", help="Required to actually download (safety flag).")
    ap.add_argument(
        "--lock-file",
        default="",
        help="Lock file path to prevent concurrent runs (default: <output-root>/.download_audio.lock).",
    )
    ap.add_argument("--retry-from-log", default="", help="Retry only failed downloads from a previous run log file (lines starting with 'FAIL ...').")
    ap.add_argument(
        "--missing-policy",
        choices=("error", "skip"),
        default="error",
        help="How to treat missing files (HTTP 404/410). 'skip' will not count them as failures.",
    )
    ap.add_argument(
        "--keep-archives",
        action="store_true",
        help="Keep downloaded ZIP archives for built-in sources (default: delete after successful extraction).",
    )
    args = ap.parse_args()

    output_root = Path(args.output_root).resolve()
    skip_existing = not args.no_skip_existing

    book_allow = set(args.book_number) if args.book_number else None

    if not args.dry_run and not args.yes:
        print("Refusing to run without --yes (this may download tens of GB). Use --dry-run to preview.")
        return 2

    lock_file = Path(args.lock_file) if args.lock_file else (output_root / ".download_audio.lock")
    if not args.dry_run:
        try:
            _acquire_lock(lock_file)
            print(f"Lock acquired: {lock_file} (pid={os.getpid()})")
        except BlockingIOError:
            print(f"Another download is already running (lock busy): {lock_file}", file=sys.stderr)
            return 2

    from concurrent.futures import ThreadPoolExecutor, as_completed

    session = _requests_session(args.user_agent, args.retries, args.backoff)

    ok = 0
    skipped = 0
    failed = 0

    start = time.time()

    if args.retry_from_log:
        log_path = Path(args.retry_from_log)
        if not log_path.exists():
            print(f"--retry-from-log file not found: {log_path}", file=sys.stderr)
            return 2
        jobs = _parse_fail_log(log_path)
        # Keep only destinations under output_root (safety).
        filtered: List[Tuple[str, Path]] = []
        for url, dest in jobs:
            try:
                if dest.resolve().is_relative_to(output_root):
                    filtered.append((url, dest))
            except Exception:
                pass
        jobs = filtered
        print(f"Output root: {output_root}")
        print(f"Retry mode: {len(jobs)} failed jobs from {log_path}")
        openbible_voices: List[Voice] = []
    else:
        conn = _mysql_connect()
        try:
            voices = _fetch_active_voices(conn)
            books = _fetch_books(conn)
            chapter_counts = _fetch_chapter_counts(conn)
        finally:
            conn.close()

        if args.translation_alias:
            allow = set(args.translation_alias)
            voices = [v for v in voices if v.translation_alias in allow]

        if args.voice_alias:
            allow = set(args.voice_alias)
            voices = [v for v in voices if v.voice_alias in allow]

        voices_no_template = [v for v in voices if not v.link_template]
        voices = [v for v in voices if v.link_template]

        openbible_voices = [v for v in voices_no_template if _openbible_zip_sources(v)]
        voices_no_template = [v for v in voices_no_template if not _openbible_zip_sources(v)]

        if voices_no_template:
            print("Skipping voices without link_template (no built-in handler):")
            for v in voices_no_template:
                print(f"  - {v.translation_alias}/{v.voice_alias} (voice_code={v.voice_code})")

        if openbible_voices:
            print("Using built-in ZIP handlers for:")
            for v in openbible_voices:
                print(f"  - {v.translation_alias}/{v.voice_alias} (voice_code={v.voice_code})")

        total_tasks = 0
        book_nums = sorted(chapter_counts.keys())
        if book_allow is not None:
            book_nums = [bn for bn in book_nums if bn in book_allow]
        for _v in voices:
            total_tasks += sum(chapter_counts.get(bn, 0) for bn in book_nums)

        print(f"Output root: {output_root}")
        print(f"Voices with link_template: {len(voices)}")
        if openbible_voices:
            print(f"Voices with ZIP handler: {len(openbible_voices)}")
        print(f"Planned chapter downloads (upper bound): {total_tasks}")

        def iter_jobs() -> Iterable[Tuple[str, Path]]:
            for v in voices:
                for book_number, max_ch in sorted(chapter_counts.items()):
                    if book_allow is not None and book_number not in book_allow:
                        continue
                    book = books.get(book_number)
                    if not book:
                        continue
                    for ch in range(1, max_ch + 1):
                        url = _build_url(v, book, ch)
                        dest = _dest_path(output_root, v, book_number, ch)
                        yield url, dest

        jobs = list(iter_jobs())

    if args.dry_run:
        if not args.retry_from_log:
            for v in openbible_voices:
                sources = _openbible_zip_sources(v) or {}
                for book_number, url in sorted(sources.items()):
                    if book_allow is not None and book_number not in book_allow:
                        continue
                    book0 = str(book_number).zfill(2)
                    dest_dir = output_root / v.translation_alias / v.voice_alias / "mp3" / book0
                    print(f"DRY openbible {url} -> {dest_dir}")

        for url, dest in jobs[:50]:
            print(f"DRY {url} -> {dest}")
        if len(jobs) > 50:
            print(f"... ({len(jobs)-50} more)")
        return 0

    # 1) Built-in ZIP handlers (sequential).
    for v in openbible_voices:
        sources = _openbible_zip_sources(v) or {}
        archives_dir = output_root / ".openbible_archives" / v.translation_alias / v.voice_alias
        for book_number, url in sorted(sources.items()):
            if book_allow is not None and book_number not in book_allow:
                continue
            book0 = str(book_number).zfill(2)
            zip_path = archives_dir / f"{book0}.zip"
            dest_dir = output_root / v.translation_alias / v.voice_alias / "mp3" / book0

            if skip_existing and dest_dir.exists():
                # Heuristic: if dest_dir already has at least one mp3, assume extracted.
                try:
                    if any(p.suffix.lower() == ".mp3" for p in dest_dir.iterdir()):
                        print(f"ZIP {v.translation_alias}/{v.voice_alias} book={book0}: already extracted -> {dest_dir}")
                        continue
                except Exception:
                    pass

            print(f"ZIP {v.translation_alias}/{v.voice_alias} book={book0}: downloading {url}")
            ok_dl, msg_dl = _download_zip_with_resume(session, url, zip_path, args.timeout)
            if not ok_dl:
                failed += 1
                print(f"FAIL zip-download {msg_dl}: {url} -> {zip_path}")
                continue

            ok_ex, sk_ex, fail_ex, msg_ex = _extract_zip_mp3(zip_path, dest_dir, book_number, skip_existing)
            ok += ok_ex
            skipped += sk_ex
            failed += fail_ex

            print(
                f"ZIP {v.translation_alias}/{v.voice_alias} book={book0}: extracted ok={ok_ex} skipped={sk_ex} failed={fail_ex} -> {dest_dir}"
            )

            if fail_ex == 0 and not args.keep_archives:
                try:
                    zip_path.unlink()
                except Exception:
                    pass

    # 2) Template-based per-chapter downloads (concurrent).
    if jobs:
        print(f"Starting chapter downloads with max_workers={args.max_workers}...")

        with ThreadPoolExecutor(max_workers=args.max_workers) as ex:
            futs = {}
            for url, dest in jobs:
                fut = ex.submit(
                    _download_one,
                    session,
                    url,
                    dest,
                    args.timeout,
                    False,
                    skip_existing,
                    args.missing_policy,
                )
                futs[fut] = (url, dest)

            last_report = 0.0
            for fut in as_completed(futs):
                url, dest = futs[fut]
                try:
                    success, msg = fut.result()
                except Exception as e:
                    success, msg = False, f"exc {type(e).__name__}: {e}"

                if success:
                    if msg == "exists" or msg.startswith("missing:"):
                        skipped += 1
                    else:
                        ok += 1
                else:
                    failed += 1
                    print(f"FAIL {msg}: {url} -> {dest}")

                now = time.time()
                if now - last_report >= 10:
                    done = ok + skipped + failed
                    rate = done / max(1.0, now - start)
                    print(
                        f"Progress: done={done}/{len(jobs)} ok={ok} skipped={skipped} failed={failed} rate={rate:.1f}/s"
                    )
                    last_report = now

    print(f"Finished: ok={ok} skipped={skipped} failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
