#!/usr/bin/env python3
"""Import course records from video_courses.csv into MySQL."""

import csv
import logging
import os
import sys
import time
from pathlib import Path

try:
    import pymysql
except ImportError:
    pymysql = None


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("CourseDataParser")


def get_db_config():
    """Read connection settings from environment, with local defaults."""
    return {
        "host": os.getenv("MYSQL_HOST", "localhost"),
        "port": int(os.getenv("MYSQL_PORT", "3306")),
        "user": os.getenv("MYSQL_USER", "root"),
        "password": os.getenv("MYSQL_PASSWORD", "MySQL$123"),
        "database": os.getenv("MYSQL_DATABASE", "codexa"),
        "charset": "utf8mb4",
        "autocommit": False,
    }


def insert_courses_from_csv():
    if pymysql is None:
        raise ImportError("Package 'pymysql' is required. Install it with: pip install pymysql")

    csv_path = Path(__file__).resolve().with_name("video_courses.csv")
    if not csv_path.is_file():
        raise FileNotFoundError(f"Course CSV file was not found: {csv_path}")

    insert_sql = """
        INSERT INTO video_courses
            (title, description, instructor_name, category, active)
        VALUES (%s, %s, %s, %s, %s)
    """
    exists_sql = "SELECT 1 FROM video_courses WHERE LOWER(title) = LOWER(%s) LIMIT 1"
    required_columns = {"title", "category"}
    start_time = time.perf_counter()
    connection = None
    inserted = 0
    skipped = 0

    try:
        connection = pymysql.connect(**get_db_config())
        with csv_path.open("r", encoding="utf-8-sig", newline="") as csv_file:
            reader = csv.DictReader(csv_file)
            if reader.fieldnames is None:
                raise ValueError("The course CSV is empty or has no header row")
            missing = required_columns - set(reader.fieldnames)
            if missing:
                raise ValueError(f"CSV is missing required columns: {', '.join(sorted(missing))}")

            with connection.cursor() as cursor:
                for line_number, row in enumerate(reader, start=2):
                    title = (row.get("title") or "").strip()
                    category = (row.get("category") or "").strip()
                    if not title or not category:
                        raise ValueError(
                            f"CSV row {line_number} must contain a title and category"
                        )

                    active_text = (row.get("active") or "1").strip() or "1"
                    try:
                        active = int(active_text)
                    except ValueError as exc:
                        raise ValueError(
                            f"CSV row {line_number} has invalid active value: {active_text!r}"
                        ) from exc
                    if active not in (0, 1):
                        raise ValueError(
                            f"CSV row {line_number} active must be 0 or 1, got {active}"
                        )

                    cursor.execute(exists_sql, (title,))
                    if cursor.fetchone() is not None:
                        skipped += 1
                        logger.info("Skipping existing course title on CSV row %d: %s", line_number, title)
                        continue

                    cursor.execute(
                        insert_sql,
                        (
                            title,
                            (row.get("description") or "").strip() or None,
                            (row.get("instructor_name") or "").strip() or None,
                            category,
                            active,
                        ),
                    )
                    inserted += 1

        connection.commit()
        logger.info(
            "Imported %d courses from %s; skipped %d existing titles",
            inserted,
            csv_path.name,
            skipped,
        )
        logger.info("Total process time: %.2f seconds", time.perf_counter() - start_time)
        return inserted
    except Exception:
        if connection is not None:
            connection.rollback()
        logger.exception("Course import failed; database changes were rolled back")
        raise
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    try:
        insert_courses_from_csv()
    except Exception:
        sys.exit(1)
