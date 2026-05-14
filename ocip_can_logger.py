#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OCIP CAN Logger

Layout: GTK/Touch 1280x800 bis Full-HD 1920x1080, Webapp responsiv

- Keine Energie-/PV-/Batterie-Logik
- CAN Logger fuer SocketCAN / python-can
- Mit UI zum Senden eigener CAN-Frames
- Touch-freundliches Sendefenster mit eingebauter Hex-Tastatur (Yocto-tauglich)
- Live Visualisierung im Kiosk-Stil
- Parallele Webapp ohne externe Abhaengigkeiten: Live-Dashboard, CAN-Senden, Channel/Bitrate
- Schreibt gleichzeitig:
    can_logger.log   candump-aehnlich
    can_logger.csv   Tabellenformat
    can_logger.asc   ASC-aehnlich
    can_logger_stats.json

Startbeispiele:
    python3 can_logger.py
    python3 can_logger.py --channel can0 --bitrate 250000 --configure-can
    python3 can_logger.py --windowed --log-dir /tmp/canlogs
    python3 can_logger.py --web-host 0.0.0.0 --web-port 8080
    (http://>ipvomgerät<:8080)
Abhaengigkeiten:
    sudo apt install python3-gi gir1.2-gtk-4.0 python3-cairo python3-gi-cairo
    pip install python-can

Hinweis fuer Yocto:
    Diese Variante braucht keine externe Bildschirmtastatur. Die Hex-Tastatur
    ist direkt in die GTK-App eingebaut.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from collections import deque, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Deque, Optional

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib, Pango, PangoCairo, Gdk  # type: ignore
import cairo  # type: ignore

try:
    import can  # type: ignore
except Exception:
    can = None


# -----------------------------------------------------------------------------
# Defaults
# -----------------------------------------------------------------------------

APP_ID = "solutions.ocip.canlogger"
DEFAULT_CHANNEL = os.environ.get("CAN_CHANNEL", "can0")
DEFAULT_INTERFACE = os.environ.get("CAN_INTERFACE", "socketcan")
DEFAULT_BITRATE = int(os.environ.get("CAN_BITRATE", "500000"))
DEFAULT_LOG_DIR = os.environ.get(
    "OCIP_CAN_LOG_DIR",
    os.path.join(os.path.expanduser("~"), "OCIP", "CAN_Logger", "logs"),
)
DEFAULT_MAX_LOG_BYTES = 100 * 1024 * 1024
DEFAULT_BACKUPS = 5
STATS_WRITE_INTERVAL_S = 1.0
UI_REFRESH_MS = 220
FPS_WINDOW_S = 5.0
MAX_RECENT_FRAMES = 420
MAX_ACTIVITY_POINTS = 120
DEFAULT_WEB_HOST = os.environ.get("CAN_WEB_HOST", "0.0.0.0")
DEFAULT_WEB_PORT = int(os.environ.get("CAN_WEB_PORT", "8080"))


# -----------------------------------------------------------------------------
# Utility
# -----------------------------------------------------------------------------


def now_iso(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(ts or time.time()).isoformat(timespec="milliseconds")


def fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h:d}h {m:02d}m {s:02d}s"
    if m > 0:
        return f"{m:d}m {s:02d}s"
    return f"{s:d}s"


def fmt_bytes(n: float) -> str:
    n = float(n or 0)
    units = ["B", "kB", "MB", "GB", "TB"]
    idx = 0
    while n >= 1024.0 and idx < len(units) - 1:
        n /= 1024.0
        idx += 1
    if idx == 0:
        return f"{int(n)} {units[idx]}"
    return f"{n:.1f} {units[idx]}"


def ensure_dir(path: str) -> str:
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".write_test")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        return path
    except Exception:
        fallback = os.path.join("/tmp", "OCIP", "CAN_Logger", "logs")
        os.makedirs(fallback, exist_ok=True)
        return fallback


# Externe Bildschirmtastatur wird bewusst nicht benoetigt.
# Fuer Yocto/Kiosk-Systeme ist die Hex-Tastatur direkt in der App eingebaut.

def configure_socketcan(channel: str, bitrate: int, restart_ms: int = 100) -> tuple[bool, str]:
    """
    Configure SocketCAN exactly in the requested two-step style:

        ip link set can0 down
        ip link set can0 up type can bitrate 500000 restart-ms 100

    Returns (ok, human_readable_status).
    """
    channel = str(channel or "can0").strip()
    bitrate = int(bitrate)
    restart_ms = int(restart_ms)
    cmd_down = ["ip", "link", "set", channel, "down"]
    cmd_up = ["ip", "link", "set", channel, "up", "type", "can", "bitrate", str(bitrate), "restart-ms", str(restart_ms)]
    try:
        r0 = subprocess.run(cmd_down, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        r1 = subprocess.run(cmd_up, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        ok = r0.returncode == 0 and r1.returncode == 0
        detail = " && ".join([" ".join(cmd_down), " ".join(cmd_up)])
        if ok:
            return True, f"OK: {detail}"
        err = (r0.stderr or r1.stderr or r0.stdout or r1.stdout or "ip link fehlgeschlagen").strip()
        return False, f"FEHLER: {detail} -> {err}"
    except FileNotFoundError:
        return False, "FEHLER: ip command nicht gefunden"
    except Exception as exc:
        return False, f"FEHLER: {exc}"


def parse_can_filters(filter_text: str):
    """
    python-can compatible filters from text like:
        123:7FF,1CEFFF24:1FFFFFFF
    """
    filters = []
    for part in (filter_text or "").split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            raise ValueError(f"Filter ohne Maske: {part}")
        can_id_s, mask_s = part.split(":", 1)
        filters.append({"can_id": int(can_id_s, 16), "can_mask": int(mask_s, 16), "extended": len(can_id_s) > 3})
    return filters or None


def data_to_hex(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)


def data_to_compact_hex(data: bytes) -> str:
    return "".join(f"{b:02X}" for b in data)

def parse_can_id(text: str) -> tuple[int, bool]:
    raw = str(text or "").strip().lower().replace("0x", "")
    if not raw:
        raise ValueError("CAN-ID fehlt")
    can_id = int(raw, 16)
    if can_id < 0:
        raise ValueError("CAN-ID darf nicht negativ sein")
    extended = can_id > 0x7FF or len(raw) > 3
    if extended and can_id > 0x1FFFFFFF:
        raise ValueError("Extended CAN-ID ist groesser als 0x1FFFFFFF")
    if not extended and can_id > 0x7FF:
        extended = True
    return can_id, extended


def parse_can_data(text: str) -> bytes:
    raw = str(text or "").strip()
    if not raw:
        return b""
    cleaned = raw.replace(",", " ").replace(";", " ").replace("-", " ").replace(":", " ")
    parts = [p for p in cleaned.split() if p]
    if len(parts) == 1 and len(parts[0]) > 2:
        token = parts[0].replace("0x", "").replace("0X", "")
        if len(token) % 2 != 0:
            token = "0" + token
        parts = [token[i:i + 2] for i in range(0, len(token), 2)]
    data = bytes(int(p.replace("0x", "").replace("0X", ""), 16) for p in parts)
    if len(data) > 8:
        raise ValueError("Classic CAN kann maximal 8 Datenbytes senden")
    return data


def data_to_ascii(data: bytes) -> str:
    return "".join(chr(b) if 32 <= b <= 126 else "." for b in data)


def rgba(r: float, g: float, b: float, a: float = 1.0):
    return (r, g, b, a)


def set_rgba(cr, col):
    cr.set_source_rgba(col[0], col[1], col[2], col[3])


def rounded_rect(cr, x, y, w, h, r):
    r = max(0.0, min(r, min(w, h) / 2.0))
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


# -----------------------------------------------------------------------------
# State / Logger model
# -----------------------------------------------------------------------------


@dataclass
class FrameRecord:
    ts: float
    rel_s: float
    channel: str
    direction: str
    can_id: int
    extended: bool
    is_error: bool
    is_remote: bool
    dlc: int
    data: bytes

    @property
    def id_text(self) -> str:
        if self.extended:
            return f"{self.can_id:08X}"
        return f"{self.can_id:03X}"

    @property
    def time_text(self) -> str:
        return datetime.fromtimestamp(self.ts).strftime("%H:%M:%S.%f")[:-3]

    @property
    def data_text(self) -> str:
        if self.is_remote:
            return "REMOTE"
        return data_to_hex(self.data)

    @property
    def ascii_text(self) -> str:
        if self.is_remote:
            return ""
        return data_to_ascii(self.data)


@dataclass
class LoggerState:
    channel: str = DEFAULT_CHANNEL
    interface: str = DEFAULT_INTERFACE
    bitrate: int = DEFAULT_BITRATE
    log_dir: str = DEFAULT_LOG_DIR
    start_ts: float = field(default_factory=time.time)

    can_online: bool = False
    logging_active: bool = False
    configure_ok: Optional[bool] = None
    status_text: str = "Initialisierung"
    error_text: str = ""
    config_text: str = ""
    send_text: str = ""
    restart_ms: int = 100

    rx_frames: int = 0
    tx_frames: int = 0
    err_frames: int = 0
    total_bytes: int = 0
    unique_ids: set[int] = field(default_factory=set)
    id_counts: dict[int, int] = field(default_factory=lambda: defaultdict(int))

    recent_frames: Deque[FrameRecord] = field(default_factory=lambda: deque(maxlen=MAX_RECENT_FRAMES))
    rate_events: Deque[tuple[float, int]] = field(default_factory=lambda: deque(maxlen=20000))
    activity_points: Deque[float] = field(default_factory=lambda: deque(maxlen=MAX_ACTIVITY_POINTS))
    last_activity_bucket: int = 0
    last_activity_count: int = 0
    last_frame: Optional[FrameRecord] = None

    fps: float = 0.0
    kbps: float = 0.0
    log_file_size: int = 0
    log_path: str = ""
    csv_path: str = ""
    asc_path: str = ""
    stats_path: str = ""

    lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)

    def snapshot(self) -> "LoggerState":
        with self.lock:
            snap = LoggerState(
                channel=self.channel,
                interface=self.interface,
                bitrate=self.bitrate,
                log_dir=self.log_dir,
                start_ts=self.start_ts,
            )
            snap.can_online = self.can_online
            snap.logging_active = self.logging_active
            snap.configure_ok = self.configure_ok
            snap.status_text = self.status_text
            snap.error_text = self.error_text
            snap.config_text = self.config_text
            snap.send_text = self.send_text
            snap.restart_ms = self.restart_ms
            snap.rx_frames = self.rx_frames
            snap.tx_frames = self.tx_frames
            snap.err_frames = self.err_frames
            snap.total_bytes = self.total_bytes
            snap.unique_ids = set(self.unique_ids)
            snap.id_counts = dict(self.id_counts)
            snap.recent_frames = deque(self.recent_frames, maxlen=MAX_RECENT_FRAMES)
            snap.rate_events = deque(self.rate_events, maxlen=20000)
            snap.activity_points = deque(self.activity_points, maxlen=MAX_ACTIVITY_POINTS)
            snap.last_activity_bucket = self.last_activity_bucket
            snap.last_activity_count = self.last_activity_count
            snap.last_frame = self.last_frame
            snap.fps = self.fps
            snap.kbps = self.kbps
            snap.log_file_size = self.log_file_size
            snap.log_path = self.log_path
            snap.csv_path = self.csv_path
            snap.asc_path = self.asc_path
            snap.stats_path = self.stats_path
            snap.lock = threading.RLock()
            return snap


class RotatingTextWriter:
    def __init__(self, path: str, max_bytes: int, backups: int, header: Optional[str] = None):
        self.path = path
        self.max_bytes = int(max_bytes)
        self.backups = int(backups)
        self.header = header
        self._file = None
        self._open()

    def _open(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        exists = os.path.exists(self.path)
        self._file = open(self.path, "a", encoding="utf-8", buffering=1)
        if (not exists or os.path.getsize(self.path) == 0) and self.header:
            self._file.write(self.header)
            if not self.header.endswith("\n"):
                self._file.write("\n")

    def close(self):
        try:
            if self._file:
                self._file.close()
        except Exception:
            pass
        self._file = None

    def _rotate_if_needed(self):
        if self.max_bytes <= 0:
            return
        try:
            if self._file:
                self._file.flush()
            if not os.path.exists(self.path) or os.path.getsize(self.path) < self.max_bytes:
                return
            self.close()
            oldest = f"{self.path}.{self.backups}"
            if os.path.exists(oldest):
                os.remove(oldest)
            for idx in range(self.backups - 1, 0, -1):
                src = f"{self.path}.{idx}"
                dst = f"{self.path}.{idx + 1}"
                if os.path.exists(src):
                    os.replace(src, dst)
            if os.path.exists(self.path):
                os.replace(self.path, f"{self.path}.1")
        except Exception:
            pass
        finally:
            self._open()

    def write_line(self, line: str):
        self._rotate_if_needed()
        if not self._file:
            self._open()
        self._file.write(line)
        if not line.endswith("\n"):
            self._file.write("\n")

    def size(self) -> int:
        try:
            return os.path.getsize(self.path)
        except Exception:
            return 0


class MultiFormatCanLogger:
    def __init__(self, log_dir: str, max_bytes: int, backups: int):
        self.log_dir = ensure_dir(log_dir)
        self.log_path = os.path.join(self.log_dir, "can_logger.log")
        self.csv_path = os.path.join(self.log_dir, "can_logger.csv")
        self.asc_path = os.path.join(self.log_dir, "can_logger.asc")
        self.stats_path = os.path.join(self.log_dir, "can_logger_stats.json")
        self.start_ts = time.time()

        asc_header = (
            f"date {datetime.fromtimestamp(self.start_ts).strftime('%a %b %d %H:%M:%S.%f %Y')[:-3]}\n"
            "base hex timestamps absolute\n"
            "internal events logged\n"
            "Begin Triggerblock\n"
        )
        csv_header = "iso_time,unix_time,relative_s,channel,direction,can_id,extended,error,remote,dlc,data_hex,ascii\n"

        self.log_writer = RotatingTextWriter(self.log_path, max_bytes, backups)
        self.csv_writer = RotatingTextWriter(self.csv_path, max_bytes, backups, header=csv_header)
        self.asc_writer = RotatingTextWriter(self.asc_path, max_bytes, backups, header=asc_header)
        self._lock = threading.RLock()

    def close(self):
        with self._lock:
            for writer in (self.log_writer, self.csv_writer, self.asc_writer):
                writer.close()

    def write_frame(self, frame: FrameRecord):
        with self._lock:
            ts = f"{frame.ts:.6f}"
            direction = "Rx" if frame.direction.upper() == "RX" else "Tx"
            id_suffix = "x" if frame.extended else ""
            compact = data_to_compact_hex(frame.data)
            data_spaced = data_to_hex(frame.data)
            flags = []
            if frame.is_error:
                flags.append("ERROR")
            if frame.is_remote:
                flags.append("RTR")
            flag_text = " " + " ".join(flags) if flags else ""

            # candump-aehnlich
            if frame.is_remote:
                candump = f"({ts}) {frame.channel} {frame.id_text}#R{frame.dlc}{flag_text}"
            else:
                candump = f"({ts}) {frame.channel} {frame.id_text}#{compact}{flag_text}"
            self.log_writer.write_line(candump)

            # CSV
            csv_line = [
                now_iso(frame.ts),
                f"{frame.ts:.6f}",
                f"{frame.rel_s:.6f}",
                frame.channel,
                frame.direction.upper(),
                frame.id_text,
                "1" if frame.extended else "0",
                "1" if frame.is_error else "0",
                "1" if frame.is_remote else "0",
                str(frame.dlc),
                data_spaced,
                frame.ascii_text,
            ]
            out = []
            for item in csv_line:
                text = str(item).replace('"', '""')
                if any(ch in text for ch in [",", "\n", '"']):
                    text = f'"{text}"'
                out.append(text)
            self.csv_writer.write_line(",".join(out))

            # ASC-aehnlich
            asc_id = f"{frame.id_text}{id_suffix}"
            if frame.is_remote:
                asc = f"{frame.rel_s:12.6f} 1  {asc_id:<10} {direction} r {frame.dlc:d}"
            else:
                asc = f"{frame.rel_s:12.6f} 1  {asc_id:<10} {direction} d {frame.dlc:d} {data_spaced}"
            self.asc_writer.write_line(asc)

    def write_stats(self, state: LoggerState):
        with state.lock:
            last = state.last_frame
            payload = {
                "ts": time.time(),
                "iso_time": now_iso(),
                "channel": state.channel,
                "interface": state.interface,
                "bitrate": state.bitrate,
                "can_online": state.can_online,
                "logging_active": state.logging_active,
                "status": state.status_text,
                "error": state.error_text,
                "config": state.config_text,
                "last_send": state.send_text,
                "restart_ms": state.restart_ms,
                "uptime_s": time.time() - state.start_ts,
                "rx_frames": state.rx_frames,
                "tx_frames": state.tx_frames,
                "err_frames": state.err_frames,
                "total_bytes": state.total_bytes,
                "unique_ids": len(state.unique_ids),
                "fps": state.fps,
                "kbps": state.kbps,
                "log_file_size": state.log_file_size,
                "paths": {
                    "log": state.log_path,
                    "csv": state.csv_path,
                    "asc": state.asc_path,
                    "stats": state.stats_path,
                },
                "last_frame": None if last is None else {
                    "time": last.time_text,
                    "id": last.id_text,
                    "direction": last.direction,
                    "dlc": last.dlc,
                    "data": last.data_text,
                    "ascii": last.ascii_text,
                    "extended": last.extended,
                    "error": last.is_error,
                    "remote": last.is_remote,
                },
                "top_ids": [
                    {"id": (f"{can_id:08X}" if can_id > 0x7FF else f"{can_id:03X}"), "count": count}
                    for can_id, count in sorted(state.id_counts.items(), key=lambda kv: kv[1], reverse=True)[:20]
                ],
            }
        try:
            tmp = self.stats_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.stats_path)
        except Exception:
            pass

    def size(self) -> int:
        return self.log_writer.size() + self.csv_writer.size() + self.asc_writer.size()


# -----------------------------------------------------------------------------
# CAN Receiver Thread
# -----------------------------------------------------------------------------


class CanLoggerThread(threading.Thread):
    def __init__(self, state: LoggerState, logger: MultiFormatCanLogger, configure_can: bool = False,
                 filters=None, restart_ms: int = 100):
        super().__init__(daemon=True)
        self.state = state
        self.logger = logger
        self.configure_can = configure_can
        self.filters = filters
        self.restart_ms = int(restart_ms)
        self._stop_evt = threading.Event()
        self._bus_lock = threading.RLock()
        self._pending_lock = threading.RLock()
        self._pending_config: Optional[tuple[str, int]] = None
        self.bus = None
        self._last_stats_write = 0.0

    def stop(self):
        self._stop_evt.set()
        self._close_bus()

    def request_reconfigure(self, channel: str, bitrate: int):
        with self._pending_lock:
            self._pending_config = (str(channel), int(bitrate))
        with self.state.lock:
            self.state.status_text = f"Wechsel auf {channel} · {int(bitrate)} bit/s"
            self.state.config_text = "Konfiguration angefordert"

    def send_frame(self, can_id: int, data: bytes, extended: bool = False, remote: bool = False) -> tuple[bool, str]:
        if can is None:
            return False, "python-can fehlt"
        data = bytes(data or b"")
        try:
            msg = can.Message(
                arbitration_id=int(can_id),
                data=[] if remote else data,
                is_extended_id=bool(extended),
                is_remote_frame=bool(remote),
            )
            with self._bus_lock:
                if self.bus is None:
                    raise RuntimeError("CAN Bus ist nicht offen")
                self.bus.send(msg, timeout=0.5)

            ts = time.time()
            frame = FrameRecord(
                ts=ts,
                rel_s=ts - self.state.start_ts,
                channel=self.state.channel,
                direction="TX",
                can_id=int(can_id),
                extended=bool(extended),
                is_error=False,
                is_remote=bool(remote),
                dlc=len(data),
                data=data,
            )
            self._record_frame(frame)
            text = f"TX OK: {frame.id_text}#{data_to_compact_hex(data) if not remote else 'R'}"
            with self.state.lock:
                self.state.send_text = text
                self.state.error_text = ""
            return True, text
        except Exception as exc:
            text = f"TX Fehler: {exc}"
            with self.state.lock:
                self.state.send_text = text
                self.state.error_text = text
            return False, text

    def _close_bus(self):
        with self._bus_lock:
            try:
                if self.bus is not None:
                    self.bus.shutdown()
            except Exception:
                pass
            self.bus = None

    def _open_bus(self):
        kwargs = {}
        if self.filters:
            kwargs["can_filters"] = self.filters
        with self._bus_lock:
            try:
                self.bus = can.interface.Bus(channel=self.state.channel, interface=self.state.interface, **kwargs)
            except TypeError:
                self.bus = can.interface.Bus(channel=self.state.channel, bustype=self.state.interface, **kwargs)

    def _configure_and_open(self, channel: str, bitrate: int, force_ip_link: bool):
        self._close_bus()
        with self.state.lock:
            self.state.channel = str(channel)
            self.state.bitrate = int(bitrate)
            self.state.restart_ms = self.restart_ms
            self.state.can_online = False
            self.state.logging_active = False
            self.state.status_text = f"Konfiguriere {channel}"

        if force_ip_link:
            ok, msg = configure_socketcan(channel, bitrate, self.restart_ms)
            with self.state.lock:
                self.state.configure_ok = ok
                self.state.config_text = msg
                if not ok:
                    self.state.error_text = msg
        else:
            with self.state.lock:
                self.state.config_text = "ip-link-Konfiguration beim Start uebersprungen"

        self._open_bus()
        with self.state.lock:
            self.state.can_online = True
            self.state.logging_active = True
            self.state.status_text = f"{self.state.channel} online · Logging aktiv"
            if self.state.can_online:
                self.state.error_text = "" if self.state.configure_ok is not False else self.state.error_text

    def _apply_pending_config_if_needed(self):
        pending = None
        with self._pending_lock:
            if self._pending_config is not None:
                pending = self._pending_config
                self._pending_config = None
        if pending is None:
            return
        channel, bitrate = pending
        self._configure_and_open(channel, bitrate, force_ip_link=True)

    def _update_rates_locked(self, now: float):
        cutoff = now - FPS_WINDOW_S
        while self.state.rate_events and self.state.rate_events[0][0] < cutoff:
            self.state.rate_events.popleft()
        frame_count = len(self.state.rate_events)
        byte_count = sum(item[1] for item in self.state.rate_events)
        window = max(0.001, min(FPS_WINDOW_S, now - self.state.start_ts))
        self.state.fps = frame_count / window
        self.state.kbps = (byte_count / 1024.0) / window

        bucket = int(now)
        if self.state.last_activity_bucket == 0:
            self.state.last_activity_bucket = bucket
        if bucket != self.state.last_activity_bucket:
            gap = min(10, bucket - self.state.last_activity_bucket)
            for _ in range(max(1, gap)):
                self.state.activity_points.append(float(self.state.last_activity_count))
                self.state.last_activity_count = 0
            self.state.last_activity_bucket = bucket

    def _record_frame(self, frame: FrameRecord):
        self.logger.write_frame(frame)
        now = frame.ts
        with self.state.lock:
            if frame.direction.upper() == "TX":
                self.state.tx_frames += 1
            else:
                self.state.rx_frames += 1
            if frame.is_error:
                self.state.err_frames += 1
            self.state.total_bytes += int(frame.dlc or len(frame.data))
            self.state.unique_ids.add(frame.can_id)
            self.state.id_counts[frame.can_id] = int(self.state.id_counts.get(frame.can_id, 0)) + 1
            self.state.recent_frames.appendleft(frame)
            self.state.rate_events.append((now, int(frame.dlc or len(frame.data))))
            self.state.last_activity_count += 1
            self.state.last_frame = frame
            self._update_rates_locked(now)
            self.state.log_file_size = self.logger.size()

        if now - self._last_stats_write >= STATS_WRITE_INTERVAL_S:
            self.logger.write_stats(self.state)
            self._last_stats_write = now

    def _message_to_record(self, msg) -> FrameRecord:
        ts = float(getattr(msg, "timestamp", 0.0) or time.time())
        data = bytes(getattr(msg, "data", b"") or b"")
        dlc = int(getattr(msg, "dlc", len(data)) or len(data))
        is_rx = bool(getattr(msg, "is_rx", True))
        return FrameRecord(
            ts=ts,
            rel_s=ts - self.state.start_ts,
            channel=str(getattr(msg, "channel", None) or self.state.channel),
            direction="RX" if is_rx else "TX",
            can_id=int(getattr(msg, "arbitration_id", 0) or 0),
            extended=bool(getattr(msg, "is_extended_id", False)),
            is_error=bool(getattr(msg, "is_error_frame", False)),
            is_remote=bool(getattr(msg, "is_remote_frame", False)),
            dlc=dlc,
            data=data[:dlc] if data else b"",
        )

    def run(self):
        if can is None:
            with self.state.lock:
                self.state.can_online = False
                self.state.logging_active = False
                self.state.status_text = "python-can fehlt"
                self.state.error_text = "Installiere: pip install python-can"
            return

        first_open = True
        while not self._stop_evt.is_set():
            try:
                if self.bus is None:
                    with self.state.lock:
                        ch = self.state.channel
                        br = self.state.bitrate
                    self._configure_and_open(ch, br, force_ip_link=(self.configure_can and first_open))
                    first_open = False

                self._apply_pending_config_if_needed()

                with self._bus_lock:
                    bus = self.bus
                if bus is None:
                    time.sleep(0.2)
                    continue

                msg = bus.recv(timeout=0.25)
                now = time.time()
                self._apply_pending_config_if_needed()

                if msg is None:
                    with self.state.lock:
                        self._update_rates_locked(now)
                        self.state.log_file_size = self.logger.size()
                    if now - self._last_stats_write >= STATS_WRITE_INTERVAL_S:
                        self.logger.write_stats(self.state)
                        self._last_stats_write = now
                    continue

                try:
                    self._record_frame(self._message_to_record(msg))
                except Exception as exc:
                    with self.state.lock:
                        self.state.error_text = f"Frame-Fehler: {exc}"

            except Exception as exc:
                with self.state.lock:
                    self.state.can_online = False
                    self.state.logging_active = False
                    self.state.status_text = "CAN offline · Reconnect läuft"
                    self.state.error_text = str(exc)
                self._close_bus()
                time.sleep(1.5)

        self._close_bus()
        with self.state.lock:
            self.state.logging_active = False
            self.state.can_online = False
            self.state.status_text = "Logger gestoppt"
        self.logger.write_stats(self.state)



# -----------------------------------------------------------------------------
# Embedded Web App
# -----------------------------------------------------------------------------


def parse_bool_value(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "ja", "on", "ext", "rtr")


WEB_INDEX_HTML = r"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>OCIP CAN Logger Live</title>
<style>
:root{
  color-scheme:dark;
  --bg:#020812;--card:rgba(7,20,34,.82);--card2:rgba(5,14,25,.92);
  --line:rgba(46,194,255,.34);--line2:rgba(83,255,157,.28);
  --text:#eef7ff;--muted:#8fa4b8;--cyan:#22caff;--green:#35f07a;--amber:#ffb238;--red:#ff5a64;
}
*{box-sizing:border-box} html,body{height:100%}
body{margin:0;background:radial-gradient(circle at 55% 18%,#0d2238 0,#06111f 36%,#01040a 100%);font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:var(--text);overflow:hidden;display:flex;align-items:center;justify-content:center}
.page{width:min(100vw,1920px);height:min(100vh,1080px);padding:12px;display:grid;grid-template-rows:70px 68px minmax(0,1fr) 22px;gap:8px}
.header{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:8px 14px;border:1px solid var(--line);border-radius:20px;background:linear-gradient(135deg,rgba(8,28,46,.88),rgba(3,10,19,.78));box-shadow:0 0 28px rgba(34,202,255,.10)}
.title h1{margin:0;font-size:clamp(22px,2.15vw,34px);letter-spacing:.02em}.title p{margin:2px 0 0;color:var(--muted);font-weight:650;font-size:13px}.pills{display:flex;gap:7px;align-items:center;flex-wrap:wrap;justify-content:flex-end}.pill{padding:6px 10px;border-radius:999px;border:1px solid var(--line);background:rgba(20,55,80,.55);font-weight:800;font-size:12px}.ok{color:var(--green);border-color:rgba(53,240,122,.5)}.bad{color:var(--red);border-color:rgba(255,90,100,.5)}
.stats{display:grid;grid-template-columns:repeat(6,1fr);gap:8px}.stat{min-height:0;padding:8px 12px;border:1px solid var(--line);border-radius:17px;background:var(--card);box-shadow:inset 0 1px 0 rgba(255,255,255,.05)}.stat .label{font-size:11px;color:var(--muted);font-weight:800;text-transform:uppercase;letter-spacing:.06em}.stat .value{margin-top:3px;font-size:clamp(21px,1.75vw,29px);font-weight:900;white-space:nowrap}.stat.green .value{color:var(--green)}.stat.cyan .value{color:var(--cyan)}.stat.amber .value{color:var(--amber)}
.main{display:grid;grid-template-columns:minmax(0,1fr) 540px;gap:8px;min-height:0}.main>section.card{display:grid;grid-template-rows:auto minmax(0,1fr)}.card{min-height:0;border:1px solid var(--line);border-radius:18px;background:var(--card);padding:10px;box-shadow:0 0 24px rgba(34,202,255,.075)}.card h2{margin:0 0 7px;font-size:17px;letter-spacing:.01em}.table-wrap{height:auto;min-height:0;overflow:auto;border-radius:14px;border:1px solid rgba(255,255,255,.06);background:rgba(1,6,12,.48)}table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}thead th{position:sticky;top:0;background:#071827;color:#9fb3c6;text-align:left;padding:5px 7px;font-size:10px;text-transform:uppercase;letter-spacing:.06em;z-index:1}td{padding:3px 7px;border-top:1px solid rgba(255,255,255,.055);font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:clamp(10px,.62vw,12px);line-height:1.08}td.data{font-weight:800;color:#eaf7ff;letter-spacing:.012em;white-space:nowrap}td.ascii{color:#9fb3c6;white-space:nowrap;max-width:130px;overflow:hidden;text-overflow:ellipsis}.dir-rx{color:var(--cyan);font-weight:900}.dir-tx{color:var(--green);font-weight:900}.dir-err{color:var(--red);font-weight:900}
.side{display:grid;grid-template-rows:126px 184px minmax(0,1fr);gap:8px;min-height:0}.last{border-color:rgba(255,178,56,.35)}.last-id{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:var(--amber);font-size:clamp(22px,1.65vw,31px);font-weight:950}.last-data{margin-top:4px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:clamp(15px,1.05vw,20px);font-weight:950;line-height:1.13;word-break:break-word}.forms{display:grid;gap:6px}.row{display:grid;grid-template-columns:1fr 1.45fr;gap:6px}.row3{display:grid;grid-template-columns:1fr 1fr auto;gap:6px}input,select,button{min-height:34px;border-radius:10px;border:1px solid var(--line);background:rgba(4,18,31,.95);color:var(--text);font-size:13px;padding:0 9px;font-weight:750}button{cursor:pointer;background:rgba(34,202,255,.18);font-weight:900}button.send{background:rgba(53,240,122,.18);border-color:rgba(53,240,122,.48)}label{display:flex;align-items:center;gap:6px;color:#c9d9e7;font-weight:800;font-size:13px}.msg{min-height:15px;color:var(--muted);font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.topids{overflow:hidden}.topids #topids{height:calc(100% - 27px);overflow:hidden}.topid{display:grid;grid-template-columns:92px 1fr auto;gap:8px;align-items:center;margin:5px 0;font-size:12px}.bar{height:8px;border-radius:999px;background:rgba(34,202,255,.12);overflow:hidden}.fill{height:100%;background:linear-gradient(90deg,var(--cyan),var(--green));border-radius:999px}.footer{display:flex;justify-content:space-between;align-items:center;gap:10px;color:var(--muted);font-size:11px;padding:0 4px}.path{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:#bdd7e8;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
@media (max-width:1000px){body{overflow:auto}.page{height:auto}.stats{grid-template-columns:repeat(2,1fr)}.main{grid-template-columns:1fr}.table-wrap{height:58vh}.row,.row3{grid-template-columns:1fr}.header{align-items:flex-start;flex-direction:column}.pills{justify-content:flex-start}}
</style>
</head>
<body>
<div class="page">
  <header class="header">
    <div class="title"><h1>OCIP CAN LOGGER LIVE</h1><p id="subtitle">SocketCAN · Live-Daten · Webapp parallel zur GTK-Oberfläche</p></div>
    <div class="pills"><span id="pillCan" class="pill">CAN</span><span id="pillLog" class="pill">Logging</span><span id="pillWeb" class="pill ok">WEB online</span><span id="clock" class="pill"></span></div>
  </header>
  <section class="stats">
    <div class="stat cyan"><div class="label">RX Frames</div><div id="rx" class="value">0</div></div>
    <div class="stat green"><div class="label">TX Frames</div><div id="tx" class="value">0</div></div>
    <div class="stat amber"><div class="label">Error Frames</div><div id="err" class="value">0</div></div>
    <div class="stat"><div class="label">Unique IDs</div><div id="unique" class="value">0</div></div>
    <div class="stat cyan"><div class="label">Frame Rate</div><div id="fps" class="value">0 fps</div></div>
    <div class="stat"><div class="label">Data Rate</div><div id="kbps" class="value">0 kB/s</div></div>
  </section>
  <main class="main">
    <section class="card"><h2>Live CAN Frames</h2><div class="table-wrap"><table><thead><tr><th>Zeit</th><th>R</th><th>Channel</th><th>ID</th><th>DLC</th><th>Daten</th><th>ASCII</th></tr></thead><tbody id="frames"></tbody></table></div></section>
    <aside class="side">
      <section class="card last"><h2>Letzter Frame</h2><div id="lastId" class="last-id">---</div><div id="lastData" class="last-data">Noch keine Daten</div><div id="lastMeta" class="msg"></div></section>
      <section class="card"><h2>Senden & Interface</h2><div class="forms">
        <div class="row3"><select id="channel"><option>can0</option><option>can1</option></select><select id="bitrate"><option>125000</option><option>250000</option><option selected>500000</option><option>1000000</option></select><button onclick="applyConfig()">Anwenden</button></div>
        <div class="row"><input id="canid" placeholder="CAN-ID z.B. 123 oder 1CEFFF24"><input id="candata" placeholder="Daten z.B. 66 99 BC ED 00 00 00 00"></div>
        <div class="row3"><label><input id="ext" type="checkbox"> EXT</label><label><input id="rtr" type="checkbox"> RTR</label><button class="send" onclick="sendFrame()">Senden</button></div>
        <div id="msg" class="msg"></div>
      </div></section>
      <section class="card topids"><h2>Top CAN IDs</h2><div id="topids"></div></section>
    </aside>
  </main>
  <footer class="footer"><span id="status">Status</span><span id="paths" class="path"></span></footer>
</div>
<script>
const $=id=>document.getElementById(id);
function fmt(n){return Number(n||0).toLocaleString('de-DE')}
function clsDir(d){d=(d||'').toUpperCase(); return d==='TX'?'dir-tx':(d==='RX'?'dir-rx':'dir-err')}
async function api(path, body){
  const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});
  return await r.json();
}
async function sendFrame(){
  try{ const j=await api('/api/send',{id:$('canid').value,data:$('candata').value,extended:$('ext').checked,remote:$('rtr').checked}); $('msg').textContent=j.message||JSON.stringify(j); $('msg').style.color=j.ok?'#35f07a':'#ff5a64'; }
  catch(e){$('msg').textContent='Sendefehler: '+e; $('msg').style.color='#ff5a64'}
}
async function applyConfig(){
  try{ const j=await api('/api/config',{channel:$('channel').value,bitrate:$('bitrate').value}); $('msg').textContent=j.message||JSON.stringify(j); $('msg').style.color=j.ok?'#35f07a':'#ff5a64'; }
  catch(e){$('msg').textContent='Config-Fehler: '+e; $('msg').style.color='#ff5a64'}
}
function update(s){
  $('clock').textContent=new Date().toLocaleString('de-DE');
  $('rx').textContent=fmt(s.rx_frames); $('tx').textContent=fmt(s.tx_frames); $('err').textContent=fmt(s.err_frames); $('unique').textContent=fmt(s.unique_ids);
  $('fps').textContent=(s.fps||0).toFixed(1)+' fps'; $('kbps').textContent=(s.kbps||0).toFixed(1)+' kB/s';
  $('subtitle').textContent=`${s.channel} · ${s.bitrate} bit/s · ${s.status_text||''}`;
  $('pillCan').textContent=s.can_online?'CAN online':'CAN offline'; $('pillCan').className='pill '+(s.can_online?'ok':'bad');
  $('pillLog').textContent=s.logging_active?'Logging aktiv':'Logging aus'; $('pillLog').className='pill '+(s.logging_active?'ok':'bad');
  $('status').textContent=(s.error_text?('Fehler: '+s.error_text):(s.config_text||s.status_text||'OK'));
  $('paths').textContent=s.paths?.log||'';
  $('channel').value=s.channel||'can0'; if(s.bitrate) $('bitrate').value=String(s.bitrate);
  const lf=s.last_frame;
  if(lf){ $('lastId').textContent=`${lf.direction} ${lf.id}`; $('lastData').textContent=lf.data||'REMOTE'; $('lastMeta').textContent=`${lf.time} · ${lf.channel} · DLC ${lf.dlc} · ${lf.extended?'EXT':'STD'} · ASCII ${lf.ascii||''}`; }
  const recent=(s.recent_frames||[]).slice(0,150);
  const sig=recent.length?`${recent[0].time}|${recent[0].direction}|${recent[0].id}|${recent[0].data}|${recent[0].ascii||''}`:'empty';
  if(sig!==window.__lastFrameSig){
    window.__lastFrameSig=sig;
    const rows=recent.map(f=>`<tr><td>${f.time}</td><td class="${clsDir(f.direction)}">${f.direction}</td><td>${f.channel}</td><td>${f.id}</td><td>${f.dlc}</td><td class="data">${f.data||''}</td><td class="ascii">${f.ascii||''}</td></tr>`).join('');
    $('frames').innerHTML=rows||'<tr><td colspan="7">Noch keine CAN Frames empfangen.</td></tr>';
  }
  const max=Math.max(1,...(s.top_ids||[]).map(x=>x.count||0));
  $('topids').innerHTML=(s.top_ids||[]).slice(0,12).map(x=>`<div class="topid"><code>${x.id}</code><div class="bar"><div class="fill" style="width:${Math.max(2,(x.count||0)/max*100)}%"></div></div><b>${fmt(x.count)}</b></div>`).join('')||'<div class="msg">Keine IDs</div>';
}
async function poll(){
  try{ const r=await fetch('/api/state',{cache:'no-store'}); update(await r.json()); }
  catch(e){$('status').textContent='Webapp Verbindung zur API gestört: '+e;}
}
setInterval(poll,750); poll();
</script>
</body>
</html>"""


class CanWebServerThread(threading.Thread):
    def __init__(self, state: LoggerState, host: str, port: int, send_callback, config_callback):
        super().__init__(daemon=True)
        self.state = state
        self.host = str(host or "0.0.0.0")
        self.port = int(port)
        self.send_callback = send_callback
        self.config_callback = config_callback
        self.httpd: Optional[ThreadingHTTPServer] = None
        self.url = f"http://{self.host if self.host != '0.0.0.0' else '0.0.0.0'}:{self.port}/"

    def stop(self):
        try:
            if self.httpd is not None:
                self.httpd.shutdown()
                self.httpd.server_close()
        except Exception:
            pass

    def _state_payload(self):
        with self.state.lock:
            recent = list(self.state.recent_frames)[:220]
            last = self.state.last_frame
            return {
                "ts": time.time(),
                "iso_time": now_iso(),
                "channel": self.state.channel,
                "interface": self.state.interface,
                "bitrate": self.state.bitrate,
                "restart_ms": self.state.restart_ms,
                "can_online": self.state.can_online,
                "logging_active": self.state.logging_active,
                "configure_ok": self.state.configure_ok,
                "status_text": self.state.status_text,
                "error_text": self.state.error_text,
                "config_text": self.state.config_text,
                "send_text": self.state.send_text,
                "uptime_s": time.time() - self.state.start_ts,
                "rx_frames": self.state.rx_frames,
                "tx_frames": self.state.tx_frames,
                "err_frames": self.state.err_frames,
                "total_bytes": self.state.total_bytes,
                "unique_ids": len(self.state.unique_ids),
                "fps": self.state.fps,
                "kbps": self.state.kbps,
                "log_file_size": self.state.log_file_size,
                "paths": {
                    "log": self.state.log_path,
                    "csv": self.state.csv_path,
                    "asc": self.state.asc_path,
                    "stats": self.state.stats_path,
                },
                "last_frame": None if last is None else {
                    "time": last.time_text,
                    "channel": last.channel,
                    "direction": last.direction,
                    "id": last.id_text,
                    "dlc": last.dlc,
                    "data": last.data_text,
                    "ascii": last.ascii_text,
                    "extended": last.extended,
                    "error": last.is_error,
                    "remote": last.is_remote,
                },
                "recent_frames": [
                    {
                        "time": f.time_text,
                        "channel": f.channel,
                        "direction": f.direction,
                        "id": f.id_text,
                        "dlc": f.dlc,
                        "data": f.data_text,
                        "ascii": f.ascii_text,
                        "extended": f.extended,
                        "error": f.is_error,
                        "remote": f.is_remote,
                    }
                    for f in recent
                ],
                "top_ids": [
                    {"id": (f"{can_id:08X}" if can_id > 0x7FF else f"{can_id:03X}"), "count": count}
                    for can_id, count in sorted(self.state.id_counts.items(), key=lambda kv: kv[1], reverse=True)[:30]
                ],
            }

    @staticmethod
    def _decode_post(handler):
        length = int(handler.headers.get("Content-Length", "0") or 0)
        raw = handler.rfile.read(length) if length > 0 else b""
        ctype = handler.headers.get("Content-Type", "")
        if "application/json" in ctype:
            return json.loads(raw.decode("utf-8") or "{}")
        data = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
        return {k: (v[-1] if isinstance(v, list) and v else v) for k, v in data.items()}

    def run(self):
        parent = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "CanLoggerWeb/1.0"

            def log_message(self, _format, *args):
                return

            def _send_bytes(self, status: int, content_type: str, data: bytes):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "content-type")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _send_json(self, payload, status: int = 200):
                self._send_bytes(status, "application/json; charset=utf-8", json.dumps(payload, ensure_ascii=False).encode("utf-8"))

            def do_OPTIONS(self):
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "content-type")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.end_headers()

            def do_GET(self):
                path = urlparse(self.path).path
                if path in ("/", "/index.html"):
                    self._send_bytes(200, "text/html; charset=utf-8", WEB_INDEX_HTML.encode("utf-8"))
                    return
                if path == "/api/state":
                    self._send_json(parent._state_payload())
                    return
                self._send_json({"ok": False, "message": "Nicht gefunden"}, 404)

            def do_POST(self):
                path = urlparse(self.path).path
                try:
                    data = parent._decode_post(self)
                    if path == "/api/send":
                        ok, msg = parent.send_callback(
                            str(data.get("id") or data.get("can_id") or ""),
                            str(data.get("data") or data.get("bytes") or ""),
                            parse_bool_value(data.get("extended") or data.get("ext")),
                            parse_bool_value(data.get("remote") or data.get("rtr")),
                        )
                        self._send_json({"ok": bool(ok), "message": msg})
                        return
                    if path == "/api/config":
                        ok, msg = parent.config_callback(str(data.get("channel") or "can0"), str(data.get("bitrate") or "500000"))
                        self._send_json({"ok": bool(ok), "message": msg})
                        return
                    self._send_json({"ok": False, "message": "Nicht gefunden"}, 404)
                except Exception as exc:
                    self._send_json({"ok": False, "message": str(exc)}, 500)

        try:
            self.httpd = ThreadingHTTPServer((self.host, self.port), Handler)
            with self.state.lock:
                old = self.state.status_text
                self.state.status_text = f"{old} · Webapp http://{self.host}:{self.port}/"
            self.httpd.serve_forever(poll_interval=0.25)
        except Exception as exc:
            with self.state.lock:
                self.state.error_text = f"Webapp Fehler: {exc}"

# -----------------------------------------------------------------------------
# Drawing UI
# -----------------------------------------------------------------------------


class CanLoggerDashboard(Gtk.DrawingArea):
    DESIGN_W = 1280.0
    DESIGN_H = 800.0

    BG0 = rgba(0.005, 0.012, 0.024, 1.0)
    TEXT = rgba(0.94, 0.97, 1.0, 0.96)
    MUTED = rgba(0.58, 0.66, 0.76, 0.90)
    CYAN = rgba(0.12, 0.78, 1.00, 0.96)
    BLUE = rgba(0.20, 0.48, 1.00, 0.92)
    GREEN = rgba(0.22, 0.95, 0.46, 0.96)
    AMBER = rgba(1.00, 0.65, 0.18, 0.96)
    RED = rgba(1.00, 0.25, 0.28, 0.95)
    PURPLE = rgba(0.78, 0.32, 1.00, 0.95)

    def __init__(self, state: LoggerState):
        super().__init__()
        self.state = state
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_draw_func(self.on_draw)

    # ----- text helpers
    def _layout(self, cr, text, size_px=16, weight=Pango.Weight.NORMAL, max_width=None,
                ellipsize=None, align=Pango.Alignment.LEFT, family="Sans", letter_spacing=None):
        layout = PangoCairo.create_layout(cr)
        layout.set_text(str(text or ""))
        fd = Pango.FontDescription()
        fd.set_family(family)
        fd.set_weight(weight)
        fd.set_size(int(size_px * Pango.SCALE))
        layout.set_font_description(fd)
        if max_width is not None:
            layout.set_width(int(max_width * Pango.SCALE))
            layout.set_single_paragraph_mode(True)
        if ellipsize is not None:
            layout.set_ellipsize(ellipsize)
        layout.set_alignment(align)
        if letter_spacing is not None:
            attrs = Pango.AttrList()
            attrs.insert(Pango.attr_letter_spacing_new(int(letter_spacing)))
            layout.set_attributes(attrs)
        return layout

    def text(self, cr, x, y, text, size=16, color=None, weight=Pango.Weight.NORMAL,
             max_width=None, ellipsize=None, align=Pango.Alignment.LEFT, family="Sans",
             letter_spacing=None):
        color = color or self.TEXT
        layout = self._layout(cr, text, size, weight, max_width, ellipsize, align, family, letter_spacing)
        set_rgba(cr, color)
        cr.move_to(x, y)
        PangoCairo.show_layout(cr, layout)
        return layout.get_pixel_size()

    def text_center(self, cr, cx, cy, text, size=16, color=None, weight=Pango.Weight.NORMAL,
                    max_width=None, ellipsize=None, family="Sans"):
        if max_width is None:
            layout = self._layout(cr, text, size, weight, family=family)
            w, h = layout.get_pixel_size()
            set_rgba(cr, color or self.TEXT)
            cr.move_to(cx - w / 2.0, cy - h / 2.0)
            PangoCairo.show_layout(cr, layout)
            return w, h
        layout = self._layout(cr, text, size, weight, max_width=max_width,
                              ellipsize=ellipsize or Pango.EllipsizeMode.END,
                              align=Pango.Alignment.CENTER, family=family)
        _, h = layout.get_pixel_size()
        set_rgba(cr, color or self.TEXT)
        cr.move_to(cx - max_width / 2.0, cy - h / 2.0)
        PangoCairo.show_layout(cr, layout)
        return max_width, h

    def value_unit(self, cr, x, y, value, unit, value_size, unit_size, color=None):
        color = color or self.TEXT
        vw, vh = self.text(cr, x, y, value, value_size, color, Pango.Weight.BOLD, family="Sans")
        self.text(cr, x + vw + value_size * 0.12, y + vh * 0.54, unit, unit_size,
                  rgba(color[0], color[1], color[2], 0.86), Pango.Weight.SEMIBOLD)

    # ----- primitive visuals
    def card(self, cr, x, y, w, h, r=24, accent=None, alpha=0.78):
        accent = accent or self.CYAN
        cr.save()
        set_rgba(cr, rgba(0, 0, 0, 0.28))
        rounded_rect(cr, x, y + h * 0.025, w, h, r)
        cr.fill()
        cr.restore()

        pat = cairo.LinearGradient(x, y, x + w, y + h)
        pat.add_color_stop_rgba(0.0, 0.035, 0.065, 0.105, alpha)
        pat.add_color_stop_rgba(0.55, 0.020, 0.035, 0.060, alpha)
        pat.add_color_stop_rgba(1.0, 0.010, 0.020, 0.038, alpha)
        cr.save()
        rounded_rect(cr, x, y, w, h, r)
        cr.set_source(pat)
        cr.fill_preserve()
        set_rgba(cr, rgba(accent[0], accent[1], accent[2], 0.34))
        cr.set_line_width(max(1.2, min(w, h) * 0.006))
        cr.stroke()
        cr.restore()

        sheen = cairo.LinearGradient(x, y, x, y + h * 0.42)
        sheen.add_color_stop_rgba(0.0, 1.0, 1.0, 1.0, 0.060)
        sheen.add_color_stop_rgba(1.0, 1.0, 1.0, 1.0, 0.000)
        cr.save()
        rounded_rect(cr, x + 1, y + 1, w - 2, h * 0.48, max(1, r - 2))
        cr.set_source(sheen)
        cr.fill()
        cr.restore()

    def pill(self, cr, x, y, w, h, text, color, active=True, icon="dot"):
        bg_alpha = 0.18 if active else 0.08
        line_alpha = 0.70 if active else 0.28
        cr.save()
        rounded_rect(cr, x, y, w, h, h / 2)
        set_rgba(cr, rgba(color[0], color[1], color[2], bg_alpha))
        cr.fill_preserve()
        set_rgba(cr, rgba(color[0], color[1], color[2], line_alpha))
        cr.set_line_width(max(1.2, h * 0.035))
        cr.stroke()
        cr.restore()
        cx = x + h * 0.42
        cy = y + h / 2
        if icon == "dot":
            cr.save()
            set_rgba(cr, rgba(color[0], color[1], color[2], 0.24))
            cr.arc(cx, cy, h * 0.24, 0, 2 * math.pi)
            cr.fill()
            set_rgba(cr, color if active else self.MUTED)
            cr.arc(cx, cy, h * 0.13, 0, 2 * math.pi)
            cr.fill()
            cr.restore()
        elif icon == "bus":
            self.icon_bus(cr, cx, cy, h * 0.48, color if active else self.MUTED)
        elif icon == "file":
            self.icon_file(cr, cx, cy, h * 0.50, color if active else self.MUTED)
        self.text(cr, x + h * 0.78, y + h * 0.24, text, size=h * 0.35,
                  color=self.TEXT if active else self.MUTED, weight=Pango.Weight.SEMIBOLD,
                  max_width=w - h * 0.95, ellipsize=Pango.EllipsizeMode.END)

    def icon_bolt(self, cr, cx, cy, s, col):
        pts = [(-0.04, -0.55), (0.25, -0.55), (0.08, -0.10), (0.31, -0.10),
               (-0.16, 0.58), (-0.02, 0.10), (-0.28, 0.10)]
        cr.save()
        for k, a in [(1.42, 0.08), (1.18, 0.14)]:
            set_rgba(cr, rgba(col[0], col[1], col[2], a))
            cr.move_to(cx + s * pts[0][0] * k, cy + s * pts[0][1] * k)
            for px, py in pts[1:]:
                cr.line_to(cx + s * px * k, cy + s * py * k)
            cr.close_path()
            cr.fill()
        grad = cairo.LinearGradient(cx, cy - s * 0.62, cx, cy + s * 0.62)
        grad.add_color_stop_rgba(0, min(1, col[0] + 0.25), min(1, col[1] + 0.25), min(1, col[2] + 0.25), 1)
        grad.add_color_stop_rgba(1, col[0], col[1], col[2], 0.95)
        cr.move_to(cx + s * pts[0][0], cy + s * pts[0][1])
        for px, py in pts[1:]:
            cr.line_to(cx + s * px, cy + s * py)
        cr.close_path()
        cr.set_source(grad)
        cr.fill()
        cr.restore()

    def icon_bus(self, cr, cx, cy, s, col):
        cr.save()
        set_rgba(cr, col)
        cr.set_line_width(max(1.4, s * 0.09))
        cr.set_line_cap(cairo.LineCap.ROUND)
        for dx in (-0.34, 0, 0.34):
            rounded_rect(cr, cx + dx * s - s * 0.10, cy - s * 0.44, s * 0.20, s * 0.20, s * 0.04)
            cr.stroke()
            cr.move_to(cx + dx * s, cy - s * 0.24)
            cr.line_to(cx + dx * s, cy + s * 0.16)
            cr.stroke()
        cr.move_to(cx - s * 0.34, cy + s * 0.16)
        cr.line_to(cx + s * 0.34, cy + s * 0.16)
        cr.stroke()
        cr.restore()

    def icon_wave(self, cr, x, y, w, h, col):
        cr.save()
        set_rgba(cr, col)
        cr.set_line_width(max(2.0, h * 0.10))
        cr.set_line_cap(cairo.LineCap.ROUND)
        pts = [
            (0.00, 0.55), (0.12, 0.55), (0.18, 0.36), (0.25, 0.74),
            (0.35, 0.12), (0.46, 0.92), (0.55, 0.48), (0.70, 0.48),
            (0.78, 0.34), (0.86, 0.62), (1.00, 0.62),
        ]
        cr.move_to(x + pts[0][0] * w, y + pts[0][1] * h)
        for px, py in pts[1:]:
            cr.line_to(x + px * w, y + py * h)
        cr.stroke()
        cr.restore()

    def icon_file(self, cr, cx, cy, s, col):
        cr.save()
        set_rgba(cr, col)
        cr.set_line_width(max(1.4, s * 0.08))
        x, y, w, h = cx - s * 0.32, cy - s * 0.44, s * 0.64, s * 0.88
        cr.move_to(x, y)
        cr.line_to(x + w * 0.68, y)
        cr.line_to(x + w, y + h * 0.28)
        cr.line_to(x + w, y + h)
        cr.line_to(x, y + h)
        cr.close_path()
        cr.stroke()
        cr.move_to(x + w * 0.68, y)
        cr.line_to(x + w * 0.68, y + h * 0.28)
        cr.line_to(x + w, y + h * 0.28)
        cr.stroke()
        for i in range(3):
            yy = y + h * (0.48 + i * 0.15)
            cr.move_to(x + w * 0.18, yy)
            cr.line_to(x + w * 0.82, yy)
            cr.stroke()
        cr.restore()

    def icon_shield(self, cr, cx, cy, s, col):
        cr.save()
        set_rgba(cr, col)
        cr.set_line_width(max(1.4, s * 0.08))
        cr.move_to(cx, cy - s * 0.48)
        cr.line_to(cx + s * 0.40, cy - s * 0.30)
        cr.line_to(cx + s * 0.33, cy + s * 0.25)
        cr.curve_to(cx + s * 0.20, cy + s * 0.44, cx + s * 0.08, cy + s * 0.53, cx, cy + s * 0.58)
        cr.curve_to(cx - s * 0.08, cy + s * 0.53, cx - s * 0.20, cy + s * 0.44, cx - s * 0.33, cy + s * 0.25)
        cr.line_to(cx - s * 0.40, cy - s * 0.30)
        cr.close_path()
        cr.stroke()
        cr.move_to(cx - s * 0.16, cy + s * 0.02)
        cr.line_to(cx - s * 0.03, cy + s * 0.16)
        cr.line_to(cx + s * 0.20, cy - s * 0.12)
        cr.stroke()
        cr.restore()

    def grid_lines(self, cr, x, y, w, h, cols=12, rows=6):
        cr.save()
        set_rgba(cr, rgba(0.45, 0.72, 1.0, 0.055))
        cr.set_line_width(1.0)
        for i in range(cols + 1):
            xx = x + w * i / cols
            cr.move_to(xx, y)
            cr.line_to(xx, y + h)
        for j in range(rows + 1):
            yy = y + h * j / rows
            cr.move_to(x, yy)
            cr.line_to(x + w, yy)
        cr.stroke()
        cr.restore()

    def stat_tile(self, cr, x, y, w, h, label, value, unit="", color=None, icon="dot"):
        color = color or self.CYAN
        self.card(cr, x, y, w, h, r=h * 0.18, accent=color, alpha=0.52)
        if icon == "rx":
            sym = "↓"
        elif icon == "tx":
            sym = "↑"
        elif icon == "id":
            sym = "#"
        elif icon == "fps":
            sym = "↯"
        elif icon == "size":
            sym = "◫"
        else:
            sym = "•"
        cr.save()
        set_rgba(cr, rgba(color[0], color[1], color[2], 0.12))
        cr.arc(x + h * 0.45, y + h * 0.50, h * 0.26, 0, 2 * math.pi)
        cr.fill()
        cr.restore()
        self.text_center(cr, x + h * 0.45, y + h * 0.50, sym, size=h * 0.34,
                         color=color, weight=Pango.Weight.BOLD)
        self.text(cr, x + h * 0.82, y + h * 0.18, label, size=h * 0.18,
                  color=self.MUTED, weight=Pango.Weight.MEDIUM,
                  max_width=w - h * 0.92, ellipsize=Pango.EllipsizeMode.END)
        val_text = str(value)
        self.text(cr, x + h * 0.82, y + h * 0.43, val_text, size=h * 0.30,
                  color=self.TEXT, weight=Pango.Weight.BOLD,
                  max_width=w - h * 1.05, ellipsize=Pango.EllipsizeMode.END)
        if unit:
            self.text(cr, x + w - h * 0.20, y + h * 0.49, unit, size=h * 0.18,
                      color=rgba(color[0], color[1], color[2], 0.88), weight=Pango.Weight.SEMIBOLD,
                      align=Pango.Alignment.RIGHT)

    def draw_activity(self, cr, x, y, w, h, points, fps):
        self.card(cr, x, y, w, h, r=22, accent=self.CYAN, alpha=0.58)
        self.icon_wave(cr, x + 24, y + 18, 44, 30, self.CYAN)
        self.text(cr, x + 82, y + 20, "Bus-Aktivität", size=22, color=self.TEXT, weight=Pango.Weight.SEMIBOLD)
        self.text(cr, x + w - 200, y + 22, f"{fps:.1f} fps aktuell", size=18, color=self.CYAN,
                  weight=Pango.Weight.SEMIBOLD, max_width=180, ellipsize=Pango.EllipsizeMode.END)
        gx, gy, gw, gh = x + 30, y + 70, w - 60, h - 100
        self.grid_lines(cr, gx, gy, gw, gh, cols=12, rows=4)
        vals = list(points)
        if len(vals) < 2:
            vals = [0.0, 0.0]
        vmax = max(vals + [1.0])

        # Area glow
        cr.save()
        grad = cairo.LinearGradient(gx, gy, gx, gy + gh)
        grad.add_color_stop_rgba(0.0, self.CYAN[0], self.CYAN[1], self.CYAN[2], 0.20)
        grad.add_color_stop_rgba(1.0, self.CYAN[0], self.CYAN[1], self.CYAN[2], 0.00)
        cr.move_to(gx, gy + gh)
        for i, v in enumerate(vals):
            px = gx + gw * i / max(1, len(vals) - 1)
            py = gy + gh - gh * min(1.0, v / vmax)
            cr.line_to(px, py)
        cr.line_to(gx + gw, gy + gh)
        cr.close_path()
        cr.set_source(grad)
        cr.fill()
        cr.restore()

        # Line
        cr.save()
        set_rgba(cr, self.CYAN)
        cr.set_line_width(3.0)
        cr.set_line_join(cairo.LineJoin.ROUND)
        for i, v in enumerate(vals):
            px = gx + gw * i / max(1, len(vals) - 1)
            py = gy + gh - gh * min(1.0, v / vmax)
            if i == 0:
                cr.move_to(px, py)
            else:
                cr.line_to(px, py)
        cr.stroke()
        cr.restore()
        self.text(cr, gx, y + h - 26, "Frames/Sekunde, letzter Verlauf", size=14, color=self.MUTED)
        self.text(cr, gx + gw - 180, y + h - 26, f"Peak {vmax:.0f} fps", size=14, color=self.MUTED,
                  max_width=180, ellipsize=Pango.EllipsizeMode.END)

    def draw_recent_table(self, cr, x, y, w, h, frames):
        """Live table tuned for the 1280x800 touchscreen layout."""
        compact = h < 520 or w < 1500
        self.card(cr, x, y, w, h, r=18 if compact else 24, accent=self.CYAN, alpha=0.64)
        icon_x = x + (20 if compact else 28)
        icon_y = y + (15 if compact else 20)
        self.icon_wave(cr, icon_x, icon_y, 38 if compact else 48, 25 if compact else 32, self.CYAN)
        self.text(cr, x + (68 if compact else 88), y + (12 if compact else 18), "Live CAN Frames",
                  size=22 if compact else 28, color=self.TEXT, weight=Pango.Weight.SEMIBOLD)
        # Untertitel bewusst entfernt: mehr Platz fuer Frames und ruhigeres Layout.
        self.pill(cr, x + w - (128 if compact else 184), y + (16 if compact else 20),
                  100 if compact else 154, 30 if compact else 38, "LIVE", self.GREEN, active=True, icon="dot")

        tx = x + (14 if compact else 22)
        ty = y + (62 if compact else 84)
        tw = w - (28 if compact else 44)
        header_h = 30 if compact else 42
        row_h = 25 if compact else 34
        cols = [0.00, 0.105, 0.178, 0.305, 0.350, 0.820, 1.00]
        names = ["Zeit", "R", "ID", "DLC", "Daten", "ASCII"]
        cr.save()
        rounded_rect(cr, tx, ty, tw, header_h, 10 if compact else 13)
        set_rgba(cr, rgba(0.10, 0.17, 0.25, 0.72))
        cr.fill_preserve()
        set_rgba(cr, rgba(0.55, 0.78, 1.0, 0.16))
        cr.set_line_width(1.0)
        cr.stroke()
        cr.restore()
        for i, name in enumerate(names):
            cx = tx + tw * cols[i] + (8 if compact else 12)
            self.text(cr, cx, ty + (7 if compact else 10), name,
                      size=11 if compact else 15, color=rgba(0.82, 0.90, 0.98, 0.96),
                      weight=Pango.Weight.SEMIBOLD)

        bottom_pad = 14 if compact else 12
        max_rows = max(1, int((h - (ty - y) - header_h - bottom_pad) / row_h))
        visible = list(frames)[:max_rows]
        mono = "Monospace"
        for r, fr in enumerate(visible):
            ry = ty + header_h + r * row_h
            if r % 2 == 0:
                cr.save()
                set_rgba(cr, rgba(0.09, 0.13, 0.19, 0.34))
                cr.rectangle(tx, ry, tw, row_h)
                cr.fill()
                cr.restore()
            set_rgba(cr, rgba(0.52, 0.72, 0.94, 0.10))
            cr.set_line_width(1.0)
            cr.move_to(tx, ry + row_h)
            cr.line_to(tx + tw, ry + row_h)
            cr.stroke()

            color = self.GREEN if fr.direction.upper() == "TX" else self.CYAN
            if fr.is_error:
                color = self.RED
            remote = "R" if fr.is_remote else ""
            err = "E" if fr.is_error else ""
            direction = ("^" if fr.direction.upper() == "TX" else "v") + fr.direction + err + remote
            text_y = ry + (6 if compact else 7)
            fs = 11 if compact else 13
            data_fs = 12 if compact else 14
            ascii_fs = 11 if compact else 13
            pad = 8 if compact else 12
            self.text(cr, tx + tw * cols[0] + pad, text_y, fr.time_text, size=fs, color=self.MUTED, family=mono)
            self.text(cr, tx + tw * cols[1] + pad, text_y, direction,
                      size=fs, color=color, weight=Pango.Weight.SEMIBOLD, family=mono)
            self.text(cr, tx + tw * cols[2] + pad, text_y, fr.id_text, size=fs, color=self.TEXT, family=mono)
            self.text(cr, tx + tw * cols[3] + pad, text_y, str(fr.dlc), size=fs, color=self.TEXT, family=mono)
            self.text(cr, tx + tw * cols[4] + pad, text_y, fr.data_text, size=data_fs,
                      color=rgba(0.90, 0.96, 1.0, 0.98), weight=Pango.Weight.SEMIBOLD,
                      max_width=tw * (cols[5] - cols[4]) - 2 * pad, ellipsize=Pango.EllipsizeMode.END, family=mono)
            self.text(cr, tx + tw * cols[5] + pad, text_y, fr.ascii_text, size=ascii_fs,
                      color=rgba(0.70, 0.78, 0.86, 0.86),
                      max_width=tw * (cols[6] - cols[5]) - 2 * pad, ellipsize=Pango.EllipsizeMode.END, family=mono)

        cr.save()
        set_rgba(cr, rgba(0.55, 0.78, 1.0, 0.10))
        cr.set_line_width(1.0)
        for c in cols[1:-1]:
            xx = tx + tw * c
            cr.move_to(xx, ty)
            cr.line_to(xx, ty + header_h + max_rows * row_h)
        cr.stroke()
        cr.restore()

        if not visible:
            self.text_center(cr, x + w / 2, y + h / 2 + 18, "Warte auf CAN-Frames",
                             size=20 if compact else 24, color=self.MUTED, weight=Pango.Weight.MEDIUM)

    def draw_last_frame_card(self, cr, x, y, w, h, fr: Optional[FrameRecord]):
        """Compact last-frame strip. Text is intentionally smaller so it stays inside the amber frame."""
        compact = h < 70 or w < 1500
        self.card(cr, x, y, w, h, r=16 if compact else 20, accent=self.AMBER if fr else self.MUTED, alpha=0.60)

        title_size = 13 if compact else 17
        id_size = 16 if compact else 23
        meta_size = 10 if compact else 14
        data_size = 15 if compact else 21
        ascii_size = 9 if compact else 12

        self.text(cr, x + 16, y + (8 if compact else 13), "Letzter Frame",
                  size=title_size, color=self.TEXT, weight=Pango.Weight.SEMIBOLD,
                  max_width=120 if compact else 160, ellipsize=Pango.EllipsizeMode.END)
        if fr is None:
            self.text(cr, x + (138 if compact else 190), y + (12 if compact else 18), "Noch keine CAN-Daten",
                      size=15 if compact else 20, color=self.MUTED, weight=Pango.Weight.MEDIUM,
                      max_width=w - (160 if compact else 220), ellipsize=Pango.EllipsizeMode.END)
            return

        color = self.GREEN if fr.direction.upper() == "TX" else self.CYAN
        if fr.is_error:
            color = self.RED

        id_x = x + (138 if compact else 190)
        meta_x = x + (275 if compact else 382)
        data_x = x + (430 if compact else 585)
        line_y = y + (9 if compact else 13)

        self.text(cr, id_x, line_y, fr.id_text,
                  size=id_size, color=color, weight=Pango.Weight.BOLD, family="Monospace",
                  max_width=(128 if compact else 180), ellipsize=Pango.EllipsizeMode.END)
        self.text(cr, meta_x, line_y + (4 if compact else 6), f"{fr.direction}  DLC {fr.dlc}  {fr.time_text}",
                  size=meta_size, color=self.MUTED, weight=Pango.Weight.MEDIUM, family="Monospace",
                  max_width=(145 if compact else 190), ellipsize=Pango.EllipsizeMode.END)
        self.text(cr, data_x, line_y, fr.data_text or "-", size=data_size,
                  color=self.TEXT, weight=Pango.Weight.BOLD, max_width=w - (data_x - x) - 24,
                  ellipsize=Pango.EllipsizeMode.END, family="Monospace")
        if fr.ascii_text:
            self.text(cr, data_x, y + h - (18 if compact else 24), f"ASCII {fr.ascii_text}",
                      size=ascii_size, color=self.MUTED,
                      max_width=w - (data_x - x) - 24, ellipsize=Pango.EllipsizeMode.END, family="Monospace")

    def draw_log_files_card(self, cr, x, y, w, h, st: LoggerState):
        self.card(cr, x, y, w, h, r=24, accent=self.BLUE, alpha=0.62)
        self.icon_file(cr, x + 42, y + 42, 34, self.CYAN)
        self.text(cr, x + 76, y + 24, "Log-Dateien", size=22, color=self.TEXT, weight=Pango.Weight.SEMIBOLD)
        y0 = y + 78
        items = [
            ("LOG", st.log_path),
            ("CSV", st.csv_path),
            ("ASC", st.asc_path),
            ("JSON", st.stats_path),
        ]
        for idx, (label, path) in enumerate(items):
            yy = y0 + idx * 44
            self.text(cr, x + 28, yy, label, size=15, color=self.CYAN, weight=Pango.Weight.BOLD, family="Monospace")
            self.text(cr, x + 82, yy, path, size=15, color=rgba(0.84, 0.92, 1.0, 0.90),
                      max_width=w - 105, ellipsize=Pango.EllipsizeMode.START, family="Monospace")
        self.text(cr, x + 28, y + h - 54, "Gesamtgröße", size=14, color=self.MUTED)
        self.text(cr, x + 28, y + h - 33, fmt_bytes(st.log_file_size), size=22, color=self.TEXT, weight=Pango.Weight.SEMIBOLD)

    def draw_top_ids_card(self, cr, x, y, w, h, id_counts):
        self.card(cr, x, y, w, h, r=24, accent=self.PURPLE, alpha=0.62)
        self.text(cr, x + 28, y + 22, "Top CAN-IDs", size=22, color=self.TEXT, weight=Pango.Weight.SEMIBOLD)
        items = sorted(id_counts.items(), key=lambda kv: kv[1], reverse=True)[:8]
        if not items:
            self.text_center(cr, x + w / 2, y + h / 2 + 8, "Warte auf Frames", size=20, color=self.MUTED)
            return
        max_count = max(count for _, count in items) or 1
        base_y = y + 70
        row_h = (h - 92) / max(1, len(items))
        for i, (can_id, count) in enumerate(items):
            yy = base_y + i * row_h
            id_text = f"{can_id:08X}" if can_id > 0x7FF else f"{can_id:03X}"
            self.text(cr, x + 28, yy + row_h * 0.20, id_text, size=min(16, row_h * 0.36), color=self.TEXT,
                      weight=Pango.Weight.SEMIBOLD, family="Monospace")
            bx = x + 142
            bw = w - 224
            bh = max(5, row_h * 0.22)
            by = yy + row_h * 0.44
            cr.save()
            rounded_rect(cr, bx, by, bw, bh, bh / 2)
            set_rgba(cr, rgba(0.20, 0.28, 0.38, 0.52))
            cr.fill()
            rounded_rect(cr, bx, by, bw * min(1.0, count / max_count), bh, bh / 2)
            set_rgba(cr, rgba(self.PURPLE[0], self.PURPLE[1], self.PURPLE[2], 0.82))
            cr.fill()
            cr.restore()
            self.text(cr, x + w - 68, yy + row_h * 0.22, str(count), size=min(15, row_h * 0.34),
                      color=self.MUTED, family="Monospace")

    def draw_footer(self, cr, x, y, w, h, st: LoggerState):
        compact = h < 60 or w < 1500
        cr.save()
        pat = cairo.LinearGradient(x, y, x, y + h)
        pat.add_color_stop_rgba(0, 0.02, 0.04, 0.07, 0.82)
        pat.add_color_stop_rgba(1, 0.01, 0.02, 0.04, 0.92)
        cr.rectangle(x, y, w, h)
        cr.set_source(pat)
        cr.fill()
        set_rgba(cr, rgba(0.28, 0.72, 1.0, 0.18))
        cr.set_line_width(1.0)
        cr.move_to(x, y)
        cr.line_to(x + w, y)
        cr.stroke()
        cr.restore()
        status_col = self.GREEN if st.can_online and st.logging_active else self.RED
        yy = y + h * (0.34 if compact else 0.30)
        self.icon_shield(cr, x + 28, y + h / 2, 18 if compact else 24, status_col)
        self.text(cr, x + (54 if compact else 66), yy, st.status_text,
                  size=13 if compact else 18, color=status_col, weight=Pango.Weight.SEMIBOLD,
                  max_width=w * 0.34, ellipsize=Pango.EllipsizeMode.END)
        bottom_info = f"Log: {st.log_path}  ·  Größe: {fmt_bytes(st.log_file_size)}"
        self.text(cr, x + w * 0.40, yy + (1 if compact else 2), bottom_info,
                  size=11 if compact else 14, color=self.MUTED, family="Monospace",
                  max_width=w * 0.56, ellipsize=Pango.EllipsizeMode.START)

    def on_draw(self, area, cr, width, height):
        st = self.state.snapshot()
        cr.set_antialias(cairo.Antialias.BEST)
        s = min(width / self.DESIGN_W, height / self.DESIGN_H)
        ox = (width - self.DESIGN_W * s) / 2.0
        oy = (height - self.DESIGN_H * s) / 2.0
        X = lambda v: ox + v * s
        Y = lambda v: oy + v * s
        S = lambda v: v * s

        bg = cairo.RadialGradient(width * 0.50, height * 0.30, min(width, height) * 0.04,
                                  width * 0.50, height * 0.30, max(width, height) * 0.82)
        bg.add_color_stop_rgba(0.00, 0.04, 0.09, 0.15, 1.0)
        bg.add_color_stop_rgba(0.55, 0.01, 0.03, 0.06, 1.0)
        bg.add_color_stop_rgba(1.00, 0.00, 0.00, 0.00, 1.0)
        cr.rectangle(0, 0, width, height)
        cr.set_source(bg)
        cr.fill()

        px, py, pw, ph = X(18), Y(14), S(1244), S(772)
        self.card(cr, px, py, pw, ph, r=S(24), accent=self.CYAN, alpha=0.32)

        # Header for 1280x800.
        hx, hy = px + S(24), py + S(20)
        cr.save()
        set_rgba(cr, rgba(self.CYAN[0], self.CYAN[1], self.CYAN[2], 0.14))
        cr.arc(hx + S(28), hy + S(28), S(28), 0, 2 * math.pi)
        cr.fill_preserve()
        set_rgba(cr, rgba(self.CYAN[0], self.CYAN[1], self.CYAN[2], 0.40))
        cr.set_line_width(S(1.6))
        cr.stroke()
        cr.restore()
        self.icon_bolt(cr, hx + S(28), hy + S(28), S(32), self.CYAN)
        self.text(cr, hx + S(72), hy + S(0), "OCIP CAN LOGGER", size=S(30), color=self.TEXT,
                  weight=Pango.Weight.BOLD, letter_spacing=int(S(120)))
        header_info = (
            f"{st.channel}  ·  {st.interface}  ·  "
            f"{st.bitrate:,} bit/s  ·  Uptime {fmt_duration(time.time() - st.start_ts)}"
        ).replace(",", ".")
        self.text_center(cr, hx + S(342), hy + S(48), header_info,
                         size=S(14), color=rgba(0.70, 0.86, 1.0, 0.92),
                         weight=Pango.Weight.SEMIBOLD, max_width=S(540),
                         ellipsize=Pango.EllipsizeMode.END)

        pill_y = hy + S(5)
        self.pill(cr, px + pw - S(520), pill_y, S(126), S(36), "CAN online" if st.can_online else "CAN offline",
                  self.GREEN if st.can_online else self.RED, active=st.can_online, icon="bus")
        self.pill(cr, px + pw - S(382), pill_y, S(150), S(36), "Logging aktiv" if st.logging_active else "Logging aus",
                  self.GREEN if st.logging_active else self.RED, active=st.logging_active, icon="dot")
        self.pill(cr, px + pw - S(220), pill_y, S(82), S(36), st.channel, self.CYAN, active=True, icon="dot")
        self.text(cr, px + pw - S(118), hy + S(0), datetime.now().strftime("%d.%m.%Y"), size=S(13), color=self.MUTED)
        self.text(cr, px + pw - S(118), hy + S(22), datetime.now().strftime("%H:%M:%S"), size=S(22), color=self.TEXT)

        # Stats row.
        stats_y = py + S(88)
        tile_gap = S(10)
        tile_w = (pw - S(48) - 5 * tile_gap) / 6
        tiles = [
            ("RX Frames", f"{st.rx_frames:,}".replace(",", "."), "", self.CYAN, "rx"),
            ("TX Frames", f"{st.tx_frames:,}".replace(",", "."), "", self.GREEN, "tx"),
            ("Errors", f"{st.err_frames:,}".replace(",", "."), "", self.RED if st.err_frames else self.MUTED, "dot"),
            ("Unique IDs", str(len(st.unique_ids)), "", self.PURPLE, "id"),
            ("Frame Rate", f"{st.fps:.1f}", "fps", self.BLUE, "fps"),
            ("Data Rate", f"{st.kbps:.2f}", "kB/s", self.AMBER, "size"),
        ]
        for i, (label, value, unit, col, icon) in enumerate(tiles):
            self.stat_tile(cr, px + S(24) + i * (tile_w + tile_gap), stats_y, tile_w, S(76),
                           label, value, unit, col, icon)

        # Main area: table first, last frame underneath. Tuned to fit 1280x800 fullscreen.
        main_x = px + S(24)
        main_y = py + S(176)
        main_w = pw - S(48)
        table_h = S(468)
        last_y = main_y + table_h + S(10)
        last_h = S(58)

        self.draw_recent_table(cr, main_x, main_y, main_w, table_h, st.recent_frames)
        self.draw_last_frame_card(cr, main_x, last_y, main_w, last_h, st.last_frame)

        note_y = last_y + last_h + S(8)
        if st.error_text:
            self.text(cr, main_x + S(6), note_y, f"Hinweis: {st.error_text}", size=S(12),
                      color=rgba(1.0, 0.55, 0.55, 0.92), max_width=main_w - S(12),
                      ellipsize=Pango.EllipsizeMode.END)
        #else:
            #self.text(cr, main_x + S(6), note_y,
                      #f"Log: {st.log_path}  ·  CSV/ASC/JSON aktiv  ·  Groesse {fmt_bytes(st.log_file_size)}",
                      #size=S(11), color=self.MUTED, max_width=main_w - S(12),
                      #ellipsize=Pango.EllipsizeMode.START, family="Monospace")

        self.draw_footer(cr, px, py + ph - S(48), pw, S(48), st)




# -----------------------------------------------------------------------------
# GTK Controls Overlay
# -----------------------------------------------------------------------------


class EmbeddedHexKeyboard(Gtk.Box):
    """Kleine eingebaute Hex-Touch-Tastatur fuer CAN-ID und Datenfeld.

    Keine externen Programme, keine Paketmanager-Abhaengigkeit: ideal fuer Yocto.
    """

    def __init__(self, panel: "ControlPanel"):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.panel = panel
        self.add_css_class("hex-keyboard")
        self.set_visible(True)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.append(header)

        title = Gtk.Label(label="Eingabe-Tastatur")
        title.add_css_class("keyboard-title")
        title.set_xalign(0)
        title.set_hexpand(True)
        header.append(title)

        self.target_label = Gtk.Label(label="Ziel: Daten")
        self.target_label.add_css_class("keyboard-target")
        self.target_label.set_xalign(1)
        header.append(self.target_label)

        target_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.append(target_row)
        self.id_target_btn = Gtk.Button(label="CAN-ID")
        self.id_target_btn.add_css_class("kbd-fn")
        self.id_target_btn.connect("clicked", lambda *_: self.panel.set_active_entry(self.panel.id_entry))
        target_row.append(self.id_target_btn)
        self.data_target_btn = Gtk.Button(label="Daten")
        self.data_target_btn.add_css_class("kbd-fn")
        self.data_target_btn.connect("clicked", lambda *_: self.panel.set_active_entry(self.panel.data_entry))
        target_row.append(self.data_target_btn)
        next_btn = Gtk.Button(label="Weiter")
        next_btn.add_css_class("kbd-fn")
        next_btn.connect("clicked", self._next_target)
        target_row.append(next_btn)

        grid = Gtk.Grid(column_spacing=8, row_spacing=8)
        self.append(grid)
        keys = [
            ["1", "2", "3", "A", "B", "C"],
            ["4", "5", "6", "D", "E", "F"],
            ["7", "8", "9", "0", "SP", "DEL"],
            ["ALL", "0x", "STD", "EXT", "RTR", "SEND"],
        ]
        for r, row in enumerate(keys):
            for c, key in enumerate(row):
                label = {
                    "SP": "Leer",
                    "DEL": "<-",
                    "ALL": "Alles",
                    "STD": "Std",
                    "EXT": "Ext",
                    "RTR": "RTR",
                    "SEND": "Senden",
                }.get(key, key)
                btn = Gtk.Button(label=label)
                btn.add_css_class("kbd-key")
                if key in ("ALL", "DEL", "SP", "0x", "STD", "EXT", "RTR", "SEND"):
                    btn.add_css_class("kbd-fn")
                if key == "SEND":
                    btn.add_css_class("kbd-send")
                # Tastaturtasten sollen den Fokus nicht vom Eingabefeld wegnehmen.
                # Das macht die Touch-Eingabe spürbar direkter und hält die Cursorposition stabil.
                try:
                    btn.set_focus_on_click(False)
                except Exception:
                    pass
                try:
                    btn.set_focusable(False)
                except Exception:
                    pass
                btn.connect("clicked", self._on_key_clicked, key)
                grid.attach(btn, c, r, 1, 1)

    def set_target_text(self, text: str):
        self.target_label.set_text(f"Ziel: {text}")

    def _next_target(self, *_args):
        if self.panel.active_entry is self.panel.id_entry:
            self.panel.set_active_entry(self.panel.data_entry)
        else:
            self.panel.set_active_entry(self.panel.id_entry)

    def _on_key_clicked(self, _button, key: str):
        entry = self.panel.active_entry or self.panel.data_entry
        if key == "SEND":
            self.panel._on_send_clicked(None)
            return
        if key == "STD":
            self.panel.extended_check.set_active(False)
            return
        if key == "EXT":
            self.panel.extended_check.set_active(True)
            return
        if key == "RTR":
            self.panel.remote_check.set_active(not self.panel.remote_check.get_active())
            return
        if key == "ALL":
            entry.set_text("")
            entry.set_position(0)
            return
        if key in ("DEL", "BS"):
            self._delete_previous_char(entry)
            return
        if key == "SP":
            self._insert(entry, " ")
            return
        if key == "0x":
            self._insert(entry, "0x")
            return
        self._insert(entry, key)

    def _selection_bounds(self, entry: Gtk.Entry) -> Optional[tuple[int, int]]:
        """Robust fuer PyGObject/GTK Rueckgabevarianten von get_selection_bounds()."""
        try:
            bounds = entry.get_selection_bounds()
        except Exception:
            return None
        if not bounds:
            return None
        try:
            if len(bounds) == 3:
                ok, start, end = bounds
                if not ok:
                    return None
            elif len(bounds) == 2:
                start, end = bounds
            else:
                return None
            start, end = int(start), int(end)
            if start == end:
                return None
            return (min(start, end), max(start, end))
        except Exception:
            return None

    def _cursor_position(self, entry: Gtk.Entry, current: str) -> int:
        try:
            pos = int(entry.get_position())
        except Exception:
            pos = len(current)
        if pos < 0 or pos > len(current):
            pos = len(current)
        return pos

    def _delete_previous_char(self, entry: Gtk.Entry):
        current = entry.get_text()
        bounds = self._selection_bounds(entry)
        if bounds is not None:
            start, end = bounds
            entry.set_text(current[:start] + current[end:])
            entry.set_position(start)
            return
        pos = self._cursor_position(entry, current)
        if pos <= 0:
            return
        entry.set_text(current[:pos - 1] + current[pos:])
        entry.set_position(pos - 1)

    def _insert(self, entry: Gtk.Entry, text: str):
        current = entry.get_text()
        bounds = self._selection_bounds(entry)
        if bounds is not None:
            start, end = bounds
            entry.set_text(current[:start] + text + current[end:])
            entry.set_position(start + len(text))
            return
        pos = self._cursor_position(entry, current)
        entry.set_text(current[:pos] + text + current[pos:])
        entry.set_position(pos + len(text))


class ControlPanel(Gtk.Box):
    BITRATES = ["125000", "250000", "500000", "1000000"]
    CHANNELS = ["can0", "can1"]

    def __init__(self, app: "CanLoggerApp"):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.app = app
        self.active_entry: Optional[Gtk.Entry] = None
        self.add_css_class("tx-panel")
        self.set_halign(Gtk.Align.END)
        self.set_valign(Gtk.Align.CENTER)
        self.set_margin_top(18)
        self.set_margin_bottom(18)
        self.set_margin_end(18)
        self.compact = False
        self.set_size_request(500, -1)

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.append(head)
        title = Gtk.Label(label="CAN Steuerung")
        title.add_css_class("panel-title")
        title.set_xalign(0)
        title.set_hexpand(True)
        head.append(title)
        self.hide_btn = Gtk.Button(label="–")
        self.hide_btn.add_css_class("mini-button")
        self.hide_btn.set_tooltip_text("Panel ein-/ausklappen")
        self.hide_btn.connect("clicked", self._toggle_compact)
        head.append(self.hide_btn)

        self.subtitle = Gtk.Label(label="Touch-Hex-Tastatur · keine externe OSK nötig")
        self.subtitle.add_css_class("panel-muted")
        self.subtitle.set_xalign(0)
        self.subtitle.set_wrap(True)
        self.append(self.subtitle)

        grid = Gtk.Grid(column_spacing=12, row_spacing=7)
        self.config_grid = grid
        grid.add_css_class("panel-grid")
        self.append(grid)

        ch_lbl = Gtk.Label(label="Channel")
        ch_lbl.set_xalign(0)
        ch_lbl.add_css_class("field-label")
        grid.attach(ch_lbl, 0, 0, 1, 1)
        self.channel_combo = Gtk.ComboBoxText()
        for ch in self.CHANNELS:
            self.channel_combo.append_text(ch)
        try:
            self.channel_combo.set_active(self.CHANNELS.index(app.args.channel))
        except ValueError:
            self.channel_combo.append_text(app.args.channel)
            self.channel_combo.set_active(len(self.CHANNELS))
        grid.attach(self.channel_combo, 1, 0, 1, 1)

        br_lbl = Gtk.Label(label="Bitrate")
        br_lbl.set_xalign(0)
        br_lbl.add_css_class("field-label")
        grid.attach(br_lbl, 0, 1, 1, 1)
        self.bitrate_combo = Gtk.ComboBoxText()
        for br in self.BITRATES:
            self.bitrate_combo.append_text(br)
        current_br = str(int(app.args.bitrate))
        if current_br in self.BITRATES:
            self.bitrate_combo.set_active(self.BITRATES.index(current_br))
        else:
            self.bitrate_combo.append_text(current_br)
            self.bitrate_combo.set_active(len(self.BITRATES))
        grid.attach(self.bitrate_combo, 1, 1, 1, 1)

        self.apply_btn = Gtk.Button(label="Übernehmen")
        self.apply_btn.add_css_class("apply-button")
        self.apply_btn.connect("clicked", self._on_apply_clicked)
        grid.attach(self.apply_btn, 0, 2, 2, 1)

        self.tx_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.append(self.tx_box)

        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        sep.set_margin_top(2)
        sep.set_margin_bottom(1)
        self.tx_box.append(sep)

        tx_title = Gtk.Label(label="Frame senden")
        tx_title.add_css_class("panel-title-small")
        tx_title.set_xalign(0)
        self.tx_box.append(tx_title)

        tx_grid = Gtk.Grid(column_spacing=10, row_spacing=7)
        self.tx_box.append(tx_grid)

        id_lbl = Gtk.Label(label="CAN-ID hex")
        id_lbl.set_xalign(0)
        id_lbl.add_css_class("field-label")
        tx_grid.attach(id_lbl, 0, 0, 1, 1)
        self.id_entry = Gtk.Entry()
        self.id_entry.set_hexpand(True)
        self.id_entry.set_placeholder_text("123 oder 1CEFFF24")
        self.id_entry.set_text("123")
        self._prepare_touch_entry(self.id_entry)
        tx_grid.attach(self.id_entry, 1, 0, 1, 1)

        data_lbl = Gtk.Label(label="Daten")
        data_lbl.set_xalign(0)
        data_lbl.add_css_class("field-label")
        tx_grid.attach(data_lbl, 0, 1, 1, 1)
        self.data_entry = Gtk.Entry()
        self.data_entry.set_hexpand(True)
        self.data_entry.set_placeholder_text("11 22 33 44 oder 11223344")
        self._prepare_touch_entry(self.data_entry)
        tx_grid.attach(self.data_entry, 1, 1, 1, 1)

        opts = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.extended_check = Gtk.CheckButton(label="Extended")
        self.remote_check = Gtk.CheckButton(label="RTR")
        opts.append(self.extended_check)
        opts.append(self.remote_check)
        tx_grid.attach(opts, 0, 2, 2, 1)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.send_btn = Gtk.Button(label="Senden")
        self.send_btn.add_css_class("send-button")
        self.send_btn.set_hexpand(True)
        self.send_btn.connect("clicked", self._on_send_clicked)
        btn_box.append(self.send_btn)

        self.keyboard_btn = Gtk.Button(label="Tastatur aus")
        self.keyboard_btn.add_css_class("keyboard-button")
        self.keyboard_btn.connect("clicked", self._on_keyboard_clicked)
        btn_box.append(self.keyboard_btn)
        tx_grid.attach(btn_box, 0, 3, 2, 1)

        self.keyboard = EmbeddedHexKeyboard(self)
        self.tx_box.append(self.keyboard)

        self.status_label = Gtk.Label(label="Bereit")
        self.status_label.add_css_class("panel-status")
        self.status_label.set_xalign(0)
        self.status_label.set_wrap(True)
        self.status_label.set_margin_top(2)
        self.append(self.status_label)

        self.hint = Gtk.Label(label="Config: ip link set <canX> down  ·  ip link set <canX> up type can bitrate <baud> restart-ms 100")
        self.hint.add_css_class("panel-muted-mono")
        self.hint.set_xalign(0)
        self.hint.set_wrap(True)
        self.append(self.hint)

        self.set_active_entry(self.data_entry)

    def set_active_entry(self, entry: Gtk.Entry):
        self.active_entry = entry
        try:
            entry.grab_focus()
        except Exception:
            pass
        if entry is self.id_entry:
            self.keyboard.set_target_text("CAN-ID")
            self.id_entry.add_css_class("active-entry")
            self.data_entry.remove_css_class("active-entry")
        else:
            self.keyboard.set_target_text("Daten")
            self.data_entry.add_css_class("active-entry")
            self.id_entry.remove_css_class("active-entry")

    def _prepare_touch_entry(self, entry: Gtk.Entry):
        """Macht Eingabefelder touchfreundlich und bindet die interne Hex-Tastatur an."""
        try:
            entry.set_input_purpose(Gtk.InputPurpose.FREE_FORM)
        except Exception:
            pass
        try:
            entry.set_input_hints(Gtk.InputHints.NO_SPELLCHECK | Gtk.InputHints.UPPERCASE_CHARS)
        except Exception:
            pass
        try:
            focus = Gtk.EventControllerFocus()
            focus.connect("enter", lambda *_: self.set_active_entry(entry))
            entry.add_controller(focus)
        except Exception:
            pass

    def _on_keyboard_clicked(self, _button):
        visible = not self.keyboard.get_visible()
        self.keyboard.set_visible(visible)
        self.keyboard_btn.set_label("Tastatur aus" if visible else "Tastatur an")
        self.status_label.set_text("Interne Hex-Tastatur aktiv" if visible else "Interne Hex-Tastatur ausgeblendet")
        self.status_label.remove_css_class("bad")
        self.status_label.add_css_class("good")

    def _toggle_compact(self, _button):
        self.compact = not self.compact
        compact = self.compact
        # Im Minus-Modus bleibt nur eine sehr kleine Griffleiste sichtbar.
        self.subtitle.set_visible(not compact)
        self.config_grid.set_visible(not compact)
        self.tx_box.set_visible(not compact)
        self.status_label.set_visible(not compact)
        self.hint.set_visible(not compact)
        self.hide_btn.set_label("+" if compact else "–")
        if compact:
            self.add_css_class("compact")
            self.set_size_request(176, -1)
            # Eingeklappt soll die Griffleiste wieder unten rechts sitzen.
            self.set_valign(Gtk.Align.END)
            self.set_margin_top(18)
            self.set_margin_bottom(58)
            self.set_margin_end(18)
            self.status_label.set_text("Panel minimiert")
        else:
            self.remove_css_class("compact")
            self.set_size_request(500, -1)
            # Aufgeklappt bleibt das Panel mittig, damit Kopf und Tastatur sichtbar bleiben.
            self.set_valign(Gtk.Align.CENTER)
            self.set_margin_top(18)
            self.set_margin_bottom(18)
            self.set_margin_end(18)

    def _on_apply_clicked(self, _button):
        channel = self.channel_combo.get_active_text() or "can0"
        bitrate_text = self.bitrate_combo.get_active_text() or "500000"
        ok, msg = self.app.apply_can_config(channel, bitrate_text)
        self.status_label.set_text(msg)
        self.status_label.remove_css_class("bad")
        self.status_label.remove_css_class("good")
        self.status_label.add_css_class("good" if ok else "bad")

    def _on_send_clicked(self, _button):
        ok, msg = self.app.send_can_frame(
            self.id_entry.get_text(),
            self.data_entry.get_text(),
            self.extended_check.get_active(),
            self.remote_check.get_active(),
        )
        self.status_label.set_text(msg)
        self.status_label.remove_css_class("bad")
        self.status_label.remove_css_class("good")
        self.status_label.add_css_class("good" if ok else "bad")


# -----------------------------------------------------------------------------
# GTK App
# -----------------------------------------------------------------------------


class CanLoggerApp(Gtk.Application):
    def __init__(self, args):
        super().__init__(application_id=APP_ID)
        self.args = args
        log_dir = ensure_dir(args.log_dir)
        self.state = LoggerState(channel=args.channel, interface=args.interface, bitrate=args.bitrate, log_dir=log_dir)
        self.logger = MultiFormatCanLogger(log_dir, args.max_bytes, args.backups)
        with self.state.lock:
            self.state.log_path = self.logger.log_path
            self.state.csv_path = self.logger.csv_path
            self.state.asc_path = self.logger.asc_path
            self.state.stats_path = self.logger.stats_path
            self.state.log_dir = self.logger.log_dir
            self.state.start_ts = self.logger.start_ts
            self.state.restart_ms = int(args.restart_ms)
        self.dashboard: Optional[CanLoggerDashboard] = None
        self.control_panel: Optional[ControlPanel] = None
        self.worker: Optional[CanLoggerThread] = None
        self.web_server: Optional[CanWebServerThread] = None
        self._ui_timer = None

    def do_activate(self):
        win = Gtk.ApplicationWindow(application=self)
        win.set_title("OCIP CAN Logger")
        win.set_default_size(1920, 1080)
        if not self.args.windowed:
            try:
                win.set_decorated(False)
            except Exception:
                pass
            win.fullscreen()
        else:
            win.set_resizable(True)

        self._install_css()
        self.dashboard = CanLoggerDashboard(self.state)
        overlay = Gtk.Overlay()
        overlay.set_child(self.dashboard)
        self.control_panel = ControlPanel(self)
        overlay.add_overlay(self.control_panel)
        win.set_child(overlay)
        win.present()

        try:
            filters = parse_can_filters(self.args.filter) if self.args.filter else None
        except Exception as exc:
            filters = None
            with self.state.lock:
                self.state.error_text = f"Filter ignoriert: {exc}"

        self.worker = CanLoggerThread(self.state, self.logger, configure_can=self.args.configure_can, filters=filters, restart_ms=self.args.restart_ms)
        self.worker.start()

        if not self.args.no_web:
            self.web_server = CanWebServerThread(
                self.state,
                self.args.web_host,
                self.args.web_port,
                self.send_can_frame,
                self.apply_can_config,
            )
            self.web_server.start()
            with self.state.lock:
                self.state.config_text = f"Webapp aktiv: http://{self.args.web_host}:{self.args.web_port}/"

        self._ui_timer = GLib.timeout_add(UI_REFRESH_MS, self._refresh_ui)

    def _install_css(self):
        css = b"""
        .tx-panel {
            background: rgba(3, 12, 22, 0.90);
            border: 1px solid rgba(34, 190, 255, 0.52);
            border-radius: 22px;
            padding: 10px;
            box-shadow: 0 0 28px rgba(0, 174, 255, 0.22);
        }
        .tx-panel.compact {
            background: rgba(3, 12, 22, 0.74);
            border: 1px solid rgba(34, 190, 255, 0.38);
            padding: 8px;
            border-radius: 16px;
        }
        .panel-title {
            color: #eef7ff;
            font-size: 18px;
            font-weight: 800;
        }
        .panel-title-small {
            color: #eef7ff;
            font-size: 16px;
            font-weight: 750;
        }
        .panel-muted {
            color: rgba(190, 210, 225, 0.82);
            font-size: 12px;
        }
        .panel-muted-mono {
            color: rgba(130, 170, 195, 0.86);
            font-family: monospace;
            font-size: 11px;
        }
        .field-label {
            color: rgba(208, 226, 240, 0.88);
            font-weight: 700;
        }
        .panel-status {
            color: rgba(210, 232, 244, 0.95);
            font-family: monospace;
            font-size: 12px;
        }
        .panel-status.good { color: #35f07a; }
        .panel-status.bad { color: #ff6b6b; }
        entry, combobox, combobox button {
            background: rgba(8, 24, 40, 0.92);
            color: #eef7ff;
            border: 1px solid rgba(34, 190, 255, 0.35);
            border-radius: 10px;
            min-height: 32px;
            font-size: 15px;
        }
        checkbutton { color: rgba(226, 239, 250, 0.92); }
        button.apply-button {
            background: rgba(25, 120, 255, 0.32);
            color: #eef7ff;
            border: 1px solid rgba(74, 170, 255, 0.72);
            border-radius: 12px;
            min-height: 36px;
            font-weight: 800;
        }
        button.send-button {
            background: rgba(23, 220, 95, 0.28);
            color: #eef7ff;
            border: 1px solid rgba(35, 240, 115, 0.70);
            border-radius: 12px;
            min-height: 34px;
            font-weight: 900;
        }
        button.keyboard-button {
            background: rgba(34, 190, 255, 0.20);
            color: #eef7ff;
            border: 1px solid rgba(34, 190, 255, 0.58);
            border-radius: 12px;
            min-height: 38px;
            font-weight: 800;
        }
        entry.active-entry {
            border: 2px solid rgba(53, 240, 122, 0.88);
            box-shadow: 0 0 12px rgba(53, 240, 122, 0.20);
        }
        .hex-keyboard {
            background: rgba(5, 18, 30, 0.72);
            border: 1px solid rgba(34, 190, 255, 0.25);
            border-radius: 16px;
            padding: 8px;
        }
        .keyboard-title {
            color: #eef7ff;
            font-size: 15px;
            font-weight: 800;
        }
        .keyboard-target {
            color: #35f07a;
            font-size: 13px;
            font-weight: 800;
        }
        button.kbd-key {
            background: rgba(12, 34, 54, 0.95);
            color: #eef7ff;
            border: 1px solid rgba(34, 190, 255, 0.34);
            border-radius: 12px;
            min-height: 40px;
            min-width: 64px;
            font-size: 16px;
            font-weight: 900;
        }
        button.kbd-fn {
            background: rgba(25, 74, 105, 0.92);
            color: #d9f3ff;
            font-size: 14px;
        }
        button.kbd-send {
            background: rgba(23, 220, 95, 0.30);
            border: 1px solid rgba(35, 240, 115, 0.72);
            color: #f2fff6;
            font-size: 15px;
        }
        button.mini-button {
            background: rgba(34, 190, 255, 0.16);
            color: #eef7ff;
            border: 1px solid rgba(34, 190, 255, 0.38);
            border-radius: 10px;
            min-height: 32px;
            min-width: 38px;
            font-weight: 900;
        }
        """
        try:
            provider = Gtk.CssProvider()
            try:
                provider.load_from_data(css)
            except TypeError:
                provider.load_from_data(css.decode("utf-8"))
            display = Gdk.Display.get_default()
            if display is not None:
                Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        except Exception:
            pass

    def apply_can_config(self, channel: str, bitrate_text: str) -> tuple[bool, str]:
        try:
            bitrate = int(str(bitrate_text).strip())
            if channel not in ("can0", "can1"):
                return False, "Nur can0 oder can1 sind in der UI vorgesehen"
            if self.worker is None:
                return False, "Worker läuft noch nicht"
            self.worker.request_reconfigure(channel, bitrate)
            msg = f"Angewendet: {channel} down/up · bitrate {bitrate} · restart-ms {self.args.restart_ms}"
            with self.state.lock:
                self.state.config_text = msg
            return True, msg
        except Exception as exc:
            return False, f"Config Fehler: {exc}"

    def send_can_frame(self, id_text: str, data_text: str, extended: bool, remote: bool) -> tuple[bool, str]:
        try:
            can_id, inferred_extended = parse_can_id(id_text)
            data = parse_can_data(data_text)
            use_extended = bool(extended or inferred_extended)
            if self.worker is None:
                return False, "Worker läuft noch nicht"
            return self.worker.send_frame(can_id, data, extended=use_extended, remote=remote)
        except Exception as exc:
            msg = f"TX Eingabe Fehler: {exc}"
            with self.state.lock:
                self.state.send_text = msg
                self.state.error_text = msg
            return False, msg

    def _refresh_ui(self):
        if self.dashboard:
            self.dashboard.queue_draw()
        return True

    def do_shutdown(self):
        if self.web_server:
            self.web_server.stop()
        if self.worker:
            self.worker.stop()
        try:
            self.logger.write_stats(self.state)
            self.logger.close()
        except Exception:
            pass
        return Gtk.Application.do_shutdown(self)


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------


def build_arg_parser():
    p = argparse.ArgumentParser(description="OCIP CAN Logger - GTK4 SocketCAN Logger mit Live-Visualisierung")
    p.add_argument("--channel", default=DEFAULT_CHANNEL, help="CAN Channel, z. B. can0")
    p.add_argument("--interface", default=DEFAULT_INTERFACE, help="python-can Interface, z. B. socketcan")
    p.add_argument("--bitrate", type=int, default=DEFAULT_BITRATE, help="Bitrate fuer optionales Interface-Setup")
    p.add_argument("--configure-can", action="store_true", help="CAN Interface per ip link beim Start konfigurieren")
    p.add_argument("--restart-ms", type=int, default=100, help="SocketCAN restart-ms Wert fuer ip link setup")
    p.add_argument("--log-dir", default=DEFAULT_LOG_DIR, help="Zielordner fuer Logdateien, Standard: ~/OCIP/CAN_Logger/logs")
    p.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_LOG_BYTES, help="Maximale Groesse je Logdatei vor Rotation")
    p.add_argument("--backups", type=int, default=DEFAULT_BACKUPS, help="Anzahl rotierter Backups")
    p.add_argument("--filter", default="", help="CAN Filter, z. B. 123:7FF,1CEFFF24:1FFFFFFF")
    p.add_argument("--windowed", action="store_true", help="Fenster statt Vollbild/Kiosk; ohne diese Option startet OCIP CAN Logger im Fullscreen")
    p.add_argument("--web-host", default=DEFAULT_WEB_HOST, help="Webapp Host, z. B. 0.0.0.0 oder 127.0.0.1")
    p.add_argument("--web-port", type=int, default=DEFAULT_WEB_PORT, help="Webapp Port")
    p.add_argument("--no-web", action="store_true", help="Parallele Webapp deaktivieren")
    return p


def main():
    args = build_arg_parser().parse_args()
    app = CanLoggerApp(args)
    raise SystemExit(app.run())


if __name__ == "__main__":
    main()
