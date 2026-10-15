#!/usr/bin/env python3
"""
Hostel & PG MySQL Ingestion Agent
=================================
Automated pipeline to process hostel/PG directory CSV files, download and encrypt
property images, and store records across normalized relational MySQL tables:
  1. address
  2. hostel (FK -> address.id)
  3. amenities
  4. hostel_amenities (FK -> hostel.id, FK -> amenities.id)
  5. image_ids (relate_id -> hostel.id)
  6. images (FK -> hostel.id, FK -> image_ids.id)

Includes transactional rollbacks per record and writes failed records to both
a 'failed_records.csv' and a multi-sheet Excel report ('processed_with_failures.xlsx').
"""

import os
import sys
import csv
import logging
import datetime
import mimetypes
import time
import urllib.request
from urllib.error import HTTPError, URLError
from http.client import HTTPException
from urllib.parse import urlparse
from typing import Dict, Any, List, Optional, Tuple

try:
    import pymysql
    from pymysql.cursors import DictCursor
except ImportError:
    pymysql = None

try:
    import pandas as pd
except ImportError:
    pd = None

try:
    from cryptography.fernet import Fernet
except ImportError:
    Fernet = None

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("HostelDBAgent")

DB_CONFIG = {
    "host": "localhost",
    "port": 3306,
    "user": "root",
    "password": "MySQL$123",
    "database": "multi_owner_hostel",
}
INGESTION_LOCK_NAME = "hostel-csv-parser-ingestion"


class ImageEncryptor:
    """Handles symmetric AES (Fernet) encryption for binary image data."""

    def __init__(self, key: Optional[bytes] = None):
        if Fernet is None:
            raise ImportError("Package 'cryptography' is required. Install via: pip install cryptography")

        if key is None:
            env_key = os.environ.get("IMAGE_ENCRYPTION_KEY")
            if env_key:
                self.key = env_key.encode("utf-8")
            else:
                self.key = Fernet.generate_key()
                logger.info(f"Generated new encryption key: {self.key.decode('utf-8')}")
                logger.info("Store this in your environment as IMAGE_ENCRYPTION_KEY to decrypt in your application.")
        else:
            self.key = key

        self.cipher = Fernet(self.key)

    def encrypt(self, raw_bytes: bytes) -> bytes:
        """Encrypt binary image data."""
        return self.cipher.encrypt(raw_bytes)

    def decrypt(self, encrypted_bytes: bytes) -> bytes:
        """Decrypt binary image data."""
        return self.cipher.decrypt(encrypted_bytes)


class HostelIngestionAgent:
    """Processes CSV records and safely inserts into MySQL database."""

    def __init__(self, db_config: Dict[str, Any], encryptor: ImageEncryptor):
        if pymysql is None:
            raise ImportError("Package 'pymysql' is required. Install via: pip install pymysql")
        self.db_config = db_config
        self.encryptor = encryptor

    def get_connection(self):
        """Establish a MySQL connection."""
        return pymysql.connect(
            host=self.db_config.get("host", "localhost"),
            port=int(self.db_config.get("port", 3306)),
            user=self.db_config.get("user", "root"),
            password=self.db_config.get("password", ""),
            database=self.db_config.get("database", "hostel_db"),
            charset="utf8mb4",
            autocommit=False,
            cursorclass=DictCursor
        )

    def download_image(self, url: str, timeout: int = 10) -> Tuple[bytes, str, str]:
        """
        Download image from URL, returning (raw_bytes, mime_type, filename).
        """
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("Image URL must be a valid HTTP or HTTPS URL")

        path = parsed.path
        filename = os.path.basename(path) or "hostel_image.jpg"
        if not os.path.splitext(filename)[1]:
            filename += ".jpg"

        mime_type, _ = mimetypes.guess_type(filename)
        if not mime_type:
            mime_type = "image/jpeg"

        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        )

        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content_type = resp.headers.get_content_type()
            if not content_type or not content_type.startswith("image/"):
                raise ValueError(
                    f"Image URL returned non-image content type: {content_type or 'unknown'}"
                )
            mime_type = content_type
            raw_bytes = resp.read()
            if not raw_bytes:
                raise ValueError("Image URL returned an empty response")

        return raw_bytes, mime_type, filename

    def get_or_create_amenity_id(self, cursor, amenity_name: str) -> int:
        """Return an existing amenity ID or create a master amenity."""
        cursor.execute(
            """
            SELECT id
            FROM amenities
            WHERE LOWER(name) = LOWER(%s)
            ORDER BY id
            LIMIT 1
            """,
            (amenity_name,),
        )
        amenity = cursor.fetchone()
        if amenity:
            return amenity["id"]

        cursor.execute(
            "INSERT INTO amenities (is_available, name) VALUES (%s, %s)",
            (1, amenity_name),
        )
        return cursor.lastrowid

    @staticmethod
    def normalize_contact(contact: str) -> str:
        """Normalize an Indian phone number to digits without the +91 prefix."""
        digits = "".join(character for character in contact if character.isdigit())
        if len(digits) == 12 and digits.startswith("91"):
            digits = digits[2:]
        return digits

    def insert_record(self, cursor, row: Dict[str, Any]) -> Optional[int]:
        """
        Insert a hostel and its related records unless its contact already exists.
        Returns the new hostel_id, or None when the contact is a duplicate.
        """
        cursor.execute(
            "SELECT GET_LOCK(%s, %s) AS lock_acquired",
            (INGESTION_LOCK_NAME, 30),
        )
        lock_result = cursor.fetchone()
        if not lock_result or lock_result["lock_acquired"] != 1:
            raise TimeoutError("Could not acquire the database ingestion lock within 30 seconds")

        contact = self.normalize_contact(str(row.get("contact") or ""))
        contact_candidates = [contact]
        if len(contact) == 10:
            contact_candidates.extend(
                (
                    f"+91 {contact[:5]} {contact[5:]}",
                    f"{contact[:5]} {contact[5:]}",
                )
            )
        if contact:
            placeholders = ", ".join(["%s"] * len(contact_candidates))
            cursor.execute(
                f"SELECT id FROM hostel WHERE contact IN ({placeholders}) LIMIT 1",
                tuple(contact_candidates),
            )
            if cursor.fetchone():
                logger.info(
                    "Skipping hostel '%s': contact %s already exists",
                    row.get("name", "Unknown"),
                    contact,
                )
                return None

        now = datetime.datetime.now()
        image_url = (row.get("image_url") or "").strip()
        image_data = None
        if image_url:
            try:
                raw_bytes, mime_type, filename = self.download_image(image_url)
            except (HTTPError, URLError, HTTPException, TimeoutError, OSError, ValueError) as err:
                logger.warning(
                    "Skipping image for hostel '%s' because its URL could not be downloaded: %s",
                    row.get("name", "Unknown"),
                    err,
                )
            else:
                image_data = (mime_type, filename, self.encryptor.encrypt(raw_bytes))

        # 1. Insert Address
        address_sql = """
            INSERT INTO address (
                city, country, state, street, zipcode, version, area, house_no, town
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
        """
        address_values = (
            str(row.get("city", ""))[:255] or None,
            str(row.get("country", "India"))[:255] or None,
            str(row.get("state", ""))[:255] or None,
            str(row.get("street", ""))[:255] or None,
            str(row.get("pincode", row.get("zipcode", "")))[:255] or None,
            0,  # version
            str(row.get("area", ""))[:255] or None,
            str(row.get("house_no", ""))[:255] or None,
            str(row.get("town", row.get("city", "")))[:255] or None,
        )
        cursor.execute(address_sql, address_values)
        address_id = cursor.lastrowid
        print(f"Inserted Address ID: {address_id}")

        # 2. Insert Hostel
        p_starts = float(row.get("price_starts", 0.0) or 0.0)
        p_ends = float(row.get("price_ends", 0.0) or 0.0)

        hostel_sql = """
            INSERT INTO hostel (
                is_active, is_for_sale, address_id, group_id, contact, name,
                type, purpose, rooms, created_by,
                created_date, price_ends, price_starts, location
            ) VALUES (
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s,
                %s, %s
            )
        """
        hostel_values = (
            1,  # is_active: True
            0,  # is_for_sale: False
            address_id,     # address_id: FK to address
            10,  # group_id: 10
            contact[:255] or None,
            str(row.get("name", ""))[:255] or None,
            str(row.get("type", ""))[:255] or None,
            "COMMERCIAL",  # purpose: COMMERCIAL
            0,  # rooms: 0
            "1",  # created_by: 1
            now,    # created_date: current timestamp
            p_ends, # price_ends,
            p_starts, # price_starts,
            str(row.get("location_2") or row.get("location", ""))[:255] or None,
        )
        cursor.execute(hostel_sql, hostel_values)
        hostel_id = cursor.lastrowid
        print(f"Inserted Hostel ID: {hostel_id}")

        # 3. Insert or reuse amenities and link them to the hostel.
        seen_amenities = set()
        for amenity_name in (row.get("amenities") or "").split(","):
            amenity_name = amenity_name.strip()[:255]
            normalized_name = amenity_name.casefold()
            if not amenity_name or normalized_name in seen_amenities:
                continue
            seen_amenities.add(normalized_name)

            amenity_id = self.get_or_create_amenity_id(cursor, amenity_name)
            cursor.execute(
                """
                INSERT INTO hostel_amenities (amenity_id, hostel_id)
                VALUES (%s, %s)
                """,
                (amenity_id, hostel_id),
            )

        # 4. Handle Image Download & Encryption
        if image_data:
            mime_type, filename, encrypted_data = image_data
            cursor.execute("SELECT COALESCE(MAX(id), 0) + 1 AS next_id FROM image_ids")
            image_id_val = cursor.fetchone()["next_id"]
            # Insert Image_IDs
            image_ids_sql = """
                INSERT INTO image_ids (
                    id, relate_id, relate_to, image_name
                ) VALUES (
                    %s, %s, %s, %s
                )
            """
            cursor.execute(
                image_ids_sql,
                (image_id_val, hostel_id, "HOSTEL_IMAGE", filename[:255]),
            )

            # Insert Images table
            images_sql = """
                INSERT INTO images (
                    image_id_id, filename, type, url, data, hostel_id,
                    uploaded_date, image_name, related_id, relate_to, name
                ) VALUES (
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s
                )
            """
            images_values = (
                image_id_val,
                filename[:255],
                mime_type[:255],
                image_url[:255],
                encrypted_data,
                hostel_id,
                now,
                filename[:255],
                hostel_id,
                "HOSTEL_IMAGE",
                str(row.get("name", ""))[:255]
            )
            cursor.execute(images_sql, images_values)

        return hostel_id

    def process_file(self, csv_path: str, failed_output_path: Optional[str] = None) -> Dict[str, Any]:
        """
        Reads CSV file, processes records row-by-row with transactional isolation,
        and saves any failed records to a failure report.
        """
        if not os.path.exists(csv_path):
            raise FileNotFoundError(f"Input file not found at: {csv_path}")

        records = []
        with open(csv_path, mode="r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            source_fieldnames = next(reader, [])
            fieldnames = []
            seen_fieldnames = {}
            for name in source_fieldnames:
                seen_fieldnames[name] = seen_fieldnames.get(name, 0) + 1
                fieldnames.append(
                    name if seen_fieldnames[name] == 1
                    else f"{name}_{seen_fieldnames[name]}"
                )

            for line_number, values in enumerate(reader, start=2):
                if len(values) != len(fieldnames):
                    raise ValueError(
                        f"CSV row {line_number} has {len(values)} columns; "
                        f"expected {len(fieldnames)}"
                    )
                records.append(dict(zip(fieldnames, values)))

        total_count = len(records)
        success_count = 0
        skipped_count = 0
        failed_records: List[Dict[str, Any]] = []

        logger.info(f"Loaded {total_count} records from {csv_path}. Initiating database ingestion...")

        conn = self.get_connection()
        try:
            for idx, row in enumerate(records, start=1):
                try:
                    with conn.cursor() as cursor:
                        hostel_id = self.insert_record(cursor, row)
                    conn.commit()
                    if hostel_id is None:
                        skipped_count += 1
                    else:
                        success_count += 1
                    if idx % 50 == 0 or idx == total_count:
                        logger.info(
                            "Progress: [%s/%s] processed (%s inserted, %s skipped, %s failed).",
                            idx,
                            total_count,
                            success_count,
                            skipped_count,
                            len(failed_records),
                        )
                except Exception as err:
                    conn.rollback()
                    logger.warning(f"Row {idx} failed ('{row.get('name', 'Unknown')}'): {err}")
                    failed_row = dict(row)
                    failed_row["failure_reason"] = str(err)
                    failed_row["failed_at"] = datetime.datetime.now().isoformat()
                    failed_records.append(failed_row)
                finally:
                    with conn.cursor() as cursor:
                        cursor.execute(
                            "SELECT RELEASE_LOCK(%s)",
                            (INGESTION_LOCK_NAME,),
                        )
        finally:
            conn.close()

        # Handle Failed Records Output
        if failed_records:
            base_dir = os.path.dirname(csv_path) or "."
            stem = os.path.splitext(os.path.basename(csv_path))[0]

            # 1. Output CSV failed records
            csv_fail_path = failed_output_path or os.path.join(base_dir, f"{stem}_failed_records.csv")
            fieldnames = list(records[0].keys()) + ["failure_reason", "failed_at"]
            with open(csv_fail_path, mode="w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(failed_records)
            logger.info(f"Exported {len(failed_records)} failed records to CSV: {csv_fail_path}")

            # 2. Output multi-sheet Excel file if pandas/openpyxl is available
            excel_path = os.path.join(base_dir, f"{stem}_processed_with_failures.xlsx")
            if pd is not None:
                try:
                    with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
                        pd.DataFrame(records).to_excel(writer, sheet_name="Original_Data", index=False)
                        pd.DataFrame(failed_records).to_excel(writer, sheet_name="Failed_Records", index=False)
                    logger.info(f"Exported multi-sheet report with Failed_Records to: {excel_path}")
                except Exception as ex:
                    logger.warning(f"Could not generate Excel report: {ex}")

        summary = {
            "total_records": total_count,
            "success_count": success_count,
            "skipped_count": skipped_count,
            "failed_count": len(failed_records),
            "status": "COMPLETED" if len(failed_records) == 0 else "PARTIAL_SUCCESS"
        }
        logger.info(f"Ingestion finished: {summary}")
        return summary


def main():
    start_time = time.perf_counter()
    script_dir = os.path.dirname(os.path.abspath(__file__))
    csv_path = os.path.join(script_dir, "hostels-india-data.csv")

    try:
        encryptor = ImageEncryptor()
        agent = HostelIngestionAgent(DB_CONFIG, encryptor)
        agent.process_file(csv_path)
    finally:
        elapsed_time = time.perf_counter() - start_time
        logger.info("Total process time: %.2f seconds", elapsed_time)


if __name__ == "__main__":
    main()