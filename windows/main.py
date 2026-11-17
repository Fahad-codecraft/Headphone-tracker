import asyncio
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime

from winrt.windows.devices.bluetooth import (
    BluetoothDevice,
    BluetoothConnectionStatus
)
from winrt.windows.devices.enumeration import DeviceInformation

from database.local_db import LocalDatabase
from config import TRACKED_DEVICE_NAMES


APP_TITLE = "Headphone Tracker"


def format_duration(seconds):
    if seconds is None:
        return "Unknown"
    total = max(0, int(seconds))
    hours = total // 3600
    minutes = (total % 3600) // 60
    seconds = total % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def local_time(value):
    if not value:
        return "—"
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone().strftime("%d %b %Y, %I:%M:%S %p")


class BluetoothMonitor:
    """Runs the existing event-driven Windows Bluetooth monitor in a worker thread."""

    def __init__(self, ui_queue):
        self.db = LocalDatabase()
        self.ui_queue = ui_queue
        self.loop = None
        self.events = None
        self.watcher = None
        self.watcher_tokens = []
        self.devices = {}
        self.stopping = False

    @staticmethod
    def is_tracked_device(name):
        if not name:
            return False
        normalized = name.casefold().strip()
        return any(
            keyword.casefold() in normalized
            for keyword in TRACKED_DEVICE_NAMES
            if keyword.strip()
        )

    def post_to_ui(self, message):
        self.ui_queue.put(message)

    def queue_event(self, event):
        if self.loop and self.events:
            self.loop.call_soon_threadsafe(self.events.put_nowait, event)

    def request_snapshot(self):
        self.queue_event(("SNAPSHOT",))

    def stop(self):
        self.queue_event(("STOP",))

    # Windows Bluetooth watcher callbacks
    def on_device_added(self, sender, info):
        self.queue_event(("ADD", info.id, info.name))

    def on_device_updated(self, sender, info):
        self.queue_event(("UPDATE", info.id, info.name))

    def on_device_removed(self, sender, info):
        self.queue_event(("REMOVE", info.id, ""))

    def on_enumeration_completed(self, sender, args):
        self.queue_event(("ENUMERATION_COMPLETED",))

    async def attach_device(self, os_device_id, name):
        if os_device_id in self.devices or not self.is_tracked_device(name):
            return

        try:
            device = await BluetoothDevice.from_id_async(os_device_id)
            if device is None:
                return

            device_name = device.name or name
            if not self.is_tracked_device(device_name):
                return

            device_key = self.db.make_device_key(os_device_id)

            def on_connection_changed(sender, args):
                connected = (
                    sender.connection_status
                    == BluetoothConnectionStatus.CONNECTED
                )
                self.queue_event((
                    "STATUS", os_device_id, device_name, connected
                ))

            token = device.add_connection_status_changed(on_connection_changed)
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
                "session_started": None
            }

            self.db.record_status(
                device_key, device_name, connected, initial_snapshot=True
            )
            self.post_to_ui(("LOG", f"Monitoring {device_name}"))

        except Exception as error:
            self.post_to_ui(("ERROR", f"Could not monitor {name}: {error}"))

    def process_status(self, os_device_id, name, connected):
        entry = self.devices.get(os_device_id)
        if entry is None or entry["connected"] == connected:
            return

        entry["connected"] = connected
        self.db.record_status(entry["key"], name, connected)

        if connected:
            entry["session_started"] = time.monotonic()
            self.post_to_ui(("LOG", f"{name} connected"))
        else:
            entry["session_started"] = None
            self.post_to_ui(("LOG", f"{name} disconnected"))

    def remove_device(self, os_device_id):
        entry = self.devices.pop(os_device_id, None)
        if entry is None:
            return
        try:
            entry["device"].remove_connection_status_changed(entry["token"])
        except Exception as error:
            self.post_to_ui(("LOG", f"Device cleanup warning: {error}"))

    def build_snapshot(self):
        active = []
        for entry in self.devices.values():
            if not entry["connected"]:
                continue
            started = entry["session_started"]
            elapsed = time.monotonic() - started if started is not None else None
            active.append({
                "name": entry["name"],
                "duration": elapsed
            })

        history = self.db.get_usage_history(days=7)
        sessions = self.db.get_recent_sessions(limit=25)
        self.post_to_ui(("SNAPSHOT", {
            "active": active,
            "history": history,
            "sessions": sessions,
            "monitoring": not self.stopping
        }))

    async def run(self):
        self.loop = asyncio.get_running_loop()
        self.events = asyncio.Queue()

        selector = BluetoothDevice.get_device_selector()
        self.watcher = DeviceInformation.create_watcher_aqs_filter(selector)

        self.watcher_tokens = [
            self.watcher.add_added(self.on_device_added),
            self.watcher.add_updated(self.on_device_updated),
            self.watcher.add_removed(self.on_device_removed),
            self.watcher.add_enumeration_completed(self.on_enumeration_completed)
        ]

        self.watcher.start()
        self.post_to_ui(("STATE", "Monitoring", True))
        self.post_to_ui(("LOG", "Bluetooth monitoring started"))

        try:
            while True:
                event = await self.events.get()
                kind = event[0]

                if kind == "STOP":
                    break
                elif kind == "ADD":
                    _, device_id, name = event
                    await self.attach_device(device_id, name)
                elif kind == "UPDATE":
                    _, device_id, name = event
                    if device_id not in self.devices:
                        await self.attach_device(device_id, name)
                elif kind == "STATUS":
                    _, device_id, name, connected = event
                    self.process_status(device_id, name, connected)
                    self.build_snapshot()
                elif kind == "REMOVE":
                    _, device_id, _ = event
                    self.remove_device(device_id)
                    self.build_snapshot()
                elif kind == "ENUMERATION_COMPLETED":
                    self.post_to_ui(("LOG", "Initial Bluetooth device scan completed"))
                    self.build_snapshot()
                elif kind == "SNAPSHOT":
                    self.build_snapshot()
        finally:
            self.stopping = True
            self.post_to_ui(("STATE", "Stopping", False))

            try:
                if self.watcher:
                    self.watcher.stop()
            except Exception:
                pass

            for device_id in list(self.devices):
                self.remove_device(device_id)

            if self.watcher:
                remove_methods = [
                    self.watcher.remove_added,
                    self.watcher.remove_updated,
                    self.watcher.remove_removed,
                    self.watcher.remove_enumeration_completed
                ]
                for token, remove_method in zip(self.watcher_tokens, remove_methods):
                    try:
                        remove_method(token)
                    except Exception:
                        pass

            self.db.close()
            self.post_to_ui(("STATE", "Stopped", False))
            self.post_to_ui(("LOG", "Bluetooth monitoring stopped"))


class HeadphoneTrackerApp:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1000x720")
        self.root.minsize(820, 600)
        self.root.configure(bg="#f3f5f8")

        self.ui_queue = queue.Queue()
        self.monitor = None
        self.monitor_thread = None
        self.monitor_running = False
        self.latest_snapshot = None

        self._setup_style()
        self._build_ui()
        self._show_configured_devices()

        self.root.after(150, self._process_ui_queue)
        self.root.after(1000, self._refresh_live_display)
        self.start_monitoring()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _setup_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Treeview", rowheight=28, font=("Segoe UI", 9))
        style.configure(
            "Treeview.Heading",
            font=("Segoe UI", 9, "bold"),
            background="#e8edf4",
            foreground="#243247"
        )
        style.map("Treeview", background=[("selected", "#dceafe")],
                  foreground=[("selected", "#172b4d")])

    def _build_ui(self):
        header = tk.Frame(self.root, bg="#172b4d", padx=24, pady=18)
        header.pack(fill="x")

        tk.Label(
            header, text="Headphone Tracker",
            font=("Segoe UI", 22, "bold"),
            fg="white", bg="#172b4d"
        ).pack(anchor="w")

        tk.Label(
            header, text="Bluetooth connection time • Windows",
            font=("Segoe UI", 10),
            fg="#cbd5e1", bg="#172b4d"
        ).pack(anchor="w", pady=(3, 0))

        body = tk.Frame(self.root, bg="#f3f5f8", padx=22, pady=18)
        body.pack(fill="both", expand=True)

        top = tk.Frame(body, bg="#f3f5f8")
        top.pack(fill="x")

        self.status_dot = tk.Label(
            top, text="●", font=("Segoe UI", 14),
            fg="#d97706", bg="#f3f5f8"
        )
        self.status_dot.pack(side="left")

        self.status_label = tk.Label(
            top, text="Starting…", font=("Segoe UI", 10, "bold"),
            fg="#334155", bg="#f3f5f8"
        )
        self.status_label.pack(side="left", padx=(6, 0))

        self.monitor_button = ttk.Button(
            top, text="Stop monitoring", command=self.stop_monitoring
        )
        self.monitor_button.pack(side="right")

        cards = tk.Frame(body, bg="#f3f5f8")
        cards.pack(fill="x", pady=(16, 14))
        for col in range(3):
            cards.columnconfigure(col, weight=1)

        self.today_value = self._metric_card(cards, 0, "TODAY'S USAGE")
        self.week_value = self._metric_card(cards, 1, "LAST 7 DAYS")
        self.connected_value = self._metric_card(cards, 2, "CONNECTED DEVICES")

        active_frame = self._section(body, "Current connection")
        self.active_text = tk.Label(
            active_frame,
            text="Waiting for Bluetooth status…",
            font=("Segoe UI", 11),
            fg="#334155", bg="white",
            justify="left", anchor="w"
        )
        self.active_text.pack(fill="x", padx=14, pady=(0, 12))

        history_frame = self._section(body, "Recent connection sessions")
        columns = ("device", "connected", "disconnected", "duration")
        self.session_table = ttk.Treeview(
            history_frame, columns=columns, show="headings", height=8
        )
        self.session_table.heading("device", text="Headphone")
        self.session_table.heading("connected", text="Connected")
        self.session_table.heading("disconnected", text="Disconnected")
        self.session_table.heading("duration", text="Duration")
        self.session_table.column("device", width=190, anchor="w")
        self.session_table.column("connected", width=220, anchor="w")
        self.session_table.column("disconnected", width=220, anchor="w")
        self.session_table.column("duration", width=110, anchor="center")
        self.session_table.pack(fill="both", expand=True, padx=12, pady=(0, 10))

        bottom = tk.Frame(body, bg="#f3f5f8")
        bottom.pack(fill="x", pady=(12, 0))

        self.device_label = tk.Label(
            bottom, text="Tracked devices: loading…",
            font=("Segoe UI", 9), fg="#64748b", bg="#f3f5f8"
        )
        self.device_label.pack(side="left")

        self.log_label = tk.Label(
            bottom, text="Ready",
            font=("Segoe UI", 9), fg="#64748b", bg="#f3f5f8"
        )
        self.log_label.pack(side="right")

    def _metric_card(self, parent, column, heading):
        card = tk.Frame(parent, bg="white", highlightbackground="#e2e8f0",
                        highlightthickness=1, padx=16, pady=13)
        card.grid(row=0, column=column, sticky="nsew",
                  padx=(0 if column == 0 else 6, 6 if column < 2 else 0))
        tk.Label(card, text=heading, font=("Segoe UI", 8, "bold"),
                 fg="#64748b", bg="white").pack(anchor="w")
        value = tk.Label(card, text="—", font=("Segoe UI", 20, "bold"),
                         fg="#172b4d", bg="white")
        value.pack(anchor="w", pady=(5, 0))
        return value

    def _section(self, parent, heading):
        frame = tk.Frame(parent, bg="white", highlightbackground="#e2e8f0",
                         highlightthickness=1)
        frame.pack(fill="both", expand=True, pady=(0, 12))
        tk.Label(frame, text=heading, font=("Segoe UI", 11, "bold"),
                 fg="#243247", bg="white").pack(anchor="w", padx=14, pady=12)
        return frame

    def _show_configured_devices(self):
        names = ", ".join(TRACKED_DEVICE_NAMES) if TRACKED_DEVICE_NAMES else "None"
        self.device_label.config(text=f"Tracked device names: {names}")

    def start_monitoring(self):
        if self.monitor_running:
            return
        self.monitor_running = True
        self.monitor_button.config(text="Stop monitoring", command=self.stop_monitoring)
        self.monitor = BluetoothMonitor(self.ui_queue)

        def worker():
            try:
                asyncio.run(self.monitor.run())
            except Exception as error:
                self.ui_queue.put(("FATAL", str(error)))

        self.monitor_thread = threading.Thread(target=worker, daemon=True)
        self.monitor_thread.start()
        self.status_label.config(text="Starting Bluetooth monitor…")
        self.status_dot.config(fg="#d97706")

    def stop_monitoring(self):
        if not self.monitor_running:
            return
        self.monitor_running = False
        self.monitor_button.config(state="disabled", text="Stopping…")
        if self.monitor:
            self.monitor.stop()

    def _process_ui_queue(self):
        try:
            while True:
                message = self.ui_queue.get_nowait()
                kind = message[0]

                if kind == "SNAPSHOT":
                    self.latest_snapshot = message[1]
                    self._render_snapshot(self.latest_snapshot)
                elif kind == "STATE":
                    label, running = message[1], message[2]
                    self.status_label.config(text=label)
                    self.status_dot.config(fg="#16a34a" if running else "#94a3b8")
                    if label == "Stopped":
                        self.monitor_running = False
                        self.monitor_button.config(
                            state="normal", text="Start monitoring",
                            command=self.start_monitoring
                        )
                elif kind == "LOG":
                    self.log_label.config(text=message[1])
                elif kind in ("ERROR", "FATAL"):
                    self.log_label.config(text=message[1][:100])
                    if kind == "FATAL":
                        self.monitor_running = False
                        self.status_label.config(text="Monitor error")
                        self.status_dot.config(fg="#dc2626")
                        self.monitor_button.config(
                            state="normal", text="Start monitoring",
                            command=self.start_monitoring
                        )
                    messagebox.showerror("Headphone Tracker", message[1])
        except queue.Empty:
            pass

        self.root.after(150, self._process_ui_queue)

    def _render_snapshot(self, snapshot):
        history = snapshot["history"]
        today = history[0]["total_seconds"] if history else 0
        week = sum(item["total_seconds"] for item in history)

        self.today_value.config(text=format_duration(today))
        self.week_value.config(text=format_duration(week))

        active = snapshot["active"]
        self.connected_value.config(text=str(len(active)))

        if active:
            lines = []
            for item in active:
                duration = (
                    format_duration(item["duration"])
                    if item["duration"] is not None
                    else "Unknown (connected before app started)"
                )
                lines.append(f'{item["name"]}     •     {duration}')
            self.active_text.config(text="\n".join(lines))
        else:
            self.active_text.config(text="No tracked headphones currently connected.")

        for item in self.session_table.get_children():
            self.session_table.delete(item)

        for row in snapshot["sessions"]:
            (name, start_utc, observed_start_utc, end_utc,
             duration_seconds, start_confidence, end_confidence) = row

            start_display = (
                local_time(start_utc) if start_utc
                else local_time(observed_start_utc) + " (start unknown)"
            )
            end_display = local_time(end_utc) if end_utc else "Still connected"

            if not end_utc and start_utc:
                started = datetime.fromisoformat(start_utc.replace("Z", "+00:00"))
                duration_seconds = max(
                    0,
                    (datetime.now().astimezone() - started.astimezone()).total_seconds()
                )
                duration_display = format_duration(duration_seconds) + " (ongoing)"
            else:
                duration_display = format_duration(duration_seconds)

            self.session_table.insert(
                "", "end",
                values=(name, start_display, end_display, duration_display)
            )

    def _refresh_live_display(self):
        # This refresh requests a database/UI snapshot only. Bluetooth remains event-driven.
        if self.monitor_running and self.monitor:
            self.monitor.request_snapshot()
        self.root.after(1000, self._refresh_live_display)

    def on_close(self):
        if self.monitor_running and self.monitor:
            self.monitor.stop()
            self.root.after(250, self._check_worker_before_close)
        else:
            self.root.destroy()

    def _check_worker_before_close(self):
        if self.monitor_thread and self.monitor_thread.is_alive():
            self.root.after(250, self._check_worker_before_close)
        else:
            self.root.destroy()


def main():
    root = tk.Tk()
    HeadphoneTrackerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
