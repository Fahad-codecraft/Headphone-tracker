
import sqlite3
import uuid
import hashlib
import os

from pathlib import Path
from datetime import datetime, timezone


APP_DIR = (
    Path(os.environ.get("LOCALAPPDATA", Path.home()))
    / "HeadphoneTracker"
)

APP_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = APP_DIR / "tracker.db"


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


class LocalDatabase:

    def __init__(self):
        self.connection = sqlite3.connect(
            DB_PATH,
            check_same_thread=False
        )

        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")

        self.create_tables()

    def create_tables(self):

        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS devices (
                device_key TEXT PRIMARY KEY,
                device_name TEXT NOT NULL,
                is_connected INTEGER NOT NULL,
                last_seen_utc TEXT NOT NULL
            )
        """)

        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS connection_events (
                event_id TEXT PRIMARY KEY,
                device_key TEXT NOT NULL,
                device_name TEXT NOT NULL,
                event_type TEXT NOT NULL,
                event_time_utc TEXT NOT NULL,
                confidence TEXT NOT NULL
            )
        """)

        self.connection.commit()

    @staticmethod
    def make_device_key(os_device_id):

        return hashlib.sha256(
            os_device_id.encode("utf-8")
        ).hexdigest()

    def record_status(
        self,
        device_key,
        device_name,
        connected,
        initial_snapshot=False
    ):

        now = utc_now()

        cursor = self.connection.cursor()

        cursor.execute("""
            SELECT is_connected
            FROM devices
            WHERE device_key = ?
        """, (device_key,))

        previous = cursor.fetchone()

        new_state = 1 if connected else 0

        # First observation of this device
        if previous is None:

            cursor.execute("""
                INSERT INTO devices
                VALUES (?, ?, ?, ?)
            """, (
                device_key,
                device_name,
                new_state,
                now
            ))

            # Do not invent a connection start time.
            # Mark an already-connected device as observed.
            if connected:

                self.insert_event(
                    device_key,
                    device_name,
                    "CONNECTED",
                    "observed_on_startup"
                )

            self.connection.commit()
            return

        previous_state = previous[0]

        # Update name and last-seen time
        cursor.execute("""
            UPDATE devices
            SET device_name = ?,
                last_seen_utc = ?
            WHERE device_key = ?
        """, (
            device_name,
            now,
            device_key
        ))

        # Ignore duplicate state notifications
        if previous_state == new_state:
            self.connection.commit()
            return

        cursor.execute("""
            UPDATE devices
            SET is_connected = ?
            WHERE device_key = ?
        """, (
            new_state,
            device_key
        ))

        event_type = (
            "CONNECTED" if connected
            else "DISCONNECTED"
        )

        confidence = (
            "observed_on_startup"
            if initial_snapshot
            else "confirmed"
        )

        self.insert_event(
            device_key,
            device_name,
            event_type,
            confidence
        )

        self.connection.commit()

    def insert_event(
        self,
        device_key,
        device_name,
        event_type,
        confidence
    ):

        self.connection.execute("""
            INSERT INTO connection_events
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            str(uuid.uuid4()),
            device_key,
            device_name,
            event_type,
            utc_now(),
            confidence
        ))

    def close(self):
        self.connection.close()
