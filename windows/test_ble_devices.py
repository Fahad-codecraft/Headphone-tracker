
import time

from winrt.windows.devices.bluetooth import BluetoothLEDevice
from winrt.windows.devices.enumeration import DeviceInformation


def on_added(sender, info):
    print(f"[BLE FOUND] {info.name}")
    print(f"  Device ID: {info.id}")
    print("-" * 50)


def on_updated(sender, info):
    print(f"[BLE UPDATED] {info.id}")


def on_removed(sender, info):
    print(f"[BLE REMOVED] {info.id}")


selector = BluetoothLEDevice.get_device_selector()

print("Starting Bluetooth LE device discovery...")
print("Waiting for Windows to enumerate BLE devices...")
print("Press CTRL+C to stop.\n")

watcher = DeviceInformation.create_watcher_aqs_filter(
    selector
)

added_token = watcher.add_added(on_added)
updated_token = watcher.add_updated(on_updated)
removed_token = watcher.add_removed(on_removed)

watcher.start()

try:
    while True:
        time.sleep(1)

except KeyboardInterrupt:
    print("\nStopping BLE discovery...")

finally:
    watcher.stop()
    watcher.remove_added(added_token)
    watcher.remove_updated(updated_token)
    watcher.remove_removed(removed_token)

    print("Stopped.")
