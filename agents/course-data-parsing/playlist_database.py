#!/usr/bin/env python3
"""Import YouTube playlists discovered from a search URL into MySQL.

Set the MYSQL_* environment variables, fill in playlist.csv, then run:
    python playlist_database.py

To run one search directly:
    python playlist_database.py "https://www.youtube.com/results?search_query=python+full+course"

The search is filtered to playlist results. A single playlist URL or ID is also
accepted. No YouTube Data API key is needed.
"""

import argparse
import csv
import difflib
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

try:
    import pymysql
except ImportError:
    pymysql = None

try:
    import yt_dlp
except ImportError:
    yt_dlp = None


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("PlaylistImporter")
PLAYLIST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
SCRIPT_DIR = Path(__file__).resolve().parent
MAX_COURSES_PER_SEARCH = 50


def extract_playlist_id(value):
    """Accept a playlist ID or a YouTube URL containing a ``list`` parameter."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if "://" not in value and PLAYLIST_ID_PATTERN.fullmatch(value):
        return value

    parsed = urlparse(value if "://" in value else "https://" + value)
    playlist_id = parse_qs(parsed.query).get("list", [None])[0]
    if playlist_id and PLAYLIST_ID_PATTERN.fullmatch(playlist_id):
        return playlist_id
    return None


def get_db_config():
    return {
        "host": os.getenv("MYSQL_HOST", "localhost"),
        "port": int(os.getenv("MYSQL_PORT", "3306")),
        "user": os.getenv("MYSQL_USER", "root"),
        "password": os.getenv("MYSQL_PASSWORD", "MySQL$123"),
        "database": os.getenv("MYSQL_DATABASE", "codexa"),
        "charset": "utf8mb4",
        "autocommit": False,
    }


def get_playlist_url(playlist_input, playlist_id):
    """Preserve a supplied playlist URL; build one for a bare playlist ID."""
    if "://" in playlist_input:
        return playlist_input
    return f"https://www.youtube.com/playlist?list={playlist_id}"


def read_playlist(playlist_input, playlist_id):
    """Extract playlist and flat entry metadata without resolving every video.

    Flat extraction avoids requesting playback metadata for each entry, which is
    where YouTube rejects members-only videos and would otherwise emit errors.
    """
    url = get_playlist_url(playlist_input, playlist_id)
    options = {
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "skip_download": True,
        "extract_flat": True,
    }
    with yt_dlp.YoutubeDL(options) as downloader:
        info = downloader.extract_info(url, download=False)
    if not info:
        raise ValueError(f"Playlist {playlist_id!r} could not be read")

    title = (info.get("title") or "").strip() or f"YouTube playlist {playlist_id}"
    channel = (info.get("channel") or info.get("uploader") or "").strip() or "Unknown channel"
    description = info.get("description") or ""
    entries = list(info.get("entries") or [])
    videos = []
    skipped_entries = []
    for entry_number, entry in enumerate(entries, start=1):
        if not entry:
            skipped_entries.append(f"entry {entry_number} has no metadata")
            continue
        availability = (entry.get("availability") or "").lower()
        if availability in {"subscriber_only", "members_only", "needs_auth", "private", "unavailable"}:
            skipped_entries.append(f"entry {entry_number} is {availability.replace('_', ' ')}")
            continue
        video_id = entry.get("id")
        video_title = (entry.get("title") or "").strip()
        if not video_id or not video_title or video_title.lower() in {"private video", "deleted video"}:
            skipped_entries.append(f"entry {entry_number} has no usable video ID or title")
            continue
        try:
            order = int(entry.get("playlist_index") or len(videos) + 1)
        except (TypeError, ValueError):
            order = len(videos) + 1
        duration = entry.get("duration") or 0
        try:
            duration = int(duration)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid duration for playlist entry {entry_number}: {duration!r}") from exc
        if duration < 0:
            raise ValueError(f"Negative duration for playlist entry {entry_number}")
        videos.append({
            "title": video_title,
            "video_id": video_id,
            "duration": duration,
            "order_index": order,
        })
    expected_count = info.get("playlist_count")
    if expected_count is not None:
        try:
            expected_count = int(expected_count)
        except (TypeError, ValueError):
            expected_count = len(entries)
    else:
        expected_count = len(entries)
    if skipped_entries or len(videos) != expected_count:
        details = "; ".join(skipped_entries[:5]) or "playlist entry count did not match extracted lessons"
        raise ValueError(
            f"Incomplete playlist metadata: expected {expected_count} lessons, "
            f"got {len(videos)} usable lessons ({details})"
        )
    if not videos:
        raise ValueError(f"Playlist {playlist_id!r} has no accessible video lessons")
    return title, channel, description, videos


def find_playlists(search_url):
    """Return distinct playlist IDs and titles from a YouTube search URL.

    yt-dlp's YouTube search URL extractor supports YouTube's result filters;
    adding the playlist filter avoids importing individual video search results.
    """
    parsed = urlparse(search_url)
    query = parse_qs(parsed.query)
    if (parsed.hostname or "").lower() not in {"youtube.com", "www.youtube.com"} or parsed.path not in {"/results", "/search"}:
        raise ValueError("Provide a YouTube search URL such as youtube.com/results?search_query=...")
    if not (query.get("search_query") or query.get("q")):
        raise ValueError("The search URL must include search_query=... or q=...")

    # YouTube's playlist type filter. Preserve other search parameters supplied
    # by the caller while overriding any filter that would exclude playlists.
    query["sp"] = ["EgIQAw%3D%3D"]
    filtered_url = urlunparse(parsed._replace(query=urlencode(query, doseq=True)))
    options = {
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "skip_download": True,
        "extract_flat": True,
    }
    with yt_dlp.YoutubeDL(options) as downloader:
        result = downloader.extract_info(filtered_url, download=False)
    if not result:
        raise ValueError("YouTube returned no search results")

    playlists = []
    seen = set()
    for entry in result.get("entries") or []:
        if not entry:
            continue
        entry_url = entry.get("url") or ""
        entry_query = parse_qs(urlparse(entry_url).query)
        candidate_id = (entry_query.get("list") or [None])[0]
        if not candidate_id and entry.get("ie_key") in {"YoutubeTab", "YoutubePlaylist"}:
            possible_id = entry.get("id") or ""
            if possible_id.startswith(("PL", "UU", "LL", "FL", "WL", "RD", "OLAK5uy")):
                candidate_id = possible_id
        if candidate_id and candidate_id not in seen:
            seen.add(candidate_id)
            playlists.append({
                "playlist_id": candidate_id,
                "title": entry.get("title") or candidate_id,
                "url": f"https://www.youtube.com/playlist?list={candidate_id}",
            })
    return playlists


def resolve_course_category(cursor, requested_category):
    """Resolve a category against existing course rows or an explicit ID.

    The repository does not define a category table, so existing course rows
    are the available source of category IDs unless COURSE_CATEGORY_ID is set.
    """
    category_id_text = os.getenv("COURSE_CATEGORY_ID", "").strip()
    requested_category = (
        requested_category or os.getenv("COURSE_CATEGORY", "").strip() or "General"
    )[:120]
    if category_id_text:
        try:
            category_id = int(category_id_text)
        except ValueError as exc:
            raise ValueError("COURSE_CATEGORY_ID must be an integer") from exc
        if category_id <= 0:
            raise ValueError("COURSE_CATEGORY_ID must be a positive integer")
        return requested_category, category_id

    cursor.execute(
        """SELECT DISTINCT category, category_id
           FROM video_courses
           WHERE category IS NOT NULL AND category_id IS NOT NULL
           ORDER BY category_id ASC"""
    )
    rows = cursor.fetchall()
    if not rows:
        raise ValueError(
            "No existing course category is available to reuse. Set COURSE_CATEGORY_ID "
            "to a valid category ID for the first import."
        )
    requested_tokens = {
        token for token in re.findall(r"[a-z0-9]+", requested_category.lower())
        if token not in {"full", "course", "in", "the", "and", "for", "tutorial", "complete"}
    }

    def category_score(row):
        name = str(row[0])
        category_tokens = set(re.findall(r"[a-z0-9]+", name.lower()))
        overlap = len(requested_tokens & category_tokens)
        return (overlap > 0, overlap, difflib.SequenceMatcher(None, requested_category.lower(), name.lower()).ratio())

    existing_category, category_id = max(rows, key=category_score)
    if existing_category.lower() != requested_category.lower():
        logger.warning(
            "No exact category match for %r; using existing category %r (ID %s)",
            requested_category,
            existing_category,
            category_id,
        )
    return existing_category[:120], int(category_id)


def import_playlist(playlist_input, category=None):
    if pymysql is None:
        raise ImportError("Package 'pymysql' is required. Install it with: pip install pymysql")
    if yt_dlp is None:
        raise ImportError("Package 'yt-dlp' is required. Install it with: pip install yt-dlp")

    playlist_id = extract_playlist_id(playlist_input)
    if not playlist_id:
        raise ValueError("Pass a YouTube playlist URL containing list=... or a playlist ID")
    category = (category or "General")[:120]

    connection = None
    try:
        title, channel, description, videos = read_playlist(playlist_input, playlist_id)
        title = title[:180]
        channel = channel[:160]
        videos = [dict(video, title=video["title"][:180]) for video in videos]
        logger.info("Playlist %r by %s: %d available videos", title, channel, len(videos))

        connection = pymysql.connect(**get_db_config())
        with connection.cursor() as cursor:
            cursor.execute(
                """SELECT TABLE_NAME, ENGINE
                   FROM information_schema.TABLES
                   WHERE TABLE_SCHEMA = DATABASE()
                     AND TABLE_NAME IN (%s, %s, %s)""",
                ("video_courses", "video_sections", "video_lessons"),
            )
            engines = {table_name: (engine or "").upper() for table_name, engine in cursor.fetchall()}
            required_tables = {"video_courses", "video_sections", "video_lessons"}
            missing_tables = required_tables - engines.keys()
            if missing_tables:
                raise RuntimeError(
                    "Required database tables were not found: " + ", ".join(sorted(missing_tables))
                )
            non_transactional = [
                table for table, engine in engines.items()
                if engine not in {"INNODB", "XTRADB", "NDBCLUSTER"}
            ]
            if non_transactional:
                raise RuntimeError(
                    "Cannot guarantee playlist rollback because these tables are not "
                    "transactional: " + ", ".join(sorted(non_transactional))
                )

            connection.begin()
            category, category_id = resolve_course_category(cursor, category)
            cursor.execute(
                """INSERT INTO video_courses
                   (title, description, instructor_name, category, category_id, active)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (title, description, channel, category, category_id, 1),
            )
            if cursor.rowcount != 1 or not cursor.lastrowid:
                raise RuntimeError("Course insert did not create exactly one course row")
            course_id = cursor.lastrowid
            cursor.execute(
                "INSERT INTO video_sections (course_id, title, order_index) VALUES (%s, %s, %s)",
                (course_id, "Main Course Content", 1),
            )
            if cursor.rowcount != 1 or not cursor.lastrowid:
                raise RuntimeError("Section insert did not create exactly one section row")
            section_id = cursor.lastrowid

            inserted_lessons = 0
            for video in videos:
                cursor.execute(
                    """INSERT INTO video_lessons
                       (section_id, title, youtube_video_id, duration_seconds, order_index)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (
                        section_id,
                        video["title"],
                        video["video_id"],
                        video["duration"],
                        video["order_index"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(
                        f"Lesson insert affected {cursor.rowcount} rows for video {video['video_id']}"
                    )
                inserted_lessons += cursor.rowcount
            if inserted_lessons != len(videos):
                raise RuntimeError(
                    f"Inserted {inserted_lessons} of {len(videos)} required lessons"
                )
        connection.commit()
        logger.info(
            "Committed course ID %s with its section and all %d lessons",
            course_id,
            inserted_lessons,
        )
        return course_id
    except Exception:
        if connection is not None:
            try:
                connection.rollback()
                logger.warning("Rolled back all inserts for playlist %s", playlist_id)
            except Exception:
                logger.exception("Rollback failed for playlist %s", playlist_id)
        raise
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                # A close failure after a successful COMMIT must not report the
                # playlist as failed after its data has already been committed.
                logger.exception("Failed to close database connection for playlist %s", playlist_id)


def append_failed_playlist(
    failed_path, playlist_value, error, playlist_title="", search_url="", category=""
):
    """Append a failed playlist and its error to the failure report CSV."""
    failed_path = Path(failed_path)
    failed_path.parent.mkdir(parents=True, exist_ok=True)
    output_row = {
        "search_url": search_url,
        "category": category,
        "playlist_title": playlist_title,
        "playlist": playlist_value,
        "error": str(error),
        "failed_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    existing_rows = []
    fieldnames = []
    if failed_path.exists() and failed_path.stat().st_size > 0:
        with failed_path.open("r", encoding="utf-8-sig", newline="") as failed_file:
            existing_reader = csv.DictReader(failed_file)
            fieldnames = existing_reader.fieldnames or []
            existing_rows = list(existing_reader)
    fieldnames = list(dict.fromkeys([*fieldnames, *output_row.keys()]))
    with failed_path.open("w", encoding="utf-8", newline="") as failed_file:
        writer = csv.DictWriter(failed_file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(existing_rows)
        writer.writerow(output_row)


def process_search_url(search_url, failed_path, category=None):
    """Import up to 50 discovered playlist courses for one search URL."""
    succeeded = 0
    failed = 0
    playlists = find_playlists(search_url)
    if not playlists:
        error = "No playlist results were found for this search URL"
        append_failed_playlist(
            failed_path, "", error, search_url=search_url, category=category or ""
        )
        raise ValueError(error)

    total_found = len(playlists)
    playlists = playlists[:MAX_COURSES_PER_SEARCH]
    logger.info(
        "Found %d playlist results; processing %d (limit %d per search)",
        total_found,
        len(playlists),
        MAX_COURSES_PER_SEARCH,
    )
    if not category:
        search_query = parse_qs(urlparse(search_url).query)
        category = " ".join(
            (search_query.get("search_query") or search_query.get("q") or [""])[0].split()
        ) or "General"
    for playlist in playlists:
        try:
            import_playlist(playlist["url"], category=category)
            succeeded += 1
        except Exception as exc:
            # import_playlist rolls back course, section, and lesson inserts
            # together before propagating the exception.
            logger.exception("Playlist import failed: %s", playlist["title"])
            append_failed_playlist(
                failed_path,
                playlist["url"],
                exc,
                playlist_title=playlist["title"],
                search_url=search_url,
                category=category,
            )
            failed += 1

    logger.info("Search import complete: %d succeeded, %d failed", succeeded, failed)
    return succeeded, failed


def process_playlists_csv(csv_path, failed_path):
    """Read search URLs and categories from CSV and process each row independently."""
    csv_path = Path(csv_path)
    if not csv_path.is_file():
        raise FileNotFoundError(f"Playlist configuration CSV was not found: {csv_path}")

    total_succeeded = 0
    total_failed = 0
    with csv_path.open("r", encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        if not reader.fieldnames:
            raise ValueError(f"Playlist configuration CSV is empty: {csv_path}")
        columns = {name.strip().lower(): name for name in reader.fieldnames if name}
        url_column = next(
            (columns[name] for name in ("playlist_url", "search_url", "url", "playlist") if name in columns),
            None,
        )
        if not url_column:
            raise ValueError("playlist.csv must contain a playlist_url or search_url column")
        category_column = columns.get("category")

        for line_number, row in enumerate(reader, start=2):
            search_url = (row.get(url_column) or "").strip()
            category = (row.get(category_column) or "").strip() if category_column else ""
            if not search_url:
                error = f"CSV row {line_number} has an empty search URL"
                logger.error(error)
                append_failed_playlist(
                    failed_path, "", error, search_url=search_url, category=category
                )
                total_failed += 1
                continue
            try:
                succeeded, failed = process_search_url(search_url, failed_path, category=category or None)
                total_succeeded += succeeded
                total_failed += failed
            except Exception as exc:
                logger.exception("Search URL on CSV row %d failed", line_number)
                append_failed_playlist(
                    failed_path, "", exc, search_url=search_url, category=category
                )
                total_failed += 1

    logger.info(
        "Playlist CSV complete: %d courses imported, %d failures",
        total_succeeded,
        total_failed,
    )
    return total_succeeded, total_failed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "search_url",
        nargs="?",
        help="Optional single playlist URL/ID or search URL; defaults to playlist.csv",
    )
    parser.add_argument(
        "--csv",
        dest="csv_path",
        default=str(SCRIPT_DIR / "playlist.csv"),
        help="Playlist search/category configuration CSV",
    )
    parser.add_argument(
        "--failed-csv",
        default=str(SCRIPT_DIR / "failed_playlists.csv"),
        help="Failure report CSV (default: failed_playlists.csv beside this script)",
    )
    args = parser.parse_args(argv)
    if not args.search_url:
        try:
            _, failed = process_playlists_csv(args.csv_path, args.failed_csv)
            return 1 if failed else 0
        except Exception as exc:
            logger.error("Could not process playlist CSV: %s", exc)
            return 1
    if extract_playlist_id(args.search_url):
        try:
            import_playlist(args.search_url)
            return 0
        except Exception as exc:
            logger.exception("Playlist import failed: %s", args.search_url)
            append_failed_playlist(args.failed_csv, args.search_url, exc)
            return 1
    try:
        _, failed = process_search_url(args.search_url, args.failed_csv)
        return 1 if failed else 0
    except Exception as exc:
        logger.error("Could not process YouTube search URL: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
