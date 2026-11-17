
import asyncio
import time

from datetime import datetime

from winrt.windows.devices.bluetooth import (
    BluetoothDevice,
    BluetoothConnectionStatus
)

from winrt.windows.devices.enumeration import (
    DeviceInformation
)

from database.local_db import LocalDatabase
from config import TRACKED_DEVICE_NAMES


class BluetoothMonitor:

    def __init__(self):

        self.db = LocalDatabase()

        self.loop = None
        self.queue = None
        self.watcher = None

        # Active device subscriptions
        self.devices = {}

        # Watcher event tokens
        self.watcher_tokens = []

    # ---------------------------------
    # Device filtering
    # ---------------------------------

    @staticmethod
    def is_tracked_device(name):

        if not name:
            return False

        normalized_name = name.casefold().strip()

        return any(
            keyword.casefold() in normalized_name
            for keyword in TRACKED_DEVICE_NAMES
            if keyword.strip()
        )

    # ---------------------------------
    # Time utilities
    # ---------------------------------

    @staticmethod
    def timestamp():

        return datetime.now().astimezone().strftime(
            "%Y-%m-%d %H:%M:%S"
        )

    @staticmethod
    def format_duration(seconds):

        total_seconds = max(0, int(seconds))

        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        seconds = total_seconds % 60

        return (
            f"{hours:02d}:"
            f"{minutes:02d}:"
            f"{seconds:02d}"
        )

    def print_event(self, name, state):

        print(
            f"[{self.timestamp()}] "
            f"{state:14} | {name}",
            flush=True
        )

    # ---------------------------------
    # Thread-safe event queue
    # ---------------------------------

    def queue_event(self, event):

        self.loop.call_soon_threadsafe(
            self.queue.put_nowait,
            event
        )

    # ---------------------------------
    # Windows watcher callbacks
    # ---------------------------------

    def on_device_added(self, sender, info):

        self.queue_event((
            "ADD",
            info.id,
            info.name
        ))

    def on_device_updated(self, sender, info):

        self.queue_event((
            "UPDATE",
            info.id,
            info.name
        ))

    def on_device_removed(self, sender, info):

        self.queue_event((
            "REMOVE",
            info.id,
            ""
        ))

    def on_enumeration_completed(self, sender, args):

        self.queue_event((
            "ENUMERATION_COMPLETED",
            "",
            ""
        ))

    
    
    def show_daily_summary(self):

        history = self.db.get_usage_history(days=7)

        def format_usage(total_seconds):
            total_seconds = max(0, int(total_seconds))
            hours = total_seconds // 3600
            minutes = (total_seconds % 3600) // 60
            seconds = total_seconds % 60
            return f"{hours}h {minutes}m {seconds}s"

        today = history[0]
        yesterday = history[1] if len(history) > 1 else {
            "total_seconds": 0,
            "devices": {},
        }

        seven_day_total = sum(
            day["total_seconds"] for day in history
        )

        seven_day_devices = {}
        for day in history:
            for name, seconds in day["devices"].items():
                seven_day_devices[name] = (
                    seven_day_devices.get(name, 0) + seconds
                )

        print("\\nUSAGE HISTORY")
        print("-" * 55)
        print(f"Today:     {format_usage(today['total_seconds'])}")
        print(f"Yesterday: {format_usage(yesterday['total_seconds'])}")
        print(
            "Last 7 days: "
            f"{format_usage(seven_day_total)} "
            "(including today)"
        )

        print("\\nDAILY BREAKDOWN")
        for day in history:
            day_label = datetime.fromisoformat(
                day["date"]
            ).strftime("%a, %d %b")
            print(
                f"{day_label}: "
                f"{format_usage(day['total_seconds'])}"
            )

        if seven_day_devices:
            print("\\nPER-HEADPHONE TOTAL (LAST 7 DAYS)")
            for name, seconds in seven_day_devices.items():
                print(f"{name}: {format_usage(seconds)}")

        print("-" * 55)


    # ---------------------------------
    # Attach to a headphone
    # ---------------------------------

    async def attach_device(self, os_device_id, name):

        if os_device_id in self.devices:
            return

        if not self.is_tracked_device(name):
            return

        try:

            device = await BluetoothDevice.from_id_async(
                os_device_id
            )

            if device is None:
                return

            device_name = device.name or name

            # Verify using the actual device name too.
            if not self.is_tracked_device(device_name):
                return

            device_key = self.db.make_device_key(
                os_device_id
            )

            # Connection state callback
            def on_connection_changed(sender, args):

                connected = (
                    sender.connection_status
                    == BluetoothConnectionStatus.CONNECTED
                )

                self.queue_event((
                    "STATUS",
                    os_device_id,
                    device_name,
                    connected
                ))

            token = device.add_connection_status_changed(
                on_connection_changed
            )

            # Read the initial connection state.
            connected = (
                device.connection_status
                == BluetoothConnectionStatus.CONNECTED
            )

            self.devices[os_device_id] = {
                "device": device,
                "token": token,
                "name": device_name,
                "key": device_key,
                "connected": connected,

                # None means the session began before
                # this monitor started.
                "session_started": (
                    None if connected else None
                )
            }

            self.db.record_status(
                device_key,
                device_name,
                connected,
                initial_snapshot=True
            )

            self.print_event(
                device_name,
                "CONNECTED" if connected
                else "DISCONNECTED"
            )

            if connected:
                print(
                    "  Session start unknown: "
                    "device was already connected at startup."
                )

        except Exception as error:

            print(
                f"Could not monitor {name}: {error}",
                flush=True
            )

    # ---------------------------------
    # Connection state changes
    # ---------------------------------

    def process_status(
        self,
        os_device_id,
        name,
        connected
    ):

        entry = self.devices.get(os_device_id)

        if entry is None:
            return

        previous_state = entry["connected"]

        # Ignore duplicate notifications.
        if previous_state == connected:
            return

        entry["connected"] = connected

        self.db.record_status(
            entry["key"],
            name,
            connected
        )

        if connected:

            # Start a new session timer.
            entry["session_started"] = time.monotonic()

            self.print_event(name, "CONNECTED")

        else:

            start = entry["session_started"]

            self.print_event(name, "DISCONNECTED")

            if start is not None:

                duration = time.monotonic() - start

                print(
                    "  Session duration: "
                    + self.format_duration(duration)
                )

            else:

                print(
                    "  Session duration unavailable: "
                    "connection began before monitoring."
                )

            entry["session_started"] = None

    # ---------------------------------
    # Remove device subscription
    # ---------------------------------

    def remove_device(self, os_device_id):

        entry = self.devices.pop(os_device_id, None)

        if entry is None:
            return

        try:

            entry["device"].remove_connection_status_changed(
                entry["token"]
            )

        except Exception as error:

            print("Cleanup warning:", error)

    # ---------------------------------
    # Display current status
    # ---------------------------------

    def show_active_sessions(self):

        print("\nACTIVE HEADPHONE SESSIONS")
        print("-" * 55)

        active_count = 0

        for entry in self.devices.values():

            if not entry["connected"]:
                continue

            active_count += 1

            start = entry["session_started"]

            print(f"Device: {entry['name']}")

            if start is not None:

                duration = time.monotonic() - start

                print(
                    "Duration: "
                    + self.format_duration(duration)
                )

            else:

                print(
                    "Duration: Unknown "
                    "(connected before monitor startup)"
                )

        if active_count == 0:
            print("No tracked headphones currently connected.")

        print("-" * 55)

    # ---------------------------------
    # Main monitoring loop
    # ---------------------------------

    async def run(self):

        self.loop = asyncio.get_running_loop()
        self.queue = asyncio.Queue()

        selector = BluetoothDevice.get_device_selector()

        self.watcher = DeviceInformation.create_watcher_aqs_filter(
            selector
        )

        self.watcher_tokens = [
            self.watcher.add_added(self.on_device_added),
            self.watcher.add_updated(self.on_device_updated),
            self.watcher.add_removed(self.on_device_removed),
            self.watcher.add_enumeration_completed(
                self.on_enumeration_completed
            )
        ]

        print("=" * 55)
        print("       HEADPHONE TRACKER - WINDOWS")
        print("=" * 55)

        print("Starting Bluetooth monitoring...")
        print("Tracked devices:")

        for name in TRACKED_DEVICE_NAMES:
            print(f"  - {name}")

        print("\nPress CTRL+C to stop.")
        print("=" * 55)

        self.watcher.start()

        try:

            while True:

                event = await self.queue.get()

                event_type = event[0]

                if event_type == "ADD":

                    _, device_id, name = event

                    await self.attach_device(
                        device_id,
                        name
                    )

                elif event_type == "UPDATE":

                    _, device_id, name = event

                    if device_id not in self.devices:

                        await self.attach_device(
                            device_id,
                            name
                        )

                elif event_type == "STATUS":

                    _, device_id, name, connected = event

                    self.process_status(
                        device_id,
                        name,
                        connected
                    )

                elif event_type == "REMOVE":

                    _, device_id, _ = event

                    self.remove_device(device_id)

                elif event_type == "ENUMERATION_COMPLETED":

                    print(
                        "\nInitial Bluetooth enumeration completed."
                    )

                if event_type in ("STATUS", "ENUMERATION_COMPLETED"):
                    self.show_active_sessions()
                    self.show_daily_summary()

        finally:

            print("\nStopping Bluetooth monitor...")

            self.show_active_sessions()

            self.watcher.stop()

            for device_id in list(self.devices.keys()):
                self.remove_device(device_id)

            for token, remove_method in zip(
                self.watcher_tokens,
                [
                    self.watcher.remove_added,
                    self.watcher.remove_updated,
                    self.watcher.remove_removed,
                    self.watcher.remove_enumeration_completed
                ]
            ):

                remove_method(token)

            self.db.close()

            print("Monitor stopped.")


if __name__ == "__main__":

    monitor = BluetoothMonitor()

    try:

        asyncio.run(monitor.run())

    except KeyboardInterrupt:

        print("\nApplication closed by user.")
