import asyncio
import queue
import sys
import threading
import time
from datetime import datetime

from PySide6.QtCore import Qt, QTimer, QRectF, QPointF
from PySide6.QtGui import QFont, QPainter, QPen, QColor
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from winrt.windows.devices.bluetooth import (
    BluetoothDevice,
    BluetoothConnectionStatus,
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
    secs = total % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def local_time(value):
    if not value:
        return "—"
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone().strftime("%d %b %Y, %I:%M:%S %p")


class MiniChart(QWidget):
    """Small QPainter chart; no external plotting package or web engine."""

    def __init__(self, mode="bar", parent=None):
        super().__init__(parent)
        self.mode = mode
        self.labels = []
        self.values = []
        self.setMinimumHeight(160)
        self.setSizePolicy(
            self.sizePolicy().horizontalPolicy(),
            self.sizePolicy().verticalPolicy()
        )

    def set_data(self, labels, values):
        self.labels = list(labels)
        self.values = [
            None if value is None else max(0.0, float(value))
            for value in values
        ]
        self.update()

    @staticmethod
    def _short_value(value):
        if value is None:
            return "—"
        if value >= 60:
            return f"{value / 60:.1f}h"
        return f"{value:.0f}m"

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor("#ffffff"))

        width = self.width()
        height = self.height()
        left, right, top, bottom = 38, 12, 12, 30
        plot = QRectF(
            left, top,
            max(1, width - left - right),
            max(1, height - top - bottom)
        )

        if not self.values or not any(v is not None for v in self.values):
            painter.setPen(QColor("#8290a3"))
            painter.drawText(
                self.rect(), Qt.AlignmentFlag.AlignCenter,
                "No usage data yet"
            )
            painter.end()
            return

        valid = [v for v in self.values if v is not None]
        maximum = max(valid) if valid else 0
        scale_max = max(1.0, maximum * 1.2)

        # Three subtle horizontal guide lines and labels.
        painter.setFont(QFont("Segoe UI", 8))
        for tick in range(3):
            fraction = tick / 2
            y = plot.bottom() - fraction * plot.height()
            painter.setPen(QPen(QColor("#e8edf3"), 1))
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(QColor("#8794a6"))
            label = self._short_value(scale_max * fraction)
            painter.drawText(
                QRectF(0, y - 8, left - 6, 16),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                label
            )

        count = len(self.values)
        if count == 0:
            painter.end()
            return

        slot = plot.width() / count
        points = []

        for index, value in enumerate(self.values):
            center_x = plot.left() + slot * (index + 0.5)
            if value is None:
                points.append(None)
                continue

            y = plot.bottom() - (value / scale_max) * plot.height()
            points.append(QPointF(center_x, y))

            if self.mode == "bar":
                bar_width = min(34.0, slot * 0.52)
                bar_rect = QRectF(
                    center_x - bar_width / 2,
                    y,
                    bar_width,
                    max(1.0, plot.bottom() - y)
                )
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor("#3478bd"))
                painter.drawRoundedRect(bar_rect, 4, 4)

        if self.mode == "line":
            painter.setPen(QPen(QColor("#3478bd"), 2.5))
            previous = None
            for point in points:
                if point is None:
                    previous = None
                    continue
                if previous is not None:
                    painter.drawLine(previous, point)
                previous = point

            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#3478bd"))
            for point in points:
                if point is not None:
                    painter.drawEllipse(point, 4, 4)

        # X labels and compact value labels.
        painter.setFont(QFont("Segoe UI", 8))
        for index, label in enumerate(self.labels):
            center_x = plot.left() + slot * (index + 0.5)
            painter.setPen(QColor("#64748b"))
            painter.drawText(
                QRectF(center_x - slot / 2, plot.bottom() + 6, slot, 18),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                str(label)
            )

            value = self.values[index]
            point = points[index]
            if value is not None and point is not None:
                painter.setPen(QColor("#43546a"))
                painter.drawText(
                    QRectF(center_x - slot / 2, point.y() - 19, slot, 16),
                    Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                    self._short_value(value)
                )

        painter.end()


class BluetoothMonitor:
    """Event-driven WinRT Bluetooth monitor, isolated from the Qt UI thread."""

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

    def post(self, message):
        self.ui_queue.put(message)

    def queue_event(self, event):
        if self.loop is not None and self.events is not None:
            self.loop.call_soon_threadsafe(self.events.put_nowait, event)

    def stop(self):
        self.queue_event(("STOP",))

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
                self.queue_event(
                    ("STATUS", os_device_id, device_name, connected)
                )

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
                "session_started": None,
            }

            self.db.record_status(
                device_key, device_name, connected, initial_snapshot=True
            )
            self.post(("LOG", f"Monitoring {device_name}"))
            self.publish_snapshot()

        except Exception as error:
            self.post(("ERROR", f"Could not monitor {name}: {error}"))

    def process_status(self, os_device_id, name, connected):
        entry = self.devices.get(os_device_id)
        if entry is None or entry["connected"] == connected:
            return

        entry["connected"] = connected
        self.db.record_status(entry["key"], name, connected)

        if connected:
            entry["session_started"] = time.monotonic()
            self.post(("LOG", f"{name} connected"))
        else:
            entry["session_started"] = None
            self.post(("LOG", f"{name} disconnected"))

    def remove_device(self, os_device_id):
        entry = self.devices.pop(os_device_id, None)
        if entry is None:
            return
        try:
            entry["device"].remove_connection_status_changed(entry["token"])
        except Exception as error:
            self.post(("LOG", f"Device cleanup warning: {error}"))

    def publish_snapshot(self):
        active = []
        for entry in self.devices.values():
            if entry["connected"]:
                active.append({
                    "name": entry["name"],
                    "started_monotonic": entry["session_started"],
                })

        # Database reads only on Bluetooth events, not on the GUI timer.
        history = self.db.get_usage_history(days=7)
        sessions = self.db.get_recent_sessions(limit=50)
        self.post(("SNAPSHOT", {
            "active": active,
            "history": history,
            "sessions": sessions,
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
            self.watcher.add_enumeration_completed(self.on_enumeration_completed),
        ]

        self.watcher.start()
        self.post(("STATE", "Monitoring", True))
        self.post(("LOG", "Bluetooth monitoring started"))

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
                    self.publish_snapshot()
                elif kind == "REMOVE":
                    _, device_id, _ = event
                    self.remove_device(device_id)
                    self.publish_snapshot()
                elif kind == "ENUMERATION_COMPLETED":
                    self.post(("LOG", "Initial Bluetooth enumeration completed"))
                    self.publish_snapshot()

        finally:
            self.stopping = True
            self.post(("STATE", "Stopping", False))
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
                    self.watcher.remove_enumeration_completed,
                ]
                for token, remove_method in zip(self.watcher_tokens, remove_methods):
                    try:
                        remove_method(token)
                    except Exception:
                        pass

            self.db.close()
            self.post(("STATE", "Stopped", False))
            self.post(("LOG", "Bluetooth monitoring stopped"))


class MetricCard(QFrame):
    def __init__(self, title):
        super().__init__()
        self.setObjectName("MetricCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 15, 18, 16)
        layout.setSpacing(8)

        heading = QLabel(title.upper())
        heading.setObjectName("MetricHeading")
        self.value = QLabel("—")
        self.value.setObjectName("MetricValue")
        layout.addWidget(heading)
        layout.addWidget(self.value)


class ChartCard(QFrame):
    def __init__(self, title, mode):
        super().__init__()
        self.setObjectName("Panel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(5)
        heading = QLabel(title)
        heading.setObjectName("SectionTitle")
        layout.addWidget(heading)
        self.chart = MiniChart(mode)
        layout.addWidget(self.chart)


class HeadphoneTrackerWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1180, 900)
        self.setMinimumSize(900, 700)

        self.ui_queue = queue.Queue()
        self.monitor = None
        self.monitor_thread = None
        self.monitor_running = False
        self.latest_snapshot = None
        self.closing_after_stop = False

        self._build_ui()
        self._apply_style()

        self.queue_timer = QTimer(self)
        self.queue_timer.timeout.connect(self._process_queue)
        self.queue_timer.start(150)

        # This timer updates the live clock only; no Bluetooth scan or DB query.
        self.live_timer = QTimer(self)
        self.live_timer.timeout.connect(self._update_live_duration)
        self.live_timer.start(1000)

        self.start_monitoring()

    def _build_ui(self):
        central = QWidget()
        central.setObjectName("Root")
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(24, 20, 24, 16)
        root.setSpacing(13)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("Headphone Tracker")
        title.setObjectName("AppTitle")
        subtitle = QLabel("Bluetooth connection time • Windows")
        subtitle.setObjectName("Subtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch(1)

        self.status_label = QLabel("●  Starting…")
        self.status_label.setObjectName("Status")
        header.addWidget(self.status_label)

        self.monitor_button = QPushButton("Stop monitoring")
        self.monitor_button.setObjectName("PrimaryButton")
        self.monitor_button.clicked.connect(self._toggle_monitoring)
        header.addWidget(self.monitor_button)
        root.addLayout(header)

        cards = QHBoxLayout()
        cards.setSpacing(12)
        self.today_card = MetricCard("Today's usage")
        self.week_card = MetricCard("Last 7 days")
        self.connected_card = MetricCard("Connected devices")
        cards.addWidget(self.today_card)
        cards.addWidget(self.week_card)
        cards.addWidget(self.connected_card)
        root.addLayout(cards)

        current_frame = QFrame()
        current_frame.setObjectName("Panel")
        current_layout = QVBoxLayout(current_frame)
        current_layout.setContentsMargins(16, 11, 16, 12)
        current_layout.setSpacing(6)
        current_title = QLabel("Current connection")
        current_title.setObjectName("SectionTitle")
        current_layout.addWidget(current_title)
        self.active_label = QLabel("Waiting for Bluetooth status…")
        self.active_label.setObjectName("ActiveText")
        self.active_label.setWordWrap(True)
        current_layout.addWidget(self.active_label)
        root.addWidget(current_frame)

        # Three small native-painted charts, no Matplotlib/WebEngine dependency.
        chart_row = QHBoxLayout()
        chart_row.setSpacing(12)
        self.daily_chart_card = ChartCard("Daily usage · 7 days", "bar")
        self.trend_chart_card = ChartCard("Usage trend · 7 days", "line")
        self.session_chart_card = ChartCard("Recent session lengths", "bar")
        chart_row.addWidget(self.daily_chart_card, 1)
        chart_row.addWidget(self.trend_chart_card, 1)
        chart_row.addWidget(self.session_chart_card, 1)
        root.addLayout(chart_row)

        history_frame = QFrame()
        history_frame.setObjectName("Panel")
        history_layout = QVBoxLayout(history_frame)
        history_layout.setContentsMargins(16, 11, 16, 12)
        history_layout.setSpacing(8)
        history_title = QLabel("Recent connection sessions")
        history_title.setObjectName("SectionTitle")
        history_layout.addWidget(history_title)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels([
            "Headphone", "Connected", "Disconnected", "Duration"
        ])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        for column in (1, 2):
            self.table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents
            )
        self.table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.setMinimumHeight(170)
        self.table.setMaximumHeight(230)
        history_layout.addWidget(self.table)
        root.addWidget(history_frame, 1)

        footer = QHBoxLayout()
        tracked = ", ".join(TRACKED_DEVICE_NAMES) if TRACKED_DEVICE_NAMES else "None"
        self.tracked_label = QLabel(f"Tracked devices: {tracked}")
        self.tracked_label.setObjectName("Footer")
        self.log_label = QLabel("Ready")
        self.log_label.setObjectName("Footer")
        self.log_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        footer.addWidget(self.tracked_label, 1)
        footer.addWidget(self.log_label, 1)
        root.addLayout(footer)

    def _apply_style(self):
        self.setStyleSheet("""
            QWidget#Root {
                background: #f3f6fa;
                color: #1e293b;
                font-family: "Segoe UI";
                font-size: 9pt;
            }
            QLabel#AppTitle {
                color: #152b4b;
                font-size: 22pt;
                font-weight: 700;
            }
            QLabel#Subtitle, QLabel#Footer {
                color: #718096;
                font-size: 8pt;
            }
            QLabel#Status {
                color: #2563a6;
                font-weight: 600;
                padding: 7px 10px;
                background: #e7f0fb;
                border-radius: 8px;
            }
            QPushButton#PrimaryButton {
                background: #245b9b;
                color: white;
                border: none;
                border-radius: 8px;
                padding: 9px 14px;
                font-weight: 600;
            }
            QPushButton#PrimaryButton:hover { background: #1d4d84; }
            QPushButton#PrimaryButton:disabled {
                background: #aab7c7;
                color: #f7f9fc;
            }
            QFrame#MetricCard, QFrame#Panel {
                background: white;
                border: 1px solid #e2e8f0;
                border-radius: 11px;
            }
            QLabel#MetricHeading {
                color: #718096;
                font-size: 8pt;
                font-weight: 700;
            }
            QLabel#MetricValue {
                color: #17365d;
                font-size: 19pt;
                font-weight: 700;
            }
            QLabel#SectionTitle {
                color: #243b53;
                font-size: 10pt;
                font-weight: 700;
            }
            QLabel#ActiveText {
                color: #334155;
                font-size: 10pt;
                padding: 2px 0;
            }
            QTableWidget {
                background: white;
                color: #334155;
                alternate-background-color: #f7f9fc;
                border: 1px solid #e6ebf1;
                border-radius: 7px;
                gridline-color: #edf1f5;
                selection-background-color: #dceafe;
                selection-color: #17365d;
            }
            QTableWidget::item { color: #334155; padding: 5px 6px; }
            QHeaderView::section {
                background: #eef2f7;
                color: #526174;
                border: none;
                border-bottom: 1px solid #dfe6ee;
                padding: 7px 6px;
                font-weight: 700;
            }
        """)

    def start_monitoring(self):
        if self.monitor_running:
            return

        self.monitor_running = True
        self.monitor_button.setText("Stop monitoring")
        self.monitor_button.setEnabled(True)

        try:
            self.monitor = BluetoothMonitor(self.ui_queue)
        except Exception as error:
            self.monitor_running = False
            self.status_label.setText("●  Database error")
            QMessageBox.critical(self, APP_TITLE, str(error))
            return

        def worker():
            try:
                asyncio.run(self.monitor.run())
            except Exception as error:
                self.ui_queue.put(("FATAL", str(error)))

        self.monitor_thread = threading.Thread(
            target=worker, name="BluetoothMonitor", daemon=True
        )
        self.monitor_thread.start()
        self.status_label.setText("●  Starting monitor…")

    def stop_monitoring(self):
        if self.monitor_running and self.monitor:
            self.monitor_running = False
            self.monitor_button.setEnabled(False)
            self.monitor_button.setText("Stopping…")
            self.monitor.stop()

    def _toggle_monitoring(self):
        if self.monitor_running:
            self.stop_monitoring()
        else:
            self.start_monitoring()

    def _process_queue(self):
        try:
            while True:
                message = self.ui_queue.get_nowait()
                kind = message[0]

                if kind == "SNAPSHOT":
                    self.latest_snapshot = message[1]
                    self._render_snapshot(message[1])
                elif kind == "STATE":
                    label, running = message[1], message[2]
                    self.status_label.setText(
                        ("●  " if running else "○  ") + label
                    )
                    if label == "Stopped":
                        self.monitor_running = False
                        self.monitor_button.setEnabled(True)
                        self.monitor_button.setText("Start monitoring")
                elif kind == "LOG":
                    self.log_label.setText(message[1])
                elif kind in ("ERROR", "FATAL"):
                    self.log_label.setText(message[1][:100])
                    if kind == "FATAL":
                        self.monitor_running = False
                        self.status_label.setText("●  Monitor error")
                        self.monitor_button.setEnabled(True)
                        self.monitor_button.setText("Start monitoring")
                    QMessageBox.warning(self, APP_TITLE, message[1])
        except queue.Empty:
            pass

        if self.closing_after_stop:
            if self.monitor_thread is None or not self.monitor_thread.is_alive():
                self.close()
                return

    def _render_snapshot(self, snapshot):
        history = snapshot["history"]
        today_seconds = history[0]["total_seconds"] if history else 0
        week_seconds = sum(day["total_seconds"] for day in history)

        self.today_card.value.setText(format_duration(today_seconds))
        self.week_card.value.setText(format_duration(week_seconds))

        active = snapshot["active"]
        self.connected_card.value.setText(str(len(active)))

        if active:
            lines = []
            for item in active:
                if item["started_monotonic"] is None:
                    duration = "Unknown — connected before app started"
                else:
                    duration = format_duration(
                        time.monotonic() - item["started_monotonic"]
                    )
                lines.append(f'{item["name"]}     •     {duration}')
            self.active_label.setText("\n".join(lines))
        else:
            self.active_label.setText("No tracked headphones currently connected.")

        # Daily bars and trend line use the same actual 7-day SQLite history.
        chart_days = list(reversed(history))
        day_labels = [
            datetime.fromisoformat(day["date"]).strftime("%a")
            for day in chart_days
        ]
        day_minutes = [
            day["total_seconds"] / 60.0 for day in chart_days
        ]
        self.daily_chart_card.chart.set_data(day_labels, day_minutes)
        self.trend_chart_card.chart.set_data(day_labels, day_minutes)

        # Plot up to 7 recent sessions in chronological order.
        session_rows = snapshot["sessions"]
        plotted = []
        for session in reversed(session_rows):
            name, start_utc, observed_start_utc, end_utc, duration, _, _ = session
            if duration is None and start_utc and not end_utc:
                start_dt = datetime.fromisoformat(start_utc.replace("Z", "+00:00"))
                duration = max(
                    0,
                    (datetime.now().astimezone() - start_dt.astimezone()).total_seconds()
                )
            if duration is not None:
                label_source = start_utc or observed_start_utc
                try:
                    label = datetime.fromisoformat(
                        label_source.replace("Z", "+00:00")
                    ).astimezone().strftime("%d %b")
                except Exception:
                    label = "Session"
                plotted.append((label, duration / 60.0))

        plotted = plotted[-7:]
        self.session_chart_card.chart.set_data(
            [item[0] for item in plotted],
            [item[1] for item in plotted]
        )

        self.table.setRowCount(len(session_rows))
        for row_index, session in enumerate(session_rows):
            (
                name, start_utc, observed_start_utc, end_utc,
                duration_seconds, start_confidence, end_confidence
            ) = session

            start_display = (
                local_time(start_utc)
                if start_utc
                else local_time(observed_start_utc) + " (start unknown)"
            )
            end_display = local_time(end_utc) if end_utc else "Still connected"

            if not end_utc and start_utc:
                started = datetime.fromisoformat(start_utc.replace("Z", "+00:00"))
                duration_seconds = max(
                    0,
                    (datetime.now().astimezone() - started.astimezone()).total_seconds(),
                )
                duration_display = format_duration(duration_seconds) + " (ongoing)"
            else:
                duration_display = format_duration(duration_seconds)

            values = (name, start_display, end_display, duration_display)
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setForeground(QColor("#334155"))
                if column == 3:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row_index, column, item)

    def _update_live_duration(self):
        # Display-only clock; no Bluetooth scanning and no SQLite access.
        if not self.latest_snapshot:
            return
        active = self.latest_snapshot["active"]
        if not active:
            return
        lines = []
        for item in active:
            started = item["started_monotonic"]
            duration = (
                "Unknown — connected before app started"
                if started is None
                else format_duration(time.monotonic() - started)
            )
            lines.append(f'{item["name"]}     •     {duration}')
        self.active_label.setText("\n".join(lines))

    def closeEvent(self, event):
        if self.monitor_running and self.monitor:
            event.ignore()
            self.closing_after_stop = True
            self.stop_monitoring()
            self.log_label.setText("Stopping Bluetooth monitor before exit…")
            return

        if self.monitor_thread and self.monitor_thread.is_alive():
            event.ignore()
            self.closing_after_stop = True
            return
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setFont(QFont("Segoe UI", 9))
    window = HeadphoneTrackerWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
