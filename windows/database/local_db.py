
import sqlite3
import uuid
import hashlib
import os

from pathlib import Path
from datetime import datetime, timezone, timedelta


APP_DIR = (
    Path(os.environ.get("LOCALAPPDATA", Path.home()))
    / "HeadphoneTracker"
)

APP_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = APP_DIR / "tracker.db"


def utc_now():
    return datetime.now(timezone.utc)


def utc_string(value=None):
    value = value or utc_now()
    return value.isoformat().replace("+00:00", "Z")


def parse_utc(value):
    return datetime.fromisoformat(
        value.replace("Z", "+00:00")
    )


class LocalDatabase:

    def __init__(self):

        self.connection = sqlite3.connect(
            DB_PATH,
            check_same_thread=False
        )

        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.execute("PRAGMA foreign_keys=ON")

        self.create_tables()
        self.recover_existing_devices()

    # ---------------------------------
    # Database creation
    # ---------------------------------

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

        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS connection_sessions (
                session_id TEXT PRIMARY KEY,
                device_key TEXT NOT NULL,
                device_name TEXT NOT NULL,
                start_utc TEXT,
                observed_start_utc TEXT NOT NULL,
                end_utc TEXT,
                duration_seconds INTEGER,
                start_confidence TEXT NOT NULL,
                end_confidence TEXT
            )
        """)

        self.connection.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_sessions_device
            ON connection_sessions(device_key)
        """)

        self.connection.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_sessions_start
            ON connection_sessions(start_utc)
        """)

        self.connection.commit()

    # ---------------------------------
    # Recovery of existing database
    # ---------------------------------

    def recover_existing_devices(self):

        now = utc_string()

        cursor = self.connection.cursor()

        cursor.execute("""
            SELECT device_key, device_name
            FROM devices
            WHERE is_connected = 1
        """)

        for device_key, device_name in cursor.fetchall():

            cursor.execute("""
                SELECT 1
                FROM connection_sessions
                WHERE device_key = ?
                  AND end_utc IS NULL
                LIMIT 1
            """, (device_key,))

            if cursor.fetchone() is None:

                # We know the device was connected previously,
                # but its exact original start time is unknown.
                cursor.execute("""
                    INSERT INTO connection_sessions (
                        session_id,
                        device_key,
                        device_name,
                        start_utc,
                        observed_start_utc,
                        end_utc,
                        duration_seconds,
                        start_confidence,
                        end_confidence
                    )
                    VALUES (?, ?, ?, NULL, ?, NULL, NULL,
                            'unknown', NULL)
                """, (
                    str(uuid.uuid4()),
                    device_key,
                    device_name,
                    now
                ))

        self.connection.commit()

    # ---------------------------------
    # Device identification
    # ---------------------------------

    @staticmethod
    def make_device_key(os_device_id):

        return hashlib.sha256(
            os_device_id.encode("utf-8")
        ).hexdigest()

    # ---------------------------------
    # Event recording
    # ---------------------------------

    def insert_event(
        self,
        device_key,
        device_name,
        event_type,
        confidence
    ):

        self.connection.execute("""
            INSERT INTO connection_events (
                event_id,
                device_key,
                device_name,
                event_type,
                event_time_utc,
                confidence
            )
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            str(uuid.uuid4()),
            device_key,
            device_name,
            event_type,
            utc_string(),
            confidence
        ))

    # ---------------------------------
    # Session creation
    # ---------------------------------

    def start_session(
        self,
        device_key,
        device_name,
        known_start=True
    ):

        now = utc_string()

        self.connection.execute("""
            INSERT INTO connection_sessions (
                session_id,
                device_key,
                device_name,
                start_utc,
                observed_start_utc,
                end_utc,
                duration_seconds,
                start_confidence,
                end_confidence
            )
            VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, NULL)
        """, (
            str(uuid.uuid4()),
            device_key,
            device_name,
            now if known_start else None,
            now,
            "confirmed" if known_start else "unknown"
        ))

    # ---------------------------------
    # Session closing
    # ---------------------------------

    def close_session(
        self,
        device_key,
        recovered=False
    ):

        cursor = self.connection.cursor()

        cursor.execute("""
            SELECT session_id, start_utc
            FROM connection_sessions
            WHERE device_key = ?
              AND end_utc IS NULL
            ORDER BY observed_start_utc DESC
            LIMIT 1
        """, (device_key,))

        session = cursor.fetchone()

        if session is None:
            return

        session_id, start_time = session

        now = utc_now()

        if start_time is not None and not recovered:

            start = parse_utc(start_time)

            duration = max(
                0,
                int((now - start).total_seconds())
            )

        else:

            # The exact session duration cannot be established.
            duration = None

        self.connection.execute("""
            UPDATE connection_sessions
            SET end_utc = ?,
                duration_seconds = ?,
                end_confidence = ?
            WHERE session_id = ?
        """, (
            utc_string(now),
            duration,
            "recovered_on_startup" if recovered else "confirmed",
            session_id
        ))

    # ---------------------------------
    # Main status update
    # ---------------------------------

    def record_status(
        self,
        device_key,
        device_name,
        connected,
        initial_snapshot=False
    ):

        now = utc_string()

        new_state = 1 if connected else 0

        cursor = self.connection.cursor()

        cursor.execute("""
            SELECT is_connected
            FROM devices
            WHERE device_key = ?
        """, (device_key,))

        previous = cursor.fetchone()

        # New device
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

            if connected:

                self.insert_event(
                    device_key,
                    device_name,
                    "CONNECTED",
                    "observed_on_startup"
                )

                self.start_session(
                    device_key,
                    device_name,
                    known_start=False
                )

            self.connection.commit()
            return

        previous_state = previous[0]

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

        # Device was already connected.
        if previous_state == new_state:

            # Make sure an open session exists.
            if connected:

                cursor.execute("""
                    SELECT 1
                    FROM connection_sessions
                    WHERE device_key = ?
                      AND end_utc IS NULL
                    LIMIT 1
                """, (device_key,))

                if cursor.fetchone() is None:

                    self.start_session(
                        device_key,
                        device_name,
                        known_start=False
                    )

            self.connection.commit()
            return

        # State has changed.
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

        if connected:

            self.start_session(
                device_key,
                device_name,
                known_start=not initial_snapshot
            )

        else:

            self.close_session(
                device_key,
                recovered=initial_snapshot
            )

        cursor.execute("""
            UPDATE devices
            SET is_connected = ?
            WHERE device_key = ?
        """, (
            new_state,
            device_key
        ))

        self.connection.commit()

    # ---------------------------------
    # Daily usage calculation
    # ---------------------------------

    def get_daily_usage(self, day=None):

        if day is None:
            day = datetime.now().astimezone().date()

        local_start = datetime.combine(
            day,
            datetime.min.time()
        ).astimezone()

        local_end = datetime.combine(
            day + timedelta(days=1),
            datetime.min.time()
        ).astimezone()

        day_start = local_start.astimezone(timezone.utc)
        day_end = local_end.astimezone(timezone.utc)

        now = utc_now()

        cursor = self.connection.cursor()

        cursor.execute("""
            SELECT device_key, device_name,
                   start_utc, end_utc
            FROM connection_sessions
            WHERE start_utc IS NOT NULL
              AND start_utc < ?
              AND (end_utc IS NULL OR end_utc > ?)
        """, (
            utc_string(day_end),
            utc_string(day_start)
        ))

        total_seconds = 0
        per_device = {}

        for device_key, name, start_value, end_value in cursor.fetchall():

            start = parse_utc(start_value)

            end = (
                parse_utc(end_value)
                if end_value
                else now
            )

            # Clip session to this local calendar day.
            clipped_start = max(start, day_start)
            clipped_end = min(end, day_end, now)

            seconds = max(
                0,
                int((clipped_end - clipped_start).total_seconds())
            )

            total_seconds += seconds

            per_device[name] = (
                per_device.get(name, 0) + seconds
            )

        return {
            "date": str(day),
            "total_seconds": total_seconds,
            "devices": per_device
        }

    # ---------------------------------
    # Daily usage history
    # ---------------------------------

    def get_usage_history(self, days=7):
        """Return daily summaries for today and the previous days."""
        if days < 1:
            raise ValueError("days must be at least 1")

        today = datetime.now().astimezone().date()
        return [
            self.get_daily_usage(today - timedelta(days=offset))
            for offset in range(days)
        ]

    # ---------------------------------
    # Recent session history
    # ---------------------------------

    def get_recent_sessions(self, limit=20):

        cursor = self.connection.cursor()

        cursor.execute("""
            SELECT device_name,
                   start_utc,
                   observed_start_utc,
                   end_utc,
                   duration_seconds,
                   start_confidence,
                   end_confidence
            FROM connection_sessions
            ORDER BY observed_start_utc DESC
            LIMIT ?
        """, (limit,))

        return cursor.fetchall()

    # ---------------------------------
    # Cleanup
    # ---------------------------------

    def close(self):
        self.connection.close()
