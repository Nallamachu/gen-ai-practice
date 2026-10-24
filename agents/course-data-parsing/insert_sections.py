#!/usr/bin/env python3
"""Import sections from video_sections.csv into MySQL."""

import csv
import logging
import os
import sys
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
logger = logging.getLogger("CourseSectionsParser")


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


def insert_sections_from_csv():
    if pymysql is None:
        raise ImportError("Package 'pymysql' is required. Install it with: pip install pymysql")

    csv_path = Path(__file__).resolve().with_name("video_sections.csv")
    if not csv_path.is_file():
        raise FileNotFoundError(f"Sections CSV file was not found: {csv_path}")

    connection = None
    inserted = 0
    insert_sql = """
        INSERT INTO video_sections (course_id, title, order_index)
        VALUES (%s, %s, %s)
    """

    try:
        connection = pymysql.connect(**get_db_config())
        with csv_path.open("r", encoding="utf-8-sig", newline="") as csv_file:
            reader = csv.DictReader(csv_file)
            required_columns = {"course_id", "title", "order_index"}
            if reader.fieldnames is None:
                raise ValueError("The sections CSV is empty or has no header row")
            missing = required_columns - set(reader.fieldnames)
            if missing:
                raise ValueError(f"Sections CSV is missing columns: {', '.join(sorted(missing))}")

            with connection.cursor() as cursor:
                for line_number, row in enumerate(reader, start=2):
                    try:
                        course_id = int(row["course_id"])
                        order_index = int(row["order_index"])
                    except (TypeError, ValueError) as exc:
                        raise ValueError(
                            f"Invalid course_id or order_index on CSV row {line_number}"
                        ) from exc

                    cursor.execute("SELECT 1 FROM video_courses WHERE id = %s LIMIT 1", (course_id,))
                    if cursor.fetchone() is None:
                        raise ValueError(
                            f"Course ID {course_id} does not exist (CSV row {line_number})"
                        )

                    cursor.execute(
                        insert_sql,
                        (course_id, row["title"], order_index),
                    )
                    inserted += 1

        connection.commit()
        logger.info("Successfully inserted %d sections from %s", inserted, csv_path.name)
        return inserted
    except Exception:
        if connection is not None:
            connection.rollback()
        logger.exception("Section import failed; database changes were rolled back")
        raise
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    try:
        insert_sections_from_csv()
    except Exception:
        sys.exit(1)
