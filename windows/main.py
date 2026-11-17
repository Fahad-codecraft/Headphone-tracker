
import asyncio
from datetime import datetime

from winrt.windows.devices.bluetooth import (
    BluetoothDevice,
    BluetoothConnectionStatus
)

from winrt.windows.devices.enumeration import (
    DeviceInformation
)

from database.local_db import LocalDatabase


class BluetoothMonitor:

    def __init__(self):

        self.db = LocalDatabase()

        self.loop = None
        self.queue = None

        self.watcher = None

        # Keep device objects and event tokens alive.
        self.devices = {}

        self.watcher_tokens = []

    def timestamp(self):

        return datetime.now().astimezone().strftime(
            "%Y-%m-%d %H:%M:%S"
        )

    def print_event(self, name, state):

        print(
            f"[{self.timestamp()}] "
            f"{state:12} | {name}",
            flush=True
        )

    def queue_event(self, event):

        self.loop.call_soon_threadsafe(
            self.queue.put_nowait,
            event
        )

    # ---------------------------------
    # Device watcher event callbacks
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
            ""
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

    # ---------------------------------
    # Attach to a Bluetooth device
    # ---------------------------------

    async def attach_device(self, os_device_id, name):

        if os_device_id in self.devices:
            return

        try:

            device = await BluetoothDevice.from_id_async(
                os_device_id
            )

            if device is None:
                return

            device_name = device.name or name or "Unknown device"

            device_key = self.db.make_device_key(
                os_device_id
            )

            # Subscribe to connection changes.
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

            self.devices[os_device_id] = {
                "device": device,
                "token": token,
                "name": device_name,
                "key": device_key
            }

            # Read current state once.
            connected = (
                device.connection_status
                == BluetoothConnectionStatus.CONNECTED
            )

            self.db.record_status(
                device_key,
                device_name,
                connected,
                initial_snapshot=True
            )

            self.print_event(
                device_name,
                "CONNECTED" if connected else "DISCONNECTED"
            )

        except Exception as error:

            print(
                f"Could not monitor {name}: {error}",
                flush=True
            )

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

            print("Event cleanup warning:", error)

        print(
            f"Device removed from watcher: {entry['name']}"
        )

    # ---------------------------------
    # Start monitoring
    # ---------------------------------

    async def run(self):

        self.loop = asyncio.get_running_loop()
        self.queue = asyncio.Queue()

        selector = BluetoothDevice.get_device_selector()

        self.watcher = DeviceInformation.create_watcher_aqs_filter(
        selector
        )

        # Register Windows device watcher events.
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
        print("Press CTRL+C to stop.")
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

                    entry = self.devices.get(device_id)

                    if entry is None:
                        continue

                    self.db.record_status(
                        entry["key"],
                        name,
                        connected
                    )

                    self.print_event(
                        name,
                        "CONNECTED" if connected
                        else "DISCONNECTED"
                    )

                elif event_type == "REMOVE":

                    _, device_id, _ = event

                    self.remove_device(device_id)

                elif event_type == "ENUMERATION_COMPLETED":

                    print(
                        "Initial Bluetooth device enumeration completed."
                    )

        finally:

            print("\nStopping Bluetooth monitor...")

            self.watcher.stop()

            # Unsubscribe all Bluetooth device events.
            for device_id in list(self.devices.keys()):
                self.remove_device(device_id)

            # Unsubscribe watcher events.
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
