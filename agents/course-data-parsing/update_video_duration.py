#!/usr/bin/env python3
"""Update lesson durations from their YouTube video IDs."""

import logging
import os
import sys

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
logger = logging.getLogger("VideoDurationUpdater")


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


def update_video_durations():
    if pymysql is None:
        raise ImportError("Package 'pymysql' is required. Install it with: pip install pymysql")
    if yt_dlp is None:
        raise ImportError("Package 'yt-dlp' is required. Install it with: pip install yt-dlp")

    connection = None
    updated = 0
    skipped = 0

    try:
        connection = pymysql.connect(**get_db_config())
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, youtube_video_id FROM video_lessons "
                "WHERE duration_seconds = 0 "
                "AND youtube_video_id IS NOT NULL AND youtube_video_id <> ''"
            )
            lessons = cursor.fetchall()
            logger.info("Found %d zero-duration lessons with a YouTube video ID", len(lessons))

            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as youtube_dl:
                for lesson_id, video_id in lessons:
                    video_id = (video_id or "").strip()
                    if not video_id:
                        skipped += 1
                        continue

                    video_url = f"https://www.youtube.com/watch?v={video_id}"
                    try:
                        details = youtube_dl.extract_info(video_url, download=False)
                        duration = details.get("duration") if details else None
                    except Exception as exc:
                        logger.warning(
                            "Could not retrieve duration for lesson ID %s (%s): %s",
                            lesson_id,
                            video_id,
                            exc,
                        )
                        skipped += 1
                        continue

                    if duration is None:
                        logger.warning("Duration unavailable for lesson ID %s (%s)", lesson_id, video_id)
                        skipped += 1
                        continue

                    duration_seconds = int(duration)
                    cursor.execute(
                        "UPDATE video_lessons SET duration_seconds = %s "
                        "WHERE id = %s AND duration_seconds = 0",
                        (duration_seconds, lesson_id),
                    )
                    if cursor.rowcount:
                        connection.commit()
                        updated += 1
                        logger.info("Updated lesson ID %s: %d seconds", lesson_id, duration_seconds)
                    else:
                        connection.rollback()
                        skipped += 1

        connection.commit()
        logger.info("Updated %d lesson durations; skipped %d", updated, skipped)
        return updated
    except Exception:
        if connection is not None:
            connection.rollback()
        logger.exception("Duration update failed; database changes were rolled back")
        raise
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    try:
        update_video_durations()
    except Exception:
        sys.exit(1)
