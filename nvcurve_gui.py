#!/usr/bin/env python3
"""
nvcurve-gui — standalone NVIDIA V/F curve tuning GUI
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
A single-file PySide6 front-end for the `nvcurve` CLI/HAL package.
Lets you read the GPU's current voltage/frequency curve, drag points
(or apply a global core offset), lock/unlock VRAM clocks, and save/
load/autoload named profiles — all without touching cTGP/nvctgp.

Privileged operations (reading NVAPI/NVML state as root, writing
/etc/nvcurve/profiles, applying curves) are delegated to root_helper.py
via `pkexec`, exactly like the parent m16R1-power-manager project.
This file is the GPU-tuning tab of that project, extracted to stand
on its own with no cTGP / gaming-tunable / CPU-isolation code at all.

Requires: PySide6, PySide6-Charts (QtCharts), a polkit-capable system,
and root_helper.py + the nvcurve/ package installed alongside this
file (see install.sh).
"""

import fcntl
import json
import os
import stat
import subprocess
import sys
import tempfile
import time

from PySide6.QtCore import Qt, QTimer, Signal, QThread, QPointF, QMargins
from PySide6.QtGui import QPainter, QColor, QFont, QBrush, QKeyEvent, QPalette, QPen, QIcon, QAction
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGroupBox, QLabel, QPushButton, QComboBox, QSpinBox, QSlider,
    QSizePolicy, QMessageBox, QInputDialog, QCheckBox,
    QSystemTrayIcon, QMenu,
)
from PySide6.QtCharts import QChart, QChartView, QLineSeries, QScatterSeries, QValueAxis
from PySide6.QtNetwork import QLocalServer, QLocalSocket

# ─── Theme ───────────────────────────────────────────────────────────────
C_BG = "#2b2420"
C_BG3 = "#3d332c"
C_BORDER = "#5a4c3f"
C_VDGREY = "#7a6a58"
C_GREY = "#bfae98"
C_LIGHT = "#f0e6d2"
C_WHITE = "#faf3e3"
C_CYAN = "#a4ccbc"
C_ORANGE = "#ddae88"
C_YELLOW = "#e3cfa0"
C_GREEN = "#cbcd84"
C_STOP = "#db9b94"

def apply_theme(app):
    """Force the warm Gruvbox theme app-wide.

    A window-level setStyleSheet("background:..;color:..") does NOT cascade
    to child widgets driven by the OS/KDE palette (labels, group boxes,
    tabs, buttons, spin boxes, the chart) — that is why most of the UI
    stayed monochrome. Setting a full QPalette plus a scoped QSS on the
    QApplication themes every widget from one place.
    """
    app.setStyle("Fusion")

    pal = QPalette()
    pal.setColor(QPalette.Window, QColor(C_BG))
    pal.setColor(QPalette.WindowText, QColor(C_LIGHT))
    pal.setColor(QPalette.Base, QColor(C_BG3))
    pal.setColor(QPalette.AlternateBase, QColor(C_BORDER))
    pal.setColor(QPalette.ToolTipBase, QColor(C_BG3))
    pal.setColor(QPalette.ToolTipText, QColor(C_LIGHT))
    pal.setColor(QPalette.Text, QColor(C_LIGHT))
    pal.setColor(QPalette.Button, QColor(C_BG3))
    pal.setColor(QPalette.ButtonText, QColor(C_LIGHT))
    pal.setColor(QPalette.BrightText, QColor(C_STOP))
    pal.setColor(QPalette.Link, QColor(C_CYAN))
    pal.setColor(QPalette.Highlight, QColor(C_ORANGE))
    pal.setColor(QPalette.HighlightedText, QColor(C_BG))
    pal.setColor(QPalette.PlaceholderText, QColor(C_GREY))
    dis = QPalette.Disabled
    pal.setColor(dis, QPalette.WindowText, QColor(C_VDGREY))
    pal.setColor(dis, QPalette.Text, QColor(C_VDGREY))
    pal.setColor(dis, QPalette.ButtonText, QColor(C_VDGREY))
    app.setPalette(pal)

    app.setStyleSheet(f"""
        QWidget {{ background:{C_BG}; color:{C_LIGHT}; }}
        QGroupBox {{
            border:1px solid {C_BORDER}; border-radius:6px;
            margin-top:9px; padding-top:9px; background:{C_BG};
        }}
        QGroupBox::title {{
            subcontrol-origin:margin; subcontrol-position:top left;
            left:8px; padding:1px 6px; color:{C_ORANGE}; font-weight:bold;
            background:{C_BG3}; border:1px solid {C_BORDER};
            border-radius:4px;
        }}
        QLabel {{ background:transparent; color:{C_LIGHT}; }}
        QCheckBox {{ background:transparent; color:{C_LIGHT}; spacing:6px; }}
        QCheckBox::indicator {{
            width:15px; height:15px; border-radius:4px;
            border:1px solid {C_BORDER}; background:{C_BG3};
        }}
        QCheckBox::indicator:hover {{ border-color:{C_ORANGE}; }}
        QCheckBox::indicator:checked {{
            background:{C_GREEN}; border-color:{C_GREEN};
        }}
        QPushButton {{
            background:{C_BG3}; color:{C_LIGHT};
            border:1px solid {C_BORDER}; border-radius:5px;
            padding:5px 11px; font-weight:600;
        }}
        QPushButton:hover {{
            background:{C_BORDER}; border-color:{C_ORANGE}; color:{C_WHITE};
        }}
        QPushButton:pressed {{ background:{C_VDGREY}; }}
        QPushButton:disabled {{
            background:{C_BG}; color:{C_VDGREY}; border-color:{C_BG3};
        }}

        /* Primary actions — read / save / select (cyan) */
        QPushButton#btnPrimary {{
            border-color:{C_CYAN}; color:{C_CYAN};
        }}
        QPushButton#btnPrimary:hover {{
            background:{C_CYAN}; color:{C_BG}; border-color:{C_CYAN};
        }}

        /* Apply — the main commit action (green, filled) */
        QPushButton#btnApply {{
            background:{C_GREEN}; color:{C_BG};
            border:1px solid {C_GREEN}; font-weight:700;
        }}
        QPushButton#btnApply:hover {{
            background:{C_LIGHT}; border-color:{C_GREEN}; color:{C_BG};
        }}
        QPushButton#btnApply:pressed {{ background:{C_GREEN}; }}

        /* Accent — default / lock (yellow-orange) */
        QPushButton#btnAccent {{
            border-color:{C_YELLOW}; color:{C_YELLOW};
        }}
        QPushButton#btnAccent:hover {{
            background:{C_YELLOW}; color:{C_BG}; border-color:{C_YELLOW};
        }}

        /* Destructive — delete / reset / unlock (red) */
        QPushButton#btnDanger {{
            border-color:{C_STOP}; color:{C_STOP};
        }}
        QPushButton#btnDanger:hover {{
            background:{C_STOP}; color:{C_BG}; border-color:{C_STOP};
        }}

        QComboBox, QSpinBox, QLineEdit {{
            background:{C_BG3}; color:{C_LIGHT};
            border:1px solid {C_BORDER}; border-radius:5px;
            padding:3px 5px; selection-background-color:{C_ORANGE};
            selection-color:{C_BG};
        }}
        QComboBox:hover, QSpinBox:hover, QLineEdit:hover {{
            border-color:{C_ORANGE};
        }}
        QComboBox:focus, QSpinBox:focus, QLineEdit:focus {{
            border-color:{C_YELLOW}; background:{C_BG};
        }}
        QComboBox::drop-down {{ border:0; width:18px; }}
        QComboBox::down-arrow {{ width:0; height:0; }}
        QComboBox QAbstractItemView {{
            background:{C_BG3}; color:{C_LIGHT};
            selection-background-color:{C_ORANGE}; selection-color:{C_BG};
            border:1px solid {C_BORDER}; outline:0;
        }}
        QSpinBox::up-button, QSpinBox::down-button {{
            background:{C_BORDER}; border:0; width:15px;
        }}
        QSpinBox::up-button:hover, QSpinBox::down-button:hover {{
            background:{C_ORANGE};
        }}
        QSpinBox::up-arrow {{
            width:0; height:0; border-left:4px solid transparent;
            border-right:4px solid transparent; border-bottom:5px solid {C_LIGHT};
        }}
        QSpinBox::down-arrow {{
            width:0; height:0; border-left:4px solid transparent;
            border-right:4px solid transparent; border-top:5px solid {C_LIGHT};
        }}
        QSlider::groove:horizontal {{
            height:5px; background:{C_BG3}; border:1px solid {C_BORDER};
            border-radius:3px;
        }}
        QSlider::sub-page:horizontal {{
            background:{C_ORANGE}; border-radius:3px;
        }}
        QSlider::handle:horizontal {{
            background:{C_YELLOW}; width:14px; margin:-6px 0;
            border-radius:7px; border:1px solid {C_BORDER};
        }}
        QSlider::handle:horizontal:hover {{ background:{C_WHITE}; }}
        QToolTip {{
            background:{C_BG3}; color:{C_LIGHT}; border:1px solid {C_ORANGE};
        }}
        QScrollBar:vertical {{ background:{C_BG}; width:12px; }}
        QScrollBar::handle:vertical {{
            background:{C_VDGREY}; border-radius:6px; min-height:20px;
        }}
        QScrollBar::handle:vertical:hover {{ background:{C_ORANGE}; }}
        QScrollBar::add-line, QScrollBar::sub-line {{ height:0; }}
    """)


ROOT_HELPER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "root_helper.py")


def _root_helper_is_trusted(path: str) -> tuple[bool, str]:
    """Check that the helper we're about to run as root can't have been tampered with.

    ROOT_HELPER_PATH is derived from __file__, so running nvcurve_gui.py out
    of a source checkout points it at a copy inside the user's own home
    directory. pkexec will happily execute that as root once authorized — it
    does not require the target to be root-owned. The installed polkit rule
    is pinned to the installed path so a checkout would at least prompt for a
    password rather than run passwordless, but "prompt, then run a
    user-writable script as root" is still a bad outcome worth refusing.

    Returns (trusted, reason).
    """
    try:
        st = os.stat(path)
    except OSError as exc:
        return False, f"cannot stat {path}: {exc}"
    if st.st_uid != 0:
        return False, (
            f"{path} is not owned by root (uid {st.st_uid}). Refusing to run it "
            "as root — install with ./install.sh and launch via nvcurve-gui."
        )
    if st.st_mode & stat.S_IWGRP or st.st_mode & stat.S_IWOTH:
        return False, (
            f"{path} is group- or world-writable (mode {st.st_mode & 0o777:04o}). "
            "Refusing to run it as root."
        )
    # A root-owned, root-only-writable file inside a directory someone else
    # can write to is not actually protected: write permission on a
    # directory is permission to unlink an entry and put a different file
    # at that name. Walk up to / and require every component to be
    # root-owned and not group/world-writable.
    parent = os.path.dirname(os.path.abspath(path))
    while True:
        try:
            dst = os.stat(parent)
        except OSError as exc:
            return False, f"cannot stat {parent}: {exc}"
        if dst.st_uid != 0:
            return False, (
                f"{parent} is not owned by root (uid {dst.st_uid}) — a file "
                f"inside it can be replaced. Refusing to run {path} as root."
            )
        if (dst.st_mode & stat.S_IWGRP or dst.st_mode & stat.S_IWOTH) and not (
            dst.st_mode & stat.S_ISVTX
        ):
            return False, (
                f"{parent} is group- or world-writable "
                f"(mode {dst.st_mode & 0o777:04o}) — a file inside it can be "
                f"replaced. Refusing to run {path} as root."
            )
        nxt = os.path.dirname(parent)
        if nxt == parent:
            break
        parent = nxt
    return True, ""
NVCURVE_PROFILES_DIR = "/etc/nvcurve/profiles"
NVCURVE_CONFIG_PATH = "/etc/nvcurve/config.json"
RUN_DIR = "/run/nvcurve-gui"

# Internal scratch profile used by "Apply Offsets" to persist+apply the
# live-edited graph without polluting the user's named-profile list.
# (Replaces the old hardcoded "ppm" name.) It is hidden from the profile
# combo and the tray menu.
SCRATCH_PROFILE = "_live"

# Freedesktop autostart entry — when enabled, the app relaunches on login
# (with --tray so it starts hidden in the tray and auto-applies the
# default profile).
AUTOSTART_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "autostart",
)
AUTOSTART_FILE = os.path.join(AUTOSTART_DIR, "nvcurve-gui.desktop")

# Minimum gap between tray-triggered profile applies, to stop rapid-fire
# clicking from queueing many privileged operations back to back.
TRAY_APPLY_COOLDOWN_MS = 3000

# The tray/status-notifier host isn't always ready the instant this
# process starts (notably right after login via --tray autostart, before
# the panel has finished registering). Retry isSystemTrayAvailable() for
# up to ~6s (15 * 400ms) before permanently giving up on the tray.
TRAY_SETUP_RETRY_MS = 400
TRAY_SETUP_MAX_RETRIES = 15

# Single-instance guard.
#
# Acquisition uses a plain OS-level flock() on a lock file — no Qt
# involved at all — so it can run before QApplication even exists and
# can never interfere with the platform plugin's native setup (X11/
# Wayland tray & status-notifier registration happens very early and is
# sensitive to what else touches native sockets/event processing at
# that moment). Only *after* the window and tray are fully set up do we
# open a QLocalServer, purely so later launches have something to ping
# to raise the existing window — that IPC channel is never involved in
# deciding who the primary instance is.
SINGLE_INSTANCE_KEY = "nvcurve-gui-single-instance"


def _single_instance_lock_dir() -> str:
    """Pick a per-user, non-world-writable directory for the lock file.

    The old fallback was tempfile.gettempdir() — i.e. /tmp/nvcurve-gui.lock,
    a fixed name in a world-writable directory. Any other local account could
    pre-create or hold that path to keep this GUI from ever starting, or
    point it somewhere unexpected via a symlink. XDG_RUNTIME_DIR is 0700 and
    correct when present; ~/.cache is the right fallback when it isn't.
    """
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir and os.path.isdir(runtime_dir):
        return runtime_dir
    cache_dir = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    target = os.path.join(cache_dir, "nvcurve-gui")
    try:
        os.makedirs(target, mode=0o700, exist_ok=True)
        return target
    except OSError:
        return tempfile.gettempdir()


SINGLE_INSTANCE_LOCK_PATH = os.path.join(
    _single_instance_lock_dir(), "nvcurve-gui.lock"
)


class SingleInstanceGuard:
    """Ensures only one nvcurve-gui process runs per user session.

    try_acquire() is pure OS-level file locking (fcntl.flock), safe to
    call before QApplication is constructed. start_ipc_server() opens
    the QLocalServer used to let later launches ping this one to raise
    its window — call it only once the window/tray are already set up.
    """

    def __init__(self, key=SINGLE_INSTANCE_KEY, lock_path=SINGLE_INSTANCE_LOCK_PATH):
        self._key = key
        self._lock_path = lock_path
        self._lock_file = None
        self.server = None

    def try_acquire(self):
        """Returns True if this process is the primary instance. Holds
        the lock file open (and thus locked) for the life of the
        process; the OS releases it automatically on exit or crash, so
        there's no stale-lock cleanup to worry about."""
        try:
            self._lock_file = open(self._lock_path, "w")
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if self._lock_file is not None:
                self._lock_file.close()
                self._lock_file = None
            return False
        return True

    def start_ipc_server(self):
        """Open the "raise my window" IPC channel. Call only after the
        window and tray are fully initialized (or have given up trying),
        so this never runs during the timing-sensitive early startup
        window."""
        QLocalServer.removeServer(self._key)
        server = QLocalServer()
        server.setSocketOptions(QLocalServer.UserAccessOption)
        if server.listen(self._key):
            self.server = server
        else:
            print(
                f"nvcurve-gui: warning: could not start single-instance "
                f"IPC server ({server.errorString()}); a second launch "
                f"won't be able to raise this window.",
                file=sys.stderr,
            )

    def notify_running_instance(self):
        """Ping the primary instance to ask it to raise its window."""
        sock = QLocalSocket()
        sock.connectToServer(self._key)
        if sock.waitForConnected(500):
            sock.write(b"raise")
            sock.flush()
            sock.waitForBytesWritten(500)
            sock.disconnectFromServer()


def SL(text, color=C_LIGHT, bold=False, size=9):
    lbl = QLabel(text)
    weight = "bold" if bold else "normal"
    lbl.setStyleSheet(f"color:{color}; font-weight:{weight}; font-size:{size}pt;")
    return lbl


# ═══════════════════════════════════════════════════════════════════════
# V/F Curve chart widget
# ═══════════════════════════════════════════════════════════════════════
class VFCurveWidget(QChartView):
    pointClicked = Signal(int, int)
    pointDragged = Signal(int, int)
    pointReleased = Signal(int, int)
    selectionChanged = Signal()

    def __init__(self, points=None, parent=None):
        super().__init__(parent)
        self.setRenderHint(QPainter.Antialiasing)
        self._group_drag = False
        self._group_drag_start_y = 0
        self.setFocusPolicy(Qt.StrongFocus)
        self.base_points = [(700 + i * 4, 1500 + i * 2) for i in range(127)]
        self.points = points if points else self.base_points.copy()
        self.base_freqs = [f for _, f in self.points]
        self.selected_indices = set()
        self.current_index = 0
        self.dragging_index = -1
        self.chart = QChart()
        self.chart.setAnimationOptions(QChart.SeriesAnimations)
        self.chart.legend().hide()
        self.chart.setBackgroundBrush(QColor(C_BG))
        self.chart.setBackgroundVisible(True)
        self.chart.setPlotAreaBackgroundBrush(QColor("#241e1a"))
        self.chart.setPlotAreaBackgroundVisible(True)
        self.chart.setMargins(QMargins(4, 4, 4, 4))
        self.setChart(self.chart)
        self._create_series()
        self.setInteractive(True)
        self.setMinimumHeight(300)

    def _create_series(self):
        self.chart.removeAllSeries()
        self.line_series = QLineSeries()
        line_pen = QPen(QColor(C_ORANGE))
        line_pen.setWidthF(2.4)
        line_pen.setCosmetic(True)
        self.line_series.setPen(line_pen)
        for v, f in self.points:
            self.line_series.append(v, f)
        self.chart.addSeries(self.line_series)

        self.scatter_series = QScatterSeries()
        self.scatter_series.setMarkerSize(8)
        self.scatter_series.setColor(QColor(C_CYAN))
        sc_pen = QPen(QColor(C_BG))
        sc_pen.setWidthF(1.2)
        self.scatter_series.setPen(sc_pen)
        self.scatter_series.setBorderColor(QColor(C_BG))
        self.scatter_series.clicked.connect(self._on_point_clicked)
        for v, f in self.points:
            self.scatter_series.append(v, f)
        self.chart.addSeries(self.scatter_series)

        self.selected_series = QScatterSeries()
        self.selected_series.setMarkerSize(12)
        self.selected_series.setColor(QColor(C_YELLOW))
        sel_pen = QPen(QColor(C_STOP))
        sel_pen.setWidthF(2.0)
        self.selected_series.setPen(sel_pen)
        self.selected_series.setBorderColor(QColor(C_STOP))
        self.selected_series.clicked.connect(self._on_point_clicked)
        for i in self.selected_indices:
            if i < len(self.points):
                v, f = self.points[i]
                self.selected_series.append(v, f)
        self.chart.addSeries(self.selected_series)

        self.axis_x = QValueAxis()
        self.axis_x.setTitleText("Voltage (mV)")
        self.axis_x.setRange(600, 1200)
        self.axis_x.setTickCount(10)
        self.axis_x.setLabelsColor(QColor(C_GREY))
        self.axis_x.setTitleBrush(QBrush(QColor(C_YELLOW)))
        self.axis_x.setLinePenColor(QColor(C_BORDER))
        grid_pen_x = QPen(QColor(C_BG3))
        grid_pen_x.setWidthF(1.0)
        self.axis_x.setGridLinePen(grid_pen_x)
        self.chart.addAxis(self.axis_x, Qt.AlignBottom)
        self.line_series.attachAxis(self.axis_x)
        self.scatter_series.attachAxis(self.axis_x)
        self.selected_series.attachAxis(self.axis_x)

        self.axis_y = QValueAxis()
        self.axis_y.setTitleText("Frequency (MHz)")
        self.axis_y.setRange(800, 3000)
        self.axis_y.setTickCount(12)
        self.axis_y.setLabelsColor(QColor(C_GREY))
        self.axis_y.setTitleBrush(QBrush(QColor(C_YELLOW)))
        self.axis_y.setLinePenColor(QColor(C_BORDER))
        grid_pen_y = QPen(QColor(C_BG3))
        grid_pen_y.setWidthF(1.0)
        self.axis_y.setGridLinePen(grid_pen_y)
        self.chart.addAxis(self.axis_y, Qt.AlignLeft)
        self.line_series.attachAxis(self.axis_y)
        self.scatter_series.attachAxis(self.axis_y)
        self.selected_series.attachAxis(self.axis_y)
        font = QFont()
        font.setPointSize(7)
        self.axis_y.setLabelsFont(font)
        self.axis_x.setLabelFormat("%.0f")
        self.axis_y.setLabelFormat("%.0f")

    def _on_point_clicked(self, point):
        for i, (v, f) in enumerate(self.points):
            if abs(v - point.x()) < 1 and abs(f - point.y()) < 1:
                self.selected_indices.clear()
                self.selected_indices.add(i)
                self.current_index = i
                self._update_series()
                self.pointClicked.emit(i, int(point.y()))
                self.selectionChanged.emit()
                break

    def set_points(self, points, base_freqs=None):
        self.points = points
        self.base_freqs = base_freqs if base_freqs else [f for _, f in points]
        self.selected_indices.clear()
        self.current_index = 0
        self._update_series()

    def update_points(self, points):
        self.points = points
        self._update_series()

    def set_point_freq(self, index, new_freq):
        v, _ = self.points[index]
        self.points[index] = (v, int(new_freq))
        self._update_series()

    def get_points(self):
        return self.points

    def get_offset(self, index):
        if index < len(self.points) and index < len(self.base_freqs):
            return self.points[index][1] - self.base_freqs[index]
        return 0

    def reset_to_base(self):
        self.set_points(self.base_points.copy(), self.base_freqs.copy())

    def _update_series(self):
        sel_pts = [QPointF(self.points[i][0], self.points[i][1])
                   for i in self.selected_indices if i < len(self.points)]
        all_pts = [QPointF(v, f) for v, f in self.points]
        self.selected_series.replace(sel_pts)
        self.scatter_series.replace(all_pts)
        self.line_series.replace(all_pts)
        self.selectionChanged.emit()

    def select_next(self):
        if not self.points:
            return
        self.current_index = (self.current_index + 1) % len(self.points)
        self.selected_indices.add(self.current_index)
        self._update_series()

    def select_prev(self):
        if not self.points:
            return
        self.current_index = (self.current_index - 1) % len(self.points)
        self.selected_indices.add(self.current_index)
        self._update_series()

    def clear_selection(self):
        self.selected_indices.clear()
        self.current_index = 0
        self._update_series()

    def select_all(self):
        self.selected_indices = set(range(len(self.points)))
        self.current_index = 0
        self._update_series()

    def adjust_selected_offset(self, delta):
        if not self.selected_indices:
            return
        for idx in self.selected_indices:
            v, f = self.points[idx]
            new_f = max(800, min(3000, f + delta))
            self.points[idx] = (v, int(new_f))
        self._update_series()
        for idx in self.selected_indices:
            self.pointDragged.emit(idx, self.points[idx][1])

    _last_drag_ms = 0

    def mousePressEvent(self, event):
        pos = event.position().toPoint()
        min_dist = 30
        self.dragging_index = -1
        self._group_drag = False
        hit_index = -1
        for i, (v, f) in enumerate(self.points):
            scene_pos = self.chart.mapToPosition(QPointF(v, f))
            dx = pos.x() - scene_pos.x()
            dy = pos.y() - scene_pos.y()
            if (dx * dx + dy * dy) < min_dist * min_dist:
                hit_index = i
                break
        if hit_index != -1:
            if hit_index in self.selected_indices:
                self.dragging_index = hit_index
                self.setCursor(Qt.ClosedHandCursor)
        else:
            if len(self.selected_indices) == len(self.points):
                self._group_drag = True
                self._group_drag_start_y = pos.y()
                self.setCursor(Qt.ClosedHandCursor)
            else:
                self.clear_selection()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.dragging_index != -1 or self._group_drag:
            now = int(time.monotonic() * 1000)
            if now - self._last_drag_ms < 16:
                super().mouseMoveEvent(event)
                return
            self._last_drag_ms = now

        if self.dragging_index != -1:
            pos = event.position().toPoint()
            scene_pos = self.chart.mapToValue(QPointF(pos.x(), pos.y()))
            new_f = int(max(800, min(3000, scene_pos.y())))
            self.set_point_freq(self.dragging_index, new_f)
            self.pointDragged.emit(self.dragging_index, new_f)
        elif self._group_drag:
            pos = event.position().toPoint()
            start_pos = QPointF(0, self._group_drag_start_y)
            current_pos = QPointF(0, pos.y())
            start_value = self.chart.mapToValue(start_pos).y()
            current_value = self.chart.mapToValue(current_pos).y()
            delta_freq = int(current_value - start_value)
            if delta_freq != 0:
                for i in range(len(self.points)):
                    v, f = self.points[i]
                    self.points[i] = (v, max(800, min(3000, f + delta_freq)))
                self._update_series()
                self.pointDragged.emit(-1, 0)
                self._group_drag_start_y = pos.y()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.dragging_index != -1:
            self.setCursor(Qt.ArrowCursor)
            self.pointReleased.emit(self.dragging_index, self.points[self.dragging_index][1])
            self.dragging_index = -1
        elif self._group_drag:
            self.setCursor(Qt.ArrowCursor)
            self._group_drag = False
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key_Right:
            self.select_next()
        elif event.key() == Qt.Key_Left:
            self.select_prev()
        elif event.key() == Qt.Key_Up:
            self.adjust_selected_offset(1)
        elif event.key() == Qt.Key_Down:
            self.adjust_selected_offset(-1)
        elif event.key() == Qt.Key_Space:
            if self.current_index in self.selected_indices:
                self.selected_indices.remove(self.current_index)
            else:
                self.selected_indices.add(self.current_index)
            self._update_series()
            self.selectionChanged.emit()
        elif event.key() == Qt.Key_Escape:
            self.clear_selection()
        else:
            super().keyPressEvent(event)


# ═══════════════════════════════════════════════════════════════════════
# Root helper plumbing (pkexec + JSON-over-stdin, worker thread)
# ═══════════════════════════════════════════════════════════════════════
class RootHelperWorker(QThread):
    finished_ok = Signal(dict)
    finished_err = Signal(str)

    def __init__(self, payload_dict, parent=None):
        super().__init__(parent)
        self.payload_dict = payload_dict

    @staticmethod
    def _parse_last_json_line(out):
        if not out:
            raise json.JSONDecodeError("empty output", "", 0)
        lines = [l for l in out.splitlines() if l.strip()]
        if not lines:
            raise json.JSONDecodeError("empty output", "", 0)
        return json.loads(lines[-1])

    def run(self):
        proc = None
        try:
            trusted, reason = _root_helper_is_trusted(ROOT_HELPER_PATH)
            if not trusted:
                self.finished_err.emit(reason)
                return

            json_arg = json.dumps(self.payload_dict)
            env = os.environ.copy()
            for var in ('DISPLAY', 'XAUTHORITY', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS'):
                if var in os.environ:
                    env[var] = os.environ[var]

            proc = subprocess.Popen(
                ["pkexec", ROOT_HELPER_PATH],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, env=env,
            )
            out, err = proc.communicate(input=json_arg, timeout=120)

            if proc.returncode == 0:
                try:
                    res = self._parse_last_json_line(out)
                except json.JSONDecodeError:
                    self.finished_err.emit("Invalid root_helper response.")
                    return
                if res.get("ok"):
                    self.finished_ok.emit(res)
                else:
                    self.finished_err.emit(res.get("error", "Unknown error"))
            else:
                real_err = None
                if out:
                    try:
                        res = self._parse_last_json_line(out)
                        real_err = res.get("error")
                    except json.JSONDecodeError:
                        pass
                self.finished_err.emit(real_err or (err.strip() if err else "pkexec/polkit authorization denied."))
        except subprocess.TimeoutExpired:
            if proc:
                proc.kill()
                proc.wait()
            self.finished_err.emit("timeout (120s)")
        except Exception as e:
            self.finished_err.emit(str(e))


# ═══════════════════════════════════════════════════════════════════════
# Main window
# ═══════════════════════════════════════════════════════════════════════
class NVCurveWindow(QMainWindow):
    _DEFAULT_MARK = " ★"

    def __init__(self):
        super().__init__()
        self.setWindowTitle("nvcurve-gui — GPU V/F Curve Tuning")
        self.resize(980, 720)

        # State
        self._default_points = []
        self._default_base_freqs = []
        self._gpu_indices = []
        self._point_offsets = {}
        self._core_offset = 0
        self._curve_modified = False
        self._flatten_threshold = -1
        self._read_offsets = {}
        self._read_core_offset = 0
        self._busy = False
        self._workers = []  # keep references so QThreads aren't GC'd mid-flight
        self._tray_cooldown = False  # rate-limits tray-triggered applies
        self._force_quit = False     # True when quitting via tray menu, not the X
        self.tray_icon = None
        self.tray_menu = None

        self.nvml_available = False
        self.nvml_handle = None
        self._nvml = None

        self._build_ui()
        self._refresh_profile_list()

        self.gpu_info_timer = QTimer(self)
        self.gpu_info_timer.setInterval(2000)
        self.gpu_info_timer.timeout.connect(self._update_gpu_info)
        self.gpu_info_timer.start()

        QTimer.singleShot(0, self._nvml_init_async)

        # System tray icon (close-to-tray + quick profile apply menu).
        self._setup_tray()

        # Auto-read the current V/F curve once on startup so the graph
        # reflects real hardware state immediately instead of the
        # placeholder default_points. Deferred with singleShot so the
        # window is shown first — the pkexec auth prompt this triggers
        # then appears over an already-visible window rather than before
        # anything is drawn.
        QTimer.singleShot(0, self._read_gpu_curve)

        # When launched with --tray (autostart on login), apply the default
        # profile once and stay hidden in the tray.
        if "--tray" in sys.argv:
            QTimer.singleShot(400, self._autoload_default_profile)

    # ─── Single-instance ──────────────────────────────────────────────
    def attach_single_instance_guard(self, guard):
        """Wire the primary instance's QLocalServer to raise this window
        whenever a second launch pings it instead of starting its own GUI."""
        self._single_instance_guard = guard
        if guard.server is not None:
            guard.server.newConnection.connect(self._on_single_instance_ping)

    def _on_single_instance_ping(self):
        server = self._single_instance_guard.server
        conn = server.nextPendingConnection()
        if conn is not None:
            conn.readyRead.connect(lambda: self._raise_from_second_instance(conn))

    def _raise_from_second_instance(self, conn):
        conn.readAll()  # drain the "raise" payload
        conn.disconnectFromServer()
        was_hidden = self.isHidden()
        if was_hidden:
            self.show()
        self.setWindowState(
            (self.windowState() & ~Qt.WindowMinimized) | Qt.WindowActive
        )
        self.raise_()
        self.activateWindow()
        if was_hidden:
            self._read_gpu_curve()

    # ─── Logging ─────────────────────────────────────────────────────
    def _log(self, text):
        self.log_view.append(text)

    # ─── UI construction ─────────────────────────────────────────────
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        # GPU Status
        info_group = QGroupBox(" GPU Status ")
        info_layout = QHBoxLayout(info_group)
        self.gpu_temp = SL("Temp: -- °C", color=C_ORANGE, bold=True)
        self.gpu_mem_temp = SL("Mem Temp: -- °C", color=C_ORANGE, bold=True)
        self.gpu_hotspot_temp = SL("Hotspot: -- °C", color=C_STOP, bold=True)
        self.gpu_power = SL("Power: -- W", color=C_YELLOW, bold=True)
        self.gpu_clock = SL("Clock: -- MHz", color=C_CYAN, bold=True)
        self.gpu_mem_clock = SL("Mem Clock: -- MHz", color=C_GREEN, bold=True)
        for w in (self.gpu_temp, self.gpu_mem_temp, self.gpu_hotspot_temp,
                  self.gpu_power, self.gpu_clock, self.gpu_mem_clock):
            info_layout.addWidget(w)
        info_layout.addStretch()

        self.btn_save_profile = QPushButton("💾 Save As")
        self.btn_save_profile.setObjectName("btnPrimary")
        self.btn_save_profile.setFixedHeight(24)
        self.btn_save_profile.setFixedWidth(90)
        self.btn_save_profile.clicked.connect(self._save_profile_as)
        info_layout.addWidget(self.btn_save_profile)

        self.profile_combo = QComboBox()
        self.profile_combo.setFixedHeight(24)
        self.profile_combo.setMinimumWidth(160)
        self.profile_combo.activated.connect(self._on_profile_selected)
        info_layout.addWidget(self.profile_combo)
        layout.addWidget(info_group)

        # Profile toolbar
        profile_toolbar = QHBoxLayout()
        self.chk_autostart = QCheckBox("Autostart in tray")
        self.chk_autostart.setToolTip(
            "Launch nvcurve-gui automatically on login, start minimized to the\n"
            "system tray, and apply the default profile (if one is set)."
        )
        self.chk_autostart.setChecked(os.path.isfile(AUTOSTART_FILE))
        self.chk_autostart.toggled.connect(self._on_autostart_toggled)
        profile_toolbar.addWidget(self.chk_autostart)
        profile_toolbar.addStretch()
        self.btn_set_default_profile = QPushButton("⭐ Default")
        self.btn_set_default_profile.setObjectName("btnAccent")
        self.btn_set_default_profile.setFixedHeight(24)
        self.btn_set_default_profile.setFixedWidth(100)
        self.btn_set_default_profile.clicked.connect(self._toggle_default_gpu_profile)
        profile_toolbar.addWidget(self.btn_set_default_profile)
        self.btn_delete_profile = QPushButton("🗑 Delete")
        self.btn_delete_profile.setObjectName("btnDanger")
        self.btn_delete_profile.setFixedHeight(24)
        self.btn_delete_profile.setFixedWidth(100)
        self.btn_delete_profile.clicked.connect(self._delete_gpu_profile)
        profile_toolbar.addWidget(self.btn_delete_profile)
        layout.addLayout(profile_toolbar)

        # Point Info & Controls
        info_panel = QGroupBox(" Point Info & Controls ")
        info_panel_layout = QHBoxLayout(info_panel)
        info_panel_layout.setContentsMargins(6, 6, 6, 6)
        info_panel_layout.setSpacing(4)

        self.point_index_label = SL("Selected: -", color=C_YELLOW, bold=True)
        self.point_voltage_label = SL("Voltage: - mV", color=C_CYAN)
        self.point_freq_label = SL("Freq: - MHz", color=C_GREEN)
        self.point_offset_label = SL("Offset: - MHz", color=C_ORANGE)
        self.point_offset_spin = QSpinBox()
        self.point_offset_spin.setRange(-1000, 1000)
        self.point_offset_spin.setValue(0)
        self.point_offset_spin.setSuffix(" MHz")
        self.point_offset_spin.setFixedWidth(90)
        self.point_offset_spin.valueChanged.connect(self._on_point_offset_spin_changed)
        self.point_offset_spin.setEnabled(False)

        for lbl in (self.point_index_label, self.point_voltage_label,
                    self.point_freq_label):
            lbl.setFixedWidth(100)
        # "Total Offset: -1234 MHz" is longer than the other readouts, so give
        # it more room; a fixed 100px clipped it under the spin box.
        self.point_offset_label.setMinimumWidth(150)

        info_panel_layout.addWidget(self.point_index_label)
        info_panel_layout.addWidget(self.point_voltage_label)
        info_panel_layout.addWidget(self.point_freq_label)
        info_panel_layout.addWidget(self.point_offset_label)
        info_panel_layout.addSpacing(8)
        info_panel_layout.addWidget(self.point_offset_spin)

        limit_label = SL("Flatten After Index:", color=C_GREY)
        limit_label.setFixedWidth(135)
        self.limit_spin = QSpinBox()
        self.limit_spin.setRange(-1, 126)
        self.limit_spin.setSpecialValueText(" ")
        self.limit_spin.setValue(-1)
        self.limit_spin.setFixedHeight(24)
        self.limit_spin.setFixedWidth(60)
        self.limit_spin.editingFinished.connect(self._on_flatten_entered)

        info_panel_layout.addStretch()
        info_panel_layout.addWidget(limit_label)
        info_panel_layout.addWidget(self.limit_spin)
        layout.addWidget(info_panel)

        # V/F Curve
        curve_group = QGroupBox(" V/F Curve ")
        curve_layout = QVBoxLayout(curve_group)
        self.vf_widget = VFCurveWidget(parent=self)
        self.vf_widget.pointReleased.connect(self._on_point_released)
        self.vf_widget.pointDragged.connect(self._on_point_dragged)
        self.vf_widget.selectionChanged.connect(self._on_selection_changed)
        curve_layout.addWidget(self.vf_widget)

        bottom_buttons_layout = QHBoxLayout()
        bottom_buttons_layout.addStretch()
        self.btn_select_all = QPushButton("✅ Select All")
        self.btn_select_all.setObjectName("btnPrimary")
        self.btn_select_all.setFixedHeight(24)
        self.btn_select_all.clicked.connect(self.vf_widget.select_all)
        bottom_buttons_layout.addWidget(self.btn_select_all)
        self.btn_reset_graph = QPushButton("↺ Reset Graph")
        self.btn_reset_graph.setObjectName("btnDanger")
        self.btn_reset_graph.setFixedHeight(24)
        self.btn_reset_graph.clicked.connect(self._reset_graph_to_last_read)
        bottom_buttons_layout.addWidget(self.btn_reset_graph)
        curve_layout.addLayout(bottom_buttons_layout)
        layout.addWidget(curve_group, stretch=1)

        # Controls
        control_group = QGroupBox(" Controls ")
        control_layout = QHBoxLayout(control_group)

        core_label = SL("Core Offset:", color=C_GREY)
        self.core_offset_spin = QSpinBox()
        self.core_offset_spin.setRange(-500, 500)
        self.core_offset_spin.setValue(0)
        self.core_offset_spin.setSuffix(" MHz")
        self.core_offset_spin.setFixedHeight(26)
        self.core_offset_spin.setFixedWidth(120)
        self.core_offset_spin.valueChanged.connect(self._on_core_offset_changed)
        control_layout.addWidget(core_label)
        control_layout.addWidget(self.core_offset_spin)
        control_layout.addSpacing(20)

        mem_label = SL("Memory Offset:", color=C_GREY)
        self.mem_offset_spin = QSpinBox()
        self.mem_offset_spin.setRange(-3000, 3000)
        self.mem_offset_spin.setValue(0)
        self.mem_offset_spin.setSuffix(" MHz")
        self.mem_offset_spin.setFixedHeight(26)
        self.mem_offset_spin.setFixedWidth(100)
        control_layout.addWidget(mem_label)
        control_layout.addWidget(self.mem_offset_spin)
        control_layout.addSpacing(15)

        vram_lock_label = SL("VRAM Lock:", color=C_GREY)
        control_layout.addWidget(vram_lock_label)
        self.vram_lock_min_spin = QSpinBox()
        self.vram_lock_min_spin.setRange(0, 20000)
        self.vram_lock_min_spin.setSpecialValueText("–")
        self.vram_lock_min_spin.setValue(0)
        self.vram_lock_min_spin.setSuffix(" MHz")
        self.vram_lock_min_spin.setFixedHeight(26)
        self.vram_lock_min_spin.setFixedWidth(90)
        self.vram_lock_min_spin.setToolTip("Min memory clock (MHz). Left at 0 → uses Max for both.")
        control_layout.addWidget(self.vram_lock_min_spin)
        control_layout.addWidget(SL("–", color=C_GREY))
        self.vram_lock_max_spin = QSpinBox()
        self.vram_lock_max_spin.setRange(0, 20000)
        self.vram_lock_max_spin.setSpecialValueText("–")
        self.vram_lock_max_spin.setValue(0)
        self.vram_lock_max_spin.setSuffix(" MHz")
        self.vram_lock_max_spin.setFixedHeight(26)
        self.vram_lock_max_spin.setFixedWidth(90)
        self.vram_lock_max_spin.setToolTip("Max memory clock (MHz) — the actual lock target.")
        control_layout.addWidget(self.vram_lock_max_spin)

        self.btn_vram_lock = QPushButton("🔒")
        self.btn_vram_lock.setObjectName("btnAccent")
        self.btn_vram_lock.setFixedHeight(24)
        self.btn_vram_lock.setFixedWidth(34)
        self.btn_vram_lock.setToolTip("Lock VRAM clock to Min–Max now")
        self.btn_vram_lock.clicked.connect(self._apply_vram_memlock)
        control_layout.addWidget(self.btn_vram_lock)
        self.btn_vram_unlock = QPushButton("🔓")
        self.btn_vram_unlock.setObjectName("btnDanger")
        self.btn_vram_unlock.setFixedHeight(24)
        self.btn_vram_unlock.setFixedWidth(34)
        self.btn_vram_unlock.setToolTip("Unlock VRAM clock")
        self.btn_vram_unlock.clicked.connect(self._reset_vram_memlock)
        control_layout.addWidget(self.btn_vram_unlock)

        control_layout.addStretch()

        self.btn_read_curve = QPushButton("📥 Read Current Curve")
        self.btn_read_curve.setObjectName("btnPrimary")
        self.btn_read_curve.setFixedHeight(24)
        self.btn_read_curve.clicked.connect(self._read_gpu_curve)
        control_layout.addWidget(self.btn_read_curve)
        self.btn_apply_offset = QPushButton("🚀 Apply Offsets")
        self.btn_apply_offset.setObjectName("btnApply")
        self.btn_apply_offset.setFixedHeight(24)
        self.btn_apply_offset.clicked.connect(self._apply_gpu_offsets)
        control_layout.addWidget(self.btn_apply_offset)
        self.btn_reset_curve = QPushButton("↺ Reset Curve")
        self.btn_reset_curve.setObjectName("btnDanger")
        self.btn_reset_curve.setFixedHeight(24)
        self.btn_reset_curve.clicked.connect(self._reset_gpu_curve)
        control_layout.addWidget(self.btn_reset_curve)

        layout.addWidget(control_group)

        # Log view
        log_group = QGroupBox(" Log ")
        log_layout = QVBoxLayout(log_group)
        from PySide6.QtWidgets import QTextEdit
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFixedHeight(110)
        self.log_view.setStyleSheet(f"background:{C_BG3}; color:{C_LIGHT}; font-family:monospace; font-size:9pt;")
        log_layout.addWidget(self.log_view)
        layout.addWidget(log_group)

    # ─── Generic root_helper runner ──────────────────────────────────
    def _run_root_helper_command(self, payload_dict, success_msg, fail_msg, callback=None, quiet_success=False):
        if self._busy:
            self._log("⏳ Another operation is still running — please wait.")
            return
        self._busy = True
        self._set_busy_ui(True)
        worker = RootHelperWorker(payload_dict, parent=self)
        self._workers.append(worker)

        def _finish():
            self._busy = False
            self._set_busy_ui(False)

        def on_ok(res):
            if not quiet_success:
                msg = res.get("message", "")
                if msg:
                    for line in msg.strip().splitlines():
                        self._log(line)
                self._log(f"✔ {success_msg}")
            if callback:
                callback(res)
            self._workers.remove(worker)
            _finish()

        def on_err(err):
            self._log(f"✘ {fail_msg}: {err}")
            if callback:
                callback(None)
            self._workers.remove(worker)
            _finish()

        worker.finished_ok.connect(on_ok)
        worker.finished_err.connect(on_err)
        worker.start()

    def _set_busy_ui(self, busy):
        """Disable the privileged action buttons while an op is in flight."""
        for attr in ("btn_apply_offset", "btn_read_curve", "btn_reset_curve",
                     "btn_vram_lock", "btn_vram_unlock", "btn_save_profile",
                     "btn_set_default_profile", "btn_delete_profile"):
            w = getattr(self, attr, None)
            if w is not None:
                w.setEnabled(not busy)
        if hasattr(self, "tray_menu") and self.tray_menu is not None:
            self.tray_menu.setEnabled(not busy)

    # ─── Curve display recompute ─────────────────────────────────────
    def _recompute_display(self):
        if not self._default_points:
            return
        points = []
        for i, (v, base_freq) in enumerate(self._default_points):
            offset = self._point_offsets.get(i, 0)
            freq = base_freq + offset + self._core_offset
            freq = max(0, freq)
            points.append((v, int(freq)))
        self.vf_widget.update_points(points)
        self._on_selection_changed()

    def _update_core_offset_ui(self):
        if self._curve_modified:
            self.core_offset_spin.setEnabled(False)
            self.core_offset_spin.setRange(-501, 500)
            self.core_offset_spin.blockSignals(True)
            self.core_offset_spin.setValue(-501)
            self.core_offset_spin.blockSignals(False)
            self.core_offset_spin.setSpecialValueText("⚠ CURVE")
            self.core_offset_spin.setStyleSheet(f"QSpinBox {{ color:{C_STOP}; font-weight:bold; }}")
        else:
            self.core_offset_spin.setEnabled(True)
            self.core_offset_spin.setSpecialValueText("")
            self.core_offset_spin.setRange(-500, 500)
            self.core_offset_spin.setStyleSheet("")

    def _apply_flatten(self):
        if not self._default_points:
            return
        start_idx = self._flatten_threshold
        if start_idx < 0 or start_idx >= len(self._default_points):
            return
        base_start_freq = self._default_points[start_idx][1]
        start_offset = self._point_offsets.get(start_idx, 0) + self._core_offset
        target_freq = base_start_freq + start_offset

        for i in range(start_idx + 1, len(self._default_points)):
            self._point_offsets.pop(i, None)
        for i in range(start_idx + 1, len(self._default_points)):
            base_freq = self._default_points[i][1]
            offset = target_freq - base_freq - self._core_offset
            self._point_offsets[i] = int(offset)

        self._recompute_display()

    def _on_flatten_entered(self):
        val = self.limit_spin.value()
        self._flatten_threshold = val
        self._apply_flatten()
        self._curve_modified = True
        self._update_core_offset_ui()
        self._log(f"Flatten applied from index {val}")

    def _on_core_offset_changed(self, val):
        self._core_offset = val
        self._recompute_display()

    def _on_point_offset_spin_changed(self, val):
        idx = self.vf_widget.current_index
        if idx is None or idx < 0 or idx >= len(self._default_points):
            return
        if idx not in self.vf_widget.selected_indices:
            return
        self._point_offsets[idx] = val - self._core_offset
        self._curve_modified = True
        self._update_core_offset_ui()
        self._recompute_display()

    def _on_point_released(self, index, freq):
        base = self._default_base_freqs[index] if index < len(self._default_base_freqs) else 0
        offset = freq - base - self._core_offset
        if self._point_offsets.get(index, 0) != offset:
            self._point_offsets[index] = offset
            self._curve_modified = True
            self._update_core_offset_ui()
            self._recompute_display()

    def _sync_offsets_from_widget(self):
        if not self._default_points:
            return
        points = self.vf_widget.get_points()
        for i, (v, f) in enumerate(points):
            if i < len(self._default_base_freqs):
                base = self._default_base_freqs[i]
                self._point_offsets[i] = f - base - self._core_offset

    def _on_point_dragged(self, index, new_freq):
        if index == -1:
            self._sync_offsets_from_widget()
            self._curve_modified = True
            self._update_core_offset_ui()
            self._recompute_display()

    def _on_selection_changed(self):
        selected = self.vf_widget.selected_indices
        if selected:
            max_idx = max(selected)
            self.limit_spin.blockSignals(True)
            self.limit_spin.setValue(max_idx)
            self.limit_spin.blockSignals(False)

            idx = self.vf_widget.current_index if self.vf_widget.current_index in selected else next(iter(selected))
            v, f = self.vf_widget.points[idx]
            offset = self._point_offsets.get(idx, 0)
            total_offset = offset + self._core_offset

            self.point_index_label.setText(f"Selected: {len(selected)} pts")
            self.point_voltage_label.setText(f"Voltage: {v} mV")
            self.point_freq_label.setText(f"Freq: {f} MHz")
            self.point_offset_label.setText(f"Total Offset: {total_offset} MHz")

            self.point_offset_spin.setEnabled(True)
            self.point_offset_spin.blockSignals(True)
            self.point_offset_spin.setValue(total_offset)
            self.point_offset_spin.blockSignals(False)
        else:
            self.point_index_label.setText("Selected: -")
            self.point_voltage_label.setText("Voltage: - mV")
            self.point_freq_label.setText("Freq: - MHz")
            self.point_offset_label.setText("Offset: - MHz")
            self.point_offset_spin.setEnabled(False)
            self.point_offset_spin.blockSignals(True)
            self.point_offset_spin.setValue(0)
            self.point_offset_spin.blockSignals(False)

    def _reset_graph_to_last_read(self):
        # The graph is about to be reverted to the last-read hardware
        # values, so any profile shown as "selected" in the combo box no
        # longer matches what's displayed — deselect it (blank the combo)
        # rather than leaving a stale profile name shown. We only clear
        # the selection here, never the list itself.
        self.profile_combo.blockSignals(True)
        self.profile_combo.setCurrentIndex(-1)
        self.profile_combo.blockSignals(False)

        if not self._read_offsets:
            self._core_offset = getattr(self, '_read_core_offset', 0)
            self._point_offsets = {}
            self.core_offset_spin.blockSignals(True)
            self.core_offset_spin.setValue(self._core_offset)
            self.core_offset_spin.blockSignals(False)
            self._curve_modified = False
            self._update_core_offset_ui()
            self._recompute_display()
            self.vf_widget.clear_selection()
            self.point_offset_spin.blockSignals(True)
            self.point_offset_spin.setValue(0)
            self.point_offset_spin.blockSignals(False)
            self._log("↺ Graph reset to last read values (core offset only).")
            return

        offsets = [self._read_offsets.get(i, 0) for i in range(127)]
        unique_offsets = set(offsets)
        if len(unique_offsets) == 1:
            core_val = unique_offsets.pop()
            self._point_offsets = self._read_offsets.copy()
            self._core_offset = core_val
            self.core_offset_spin.blockSignals(True)
            self.core_offset_spin.setValue(core_val)
            self.core_offset_spin.blockSignals(False)
            self._curve_modified = False
        else:
            self._point_offsets = self._read_offsets.copy()
            self._core_offset = 0
            self.core_offset_spin.blockSignals(True)
            self.core_offset_spin.setValue(0)
            self.core_offset_spin.blockSignals(False)
            self._curve_modified = True

        self._flatten_threshold = -1
        self.limit_spin.blockSignals(True)
        self.limit_spin.setValue(-1)
        self.limit_spin.blockSignals(False)
        self._update_core_offset_ui()
        self._recompute_display()
        self.vf_widget.clear_selection()
        self.point_offset_spin.blockSignals(True)
        self.point_offset_spin.setValue(0)
        self.point_offset_spin.blockSignals(False)
        self._log("↺ Graph reset to last read values.")

    # ─── Profile save/load/default/delete ────────────────────────────
    def _strip_default_mark(self, text):
        return text[:-len(self._DEFAULT_MARK)] if text.endswith(self._DEFAULT_MARK) else text

    def _gather_current_profile_data(self):
        offsets = {}
        for i in range(127):
            offset = self._point_offsets.get(i, 0) + self._core_offset
            if offset != 0:
                offsets[str(i)] = offset * 1000
        mem_off = self.mem_offset_spin.value()
        vram_lock_max = self.vram_lock_max_spin.value()
        vram_lock_min = self.vram_lock_min_spin.value()
        if vram_lock_max != 0 and vram_lock_min == 0:
            vram_lock_min = vram_lock_max
        return {
            "name": "custom",
            "curve_deltas": offsets,
            "mem_offset_mhz": mem_off,
            "power_limit_w": None,
            "mem_locked_min_mhz": vram_lock_min if vram_lock_max != 0 else None,
            "mem_locked_max_mhz": vram_lock_max if vram_lock_max != 0 else None,
        }

    def _save_profile_as(self):
        name, ok = QInputDialog.getText(self, "Profile Name", "Enter profile name:", text="custom")
        if not ok or not name.strip():
            self._log("❌ No profile name entered, cancelled.")
            return
        name = name.strip()
        profile_data = self._gather_current_profile_data()
        profile_data["name"] = name
        json_str = json.dumps(profile_data, indent=2)

        payload = {"op": "write_nvcurve_profile", "name": name, "content": json_str}

        def on_saved(res):
            if res is None:
                return
            self._refresh_profile_list()
            idx = self._find_profile_index(name)
            if idx >= 0:
                self.profile_combo.setCurrentIndex(idx)

        self._run_root_helper_command(
            payload, f"Profile '{name}' saved.", f"Profile '{name}' could not be saved.",
            callback=on_saved,
        )

    def _find_profile_index(self, name: str) -> int:
        for i in range(self.profile_combo.count()):
            if self._strip_default_mark(self.profile_combo.itemText(i)) == name:
                return i
        return -1

    def _refresh_profile_list(self):
        try:
            if not os.path.exists(NVCURVE_PROFILES_DIR):
                self._log(f"⚠️ Profile directory does not exist yet: {NVCURVE_PROFILES_DIR}")
                self.profile_combo.blockSignals(True)
                self.profile_combo.clear()
                self.profile_combo.blockSignals(False)
                return
            files = [f for f in os.listdir(NVCURVE_PROFILES_DIR) if f.endswith('.json')]
            # Hide the internal scratch profile used by "Apply Offsets".
            files = [f for f in files if f != f"{SCRATCH_PROFILE}.json"]
            files.sort()
            default_name = self._read_default_gpu_profile_name()
            self.profile_combo.blockSignals(True)
            self.profile_combo.clear()
            for f in files:
                name = f.replace('.json', '')
                label = f"{name}{self._DEFAULT_MARK}" if name == default_name else name
                self.profile_combo.addItem(label, f)
            if self.profile_combo.count() > 0:
                self.profile_combo.setCurrentIndex(0)
            self.profile_combo.blockSignals(False)
            if getattr(self, "tray_menu", None) is not None:
                self._rebuild_tray_menu()
        except Exception as e:
            self._log(f"⚠️ Could not read the profile list: {e}")

    def _on_profile_selected(self, index):
        if index < 0:
            return
        profile_name = self._strip_default_mark(self.profile_combo.currentText())
        if not profile_name:
            return
        profile_path = os.path.join(NVCURVE_PROFILES_DIR, f"{profile_name}.json")
        try:
            with open(profile_path, 'r') as f:
                data = json.load(f)
            self._apply_profile_to_ui(data, profile_name)
        except FileNotFoundError:
            self._log(f"❌ Profile file not found: {profile_path}")
        except Exception as e:
            self._log(f"❌ Error while loading profile: {e}")

    def _apply_profile_to_ui(self, data, profile_name=""):
        if not self._default_points:
            self._default_points = [(700 + i * 4, 1500 + i * 2) for i in range(127)]
            self._default_base_freqs = [f for _, f in self._default_points]

        curve_deltas = data.get('curve_deltas', {})
        mem_offset_raw = data.get('mem_offset_mhz', 0) or 0
        if mem_offset_raw != 0:
            mem_offset = mem_offset_raw
        elif '131' in curve_deltas:
            mem_offset = curve_deltas['131'] // 1000
        elif '132' in curve_deltas:
            mem_offset = curve_deltas['132'] // 1000
        else:
            mem_offset = 0

        new_offsets = {}
        for i in range(127):
            val_khz = curve_deltas.get(str(i), 0)
            new_offsets[i] = val_khz // 1000

        all_vals = list(new_offsets.values())
        if all_vals and all(v == all_vals[0] for v in all_vals):
            core_val = all_vals[0]
            self._core_offset = core_val
            self._point_offsets = {}
        else:
            self._core_offset = 0
            self._point_offsets = new_offsets

        self.core_offset_spin.blockSignals(True)
        self.core_offset_spin.setValue(self._core_offset)
        self.core_offset_spin.blockSignals(False)
        self.mem_offset_spin.blockSignals(True)
        self.mem_offset_spin.setValue(mem_offset)
        self.mem_offset_spin.blockSignals(False)
        self.vram_lock_min_spin.blockSignals(True)
        self.vram_lock_max_spin.blockSignals(True)
        self.vram_lock_min_spin.setValue(data.get('mem_locked_min_mhz') or 0)
        self.vram_lock_max_spin.setValue(data.get('mem_locked_max_mhz') or 0)
        self.vram_lock_min_spin.blockSignals(False)
        self.vram_lock_max_spin.blockSignals(False)

        # NOTE: intentionally NOT touching self._read_offsets /
        # self._read_core_offset here — those represent the last value
        # actually read from the GPU hardware, and must stay untouched
        # by loading a profile into the UI. Otherwise "Reset Graph"
        # would revert to this profile's data instead of real hardware
        # state.

        self._flatten_threshold = -1
        self.limit_spin.blockSignals(True)
        self.limit_spin.setValue(-1)
        self.limit_spin.blockSignals(False)
        self._curve_modified = False
        self._update_core_offset_ui()

        self.vf_widget.clear_selection()
        self._recompute_display()
        self.point_offset_spin.blockSignals(True)
        self.point_offset_spin.setValue(0)
        self.point_offset_spin.blockSignals(False)
        self._log(f"✅ Profile applied to UI: {profile_name} (Core: {self._core_offset}, Mem: {mem_offset})")

    def _read_default_gpu_profile_name(self):
        try:
            with open(NVCURVE_CONFIG_PATH, "r") as f:
                cfg = json.load(f)
            profiles = cfg.get("auto_load_profiles", {})
            if not profiles and cfg.get("auto_load_profile"):
                profiles = {"idx:0": cfg["auto_load_profile"]}
            if profiles:
                return next(iter(profiles.values()))
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        except Exception as e:
            self._log(f"⚠️ Could not read default GPU profile: {e}")
        return None

    def _refresh_default_star(self):
        default_name = self._read_default_gpu_profile_name()
        self.profile_combo.blockSignals(True)
        current_idx = self.profile_combo.currentIndex()
        for i in range(self.profile_combo.count()):
            plain = self._strip_default_mark(self.profile_combo.itemText(i))
            label = f"{plain}{self._DEFAULT_MARK}" if plain == default_name else plain
            if self.profile_combo.itemText(i) != label:
                self.profile_combo.setItemText(i, label)
        self.profile_combo.setCurrentIndex(current_idx)
        self.profile_combo.blockSignals(False)
        self.profile_combo.update()
        if getattr(self, "tray_menu", None) is not None:
            self._rebuild_tray_menu()
        self._log(f"ℹ️ Default GPU profile is now: {default_name or '(none)'}")

    def _toggle_default_gpu_profile(self):
        profile_name = self._strip_default_mark(self.profile_combo.currentText())
        if not profile_name:
            self._log("❌ No profile selected.")
            return
        current_default = self._read_default_gpu_profile_name()
        if current_default == profile_name:
            self._clear_default_gpu_profile()
        else:
            self._set_default_gpu_profile()

    def _set_default_gpu_profile(self):
        profile_name = self._strip_default_mark(self.profile_combo.currentText())
        if not profile_name:
            self._log("❌ No profile selected to set as default.")
            return
        payload = {"op": "set_default_gpu_profile", "name": profile_name}
        self._run_root_helper_command(
            payload,
            f"'{profile_name}' set as default GPU profile.",
            f"Could not set '{profile_name}' as default.",
            callback=lambda _res: self._refresh_default_star(),
        )

    def _clear_default_gpu_profile(self):
        payload = {"op": "set_default_gpu_profile", "clear": True}
        self._run_root_helper_command(
            payload, "Default GPU profile removed.", "Could not remove default GPU profile.",
            callback=lambda _res: self._refresh_default_star(),
        )

    def _delete_gpu_profile(self):
        profile_name = self._strip_default_mark(self.profile_combo.currentText())
        if not profile_name:
            self._log("❌ No profile selected to delete.")
            return
        reply = QMessageBox.question(
            self, "Delete Profile", f"Delete profile '{profile_name}'? This cannot be undone.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        payload = {"op": "delete_nvcurve_profile", "name": profile_name}
        self._run_root_helper_command(
            payload, f"Profile '{profile_name}' deleted.", f"Could not delete '{profile_name}'.",
            callback=lambda _res: self._refresh_profile_list(),
        )

    # ─── Read / Apply / Reset curve ──────────────────────────────────
    def _read_gpu_curve(self):
        self.btn_read_curve.setEnabled(False)
        self.btn_read_curve.setText("⏳ Reading...")
        payload = {"op": "read_gpu_curve"}
        self._run_root_helper_command(
            payload, "Curve read successfully.", "Curve read failed.",
            callback=self._on_curve_read_finished,
        )

    def _apply_gpu_offsets(self):
        profile_name = SCRATCH_PROFILE
        if not self._default_points:
            self._log("❌ No curve loaded yet — read the current curve or load a profile first.")
            return

        graph_points = self.vf_widget.get_points()
        offsets_to_apply = {}
        for i, (_, freq) in enumerate(graph_points):
            if i >= len(self._default_base_freqs):
                break
            base = self._default_base_freqs[i]
            delta = int(round(freq)) - base
            if delta != 0:
                offsets_to_apply[i] = delta * 1000

        mem_off = self.mem_offset_spin.value()

        vram_lock_max = self.vram_lock_max_spin.value()
        vram_lock_min = self.vram_lock_min_spin.value()
        if vram_lock_max != 0 and vram_lock_min == 0:
            vram_lock_min = vram_lock_max

        if not offsets_to_apply and mem_off == 0 and vram_lock_max == 0:
            self._log("No offsets to apply.")
            return

        profile_data = {
            "name": profile_name,
            "curve_deltas": {str(k): v for k, v in offsets_to_apply.items()},
            "mem_offset_mhz": mem_off if mem_off != 0 else None,
            "power_limit_w": None,
            "mem_locked_min_mhz": vram_lock_min if vram_lock_max != 0 else None,
            "mem_locked_max_mhz": vram_lock_max if vram_lock_max != 0 else None,
        }

        payload = {"op": "apply_gpu_offsets", "profile_name": profile_name, "profile_data": profile_data}
        self._run_root_helper_command(
            payload, f"Profile '{profile_name}' applied successfully.",
            f"Failed to apply profile '{profile_name}'.",
            callback=self._after_apply_read,
        )

    def _reset_gpu_curve(self):
        self.btn_reset_curve.setEnabled(False)
        self.btn_reset_curve.setText("⏳ Resetting...")
        payload = {"op": "reset_gpu_curve"}
        self._run_root_helper_command(
            payload, "Reset successful.", "Reset failed.",
            callback=self._after_reset_read,
        )
        self._point_offsets = {}
        self._core_offset = 0
        self.core_offset_spin.setValue(0)
        self.mem_offset_spin.setValue(0)
        self.vram_lock_min_spin.setValue(0)
        self.vram_lock_max_spin.setValue(0)

    def _apply_vram_memlock(self):
        max_mhz = self.vram_lock_max_spin.value()
        min_mhz = self.vram_lock_min_spin.value()
        if max_mhz == 0:
            self._log("⚠️ Enter a Max MHz value first (Min defaults to Max if left at 0).")
            return
        if min_mhz == 0:
            min_mhz = max_mhz
        if min_mhz > max_mhz:
            self._log("⚠️ VRAM Lock: Min cannot be greater than Max.")
            return

        self.btn_vram_lock.setEnabled(False)
        payload = {"op": "set_vram_memlock", "min_mhz": min_mhz, "max_mhz": max_mhz}

        def done(_res):
            self.btn_vram_lock.setEnabled(True)

        self._run_root_helper_command(
            payload, f"VRAM locked to {min_mhz}–{max_mhz} MHz.", "Failed to lock VRAM clock.",
            callback=done,
        )

    def _reset_vram_memlock(self):
        self.btn_vram_unlock.setEnabled(False)
        payload = {"op": "reset_vram_memlock"}

        def done(_res):
            self.btn_vram_unlock.setEnabled(True)

        self._run_root_helper_command(
            payload, "VRAM clock unlocked.", "Failed to unlock VRAM clock.",
            callback=done,
        )

    # ─── Result-file processing (read/apply/reset) ───────────────────
    def _on_curve_read_finished(self, res):
        self.btn_read_curve.setEnabled(True)
        self.btn_read_curve.setText("📥 Read Current Curve")
        if res is None:
            self._log("❌ Read operation failed.")
            return
        json_path = os.path.join(RUN_DIR, "nvcurve_read.json")
        try:
            if not os.path.exists(json_path):
                self._log("❌ Curve data file not found.")
                return
            with open(json_path) as f:
                data = json.load(f)
            if "vf_curve" in data:
                all_points = data["vf_curve"]

                # The GPU-domain branch below has always populated
                # core_offset_spin from the read curve; the memory-domain
                # points were read into `all_points` too but nothing ever
                # forwarded them to mem_offset_spin, so the box stayed at
                # its old/manual value after every "Read Current Curve" —
                # silently disagreeing with the hardware. Mirror the same
                # "uniform offset across every point of the domain" logic
                # used for the core offset.
                #
                # freq_offset_kHz here comes straight off the same
                # ClockBoostTable NVML's mem clock-offset writes through
                # (see hal/limits.py's set_clock_offsets doubling note), so
                # it's in the same raw/half-rate MHz domain and needs the
                # same /2 conversion to the effective MHz the Mem Offset
                # box and every saved profile use.
                mem_points = [p for p in all_points if p.get("domain") == "memory"]
                if mem_points:
                    mem_offsets_raw = [p.get("freq_offset_kHz", 0) // 1000 for p in mem_points]
                    if all(o == mem_offsets_raw[0] for o in mem_offsets_raw):
                        mem_offset_effective = int(round(mem_offsets_raw[0] / 2))
                    else:
                        # Non-uniform memory-domain deltas shouldn't happen
                        # via this app, but don't silently pick one point
                        # over another if it does — average them instead.
                        mem_offset_effective = int(round((sum(mem_offsets_raw) / len(mem_offsets_raw)) / 2))
                    self.mem_offset_spin.blockSignals(True)
                    self.mem_offset_spin.setValue(mem_offset_effective)
                    self.mem_offset_spin.blockSignals(False)
                    self._log(f"ℹ️ Memory offset detected: {mem_offset_effective} MHz")
                else:
                    self.mem_offset_spin.blockSignals(True)
                    self.mem_offset_spin.setValue(0)
                    self.mem_offset_spin.blockSignals(False)

                gpu_points = [p for p in all_points if p.get("domain") == "gpu"]
                if gpu_points:
                    points = []
                    base_freqs = []
                    offsets = {}
                    for p in gpu_points:
                        idx = p["index"]
                        current_freq_mhz = p["freq_kHz"] // 1000
                        volt_mv = p["volt_uV"] // 1000
                        offset_mhz = p.get("freq_offset_kHz", 0) // 1000
                        base_freq_mhz = current_freq_mhz - offset_mhz
                        points.append((volt_mv, base_freq_mhz))
                        base_freqs.append(base_freq_mhz)
                        offsets[idx] = offset_mhz

                    offset_values = list(offsets.values())
                    if offset_values and all(o == offset_values[0] for o in offset_values):
                        global_offset = offset_values[0]
                        self._core_offset = global_offset
                        self._point_offsets = {}
                        self.core_offset_spin.blockSignals(True)
                        self.core_offset_spin.setValue(global_offset)
                        self.core_offset_spin.blockSignals(False)
                        self._log(f"ℹ️ Global core offset detected: {global_offset} MHz")
                    else:
                        self._core_offset = 0
                        self.core_offset_spin.blockSignals(True)
                        self.core_offset_spin.setValue(0)
                        self.core_offset_spin.blockSignals(False)
                        self._point_offsets = offsets

                    self._curve_modified = False
                    self._default_points = points
                    self._default_base_freqs = base_freqs
                    self._gpu_indices = [p["index"] for p in gpu_points]
                    self._read_offsets = self._point_offsets.copy() if self._point_offsets else {}

                    self._flatten_threshold = -1
                    self.limit_spin.blockSignals(True)
                    self.limit_spin.setValue(-1)
                    self.limit_spin.blockSignals(False)

                    self.vf_widget.clear_selection()
                    self.vf_widget.set_points(points, base_freqs)
                    self._recompute_display()
                    self._log(f"✅ V/F curve loaded ({len(points)} GPU points).")
                    if points:
                        self.vf_widget.axis_y.setRange(0, max(f for _, f in points) + 200)
                        self.vf_widget.axis_x.setRange(min(v for v, _ in points) - 50, max(v for v, _ in points) + 50)
                else:
                    self._log("❌ No GPU points found in vf_curve.")
            else:
                self._log("❌ 'vf_curve' key not found in JSON response.")
        except Exception as e:
            self._log(f"❌ Error processing curve: {e}")
        finally:
            self._reset_graph_to_last_read()
        try:
            os.remove(json_path)
        except Exception:
            pass

    def _after_apply_read(self, res):
        if res is None:
            self._log("❌ Apply operation failed.")
            return
        json_path = os.path.join(RUN_DIR, "nvcurve_apply_result.json")
        try:
            if not os.path.exists(json_path):
                self._log("❌ Apply result file not found.")
                return
            with open(json_path) as f:
                data = json.load(f)
            if "vf_curve" in data:
                all_points = data["vf_curve"]
                new_offsets = {}
                for p in all_points:
                    idx = p["index"]
                    offset_khz = p.get("freq_offset_kHz", 0)
                    if p.get("domain") == "gpu":
                        new_offsets[idx] = offset_khz // 1000
                self._point_offsets = new_offsets
                self._read_offsets = new_offsets.copy()
                self._core_offset = 0
                self.core_offset_spin.blockSignals(True)
                self.core_offset_spin.setValue(0)
                self.core_offset_spin.blockSignals(False)
                self._flatten_threshold = -1
                self.limit_spin.blockSignals(True)
                self.limit_spin.setValue(-1)
                self.limit_spin.blockSignals(False)
                self.vf_widget.clear_selection()
                self._recompute_display()
                self._log("✅ Offsets applied and curve updated.")
            else:
                self._log("❌ 'vf_curve' key not found in apply result.")
        except Exception as e:
            self._log(f"❌ Error processing apply result: {e}")
        finally:
            self._reset_graph_to_last_read()
        try:
            os.remove(json_path)
        except Exception:
            pass

    def _after_reset_read(self, res):
        self.btn_reset_curve.setEnabled(True)
        self.btn_reset_curve.setText("↺ Reset Curve")
        # The curve was just reset to stock — deselect any profile shown
        # in the combo box (list stays intact, only the selection clears).
        self.profile_combo.blockSignals(True)
        self.profile_combo.setCurrentIndex(-1)
        self.profile_combo.blockSignals(False)
        if res is None:
            self._log("❌ Reset operation failed.")
            return
        json_path = os.path.join(RUN_DIR, "nvcurve_reset_result.json")
        try:
            if not os.path.exists(json_path):
                self._log("❌ Reset result file not found.")
                return
            with open(json_path) as f:
                data = json.load(f)
            if "vf_curve" in data:
                all_points = data["vf_curve"]
                gpu_points = [p for p in all_points if p.get("domain") == "gpu"]
                if gpu_points:
                    points = []
                    base_freqs = []
                    for p in gpu_points:
                        current_freq_mhz = p["freq_kHz"] // 1000
                        volt_mv = p["volt_uV"] // 1000
                        offset_mhz = p.get("freq_offset_kHz", 0) // 1000
                        base_freq_mhz = current_freq_mhz - offset_mhz
                        points.append((volt_mv, base_freq_mhz))
                        base_freqs.append(base_freq_mhz)
                    self._default_points = points
                    self._default_base_freqs = base_freqs
                    self._gpu_indices = [p["index"] for p in gpu_points]
                self._point_offsets = {}
                self._read_offsets = {}
                self._core_offset = 0
                self._curve_modified = False
                self.vf_widget.clear_selection()
                self.vf_widget.set_points(points if gpu_points else self._default_points, base_freqs if gpu_points else self._default_base_freqs)
                self._recompute_display()
                self._log("✅ Curve reset and reloaded.")
        except Exception as e:
            self._log(f"❌ Error processing reset result: {e}")
        finally:
            self._update_core_offset_ui()
        try:
            os.remove(json_path)
        except Exception:
            pass

    # ─── GPU info polling ─────────────────────────────────────────────
    def _update_gpu_info(self):
        if not self.isVisible():
            return
        if self.nvml_available and self.nvml_handle is not None:
            try:
                nvml = self._nvml
                temp = nvml.nvmlDeviceGetTemperature(self.nvml_handle, nvml.NVML_TEMPERATURE_GPU)
                self.gpu_temp.setText(f"Temp: {temp} °C")
                try:
                    mem_temp = nvml.nvmlDeviceGetTemperature(self.nvml_handle, nvml.NVML_TEMPERATURE_MEM)
                    self.gpu_mem_temp.setText(f"Mem Temp: {mem_temp} °C")
                except Exception:
                    self.gpu_mem_temp.setText("Mem Temp: N/A")
                try:
                    hotspot_temp = nvml.nvmlDeviceGetTemperature(self.nvml_handle, nvml.NVML_TEMPERATURE_HOTSPOT)
                    self.gpu_hotspot_temp.setText(f"Hotspot: {hotspot_temp} °C")
                except Exception:
                    self.gpu_hotspot_temp.setText("Hotspot: N/A")
                power = nvml.nvmlDeviceGetPowerUsage(self.nvml_handle) / 1000.0
                self.gpu_power.setText(f"Power: {power:.1f} W")
                clock_graphics = nvml.nvmlDeviceGetClockInfo(self.nvml_handle, nvml.NVML_CLOCK_GRAPHICS)
                clock_mem = nvml.nvmlDeviceGetClockInfo(self.nvml_handle, nvml.NVML_CLOCK_MEM)
                self.gpu_clock.setText(f"Clock: {clock_graphics} MHz")
                self.gpu_mem_clock.setText(f"Mem Clock: {clock_mem} MHz")
            except Exception:
                self._fallback_gpu_info()
        else:
            self._fallback_gpu_info()

    def _fallback_gpu_info(self):
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=temperature.gpu,power.draw,clocks.current.graphics,clocks.current.memory",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=2,
            )
            if result.stdout:
                parts = [p.strip() for p in result.stdout.strip().split(',')]
                if len(parts) >= 4:
                    temp, power, clock, mem = parts
                    self.gpu_temp.setText(f"Temp: {temp} °C")
                    self.gpu_power.setText(f"Power: {power} W")
                    self.gpu_clock.setText(f"Clock: {clock} MHz")
                    self.gpu_mem_clock.setText(f"Mem Clock: {mem} MHz")
                    self.gpu_mem_temp.setText("Mem Temp: N/A")
                    self.gpu_hotspot_temp.setText("Hotspot: N/A")
        except Exception:
            pass

    # ─── NVML init (background thread, non-blocking) ─────────────────
    def _nvml_init_async(self):
        class _NvmlInitThread(QThread):
            done = Signal(object, object, str)

            def run(self):
                try:
                    import pynvml as nvml
                    nvml.nvmlInit()
                    handle = nvml.nvmlDeviceGetHandleByIndex(0)
                    self.done.emit(nvml, handle, "")
                except Exception as e:
                    self.done.emit(None, None, str(e))

        self._nvml_thread = _NvmlInitThread(self)
        self._nvml_thread.done.connect(self._on_nvml_init_done)
        self._nvml_thread.start()

    def _on_nvml_init_done(self, nvml_module, handle, err):
        if nvml_module is not None and handle is not None:
            self._nvml = nvml_module
            self.nvml_handle = handle
            self.nvml_available = True
            self._log("✅ NVML initialized")
        else:
            self._nvml = None
            self.nvml_handle = None
            self.nvml_available = False
            self._log(f"⚠️ NVML not available: {err} (falling back to nvidia-smi)")

    # ─── System tray ─────────────────────────────────────────────────
    def _make_tray_icon(self):
        """Themed tray icon — see module-level _build_icon_pixmap()."""
        return QIcon(_build_icon_pixmap(64))

    def _setup_tray(self, attempt=0):
        # The desktop's tray/status-notifier host isn't always ready the
        # instant this process starts — this is especially common right
        # after login when launched via --tray autostart, before the
        # panel/status-area has finished registering with the session.
        # A one-shot isSystemTrayAvailable() check at that moment can
        # come back False even though the tray is about to be available
        # a moment later. Retry for a few seconds before actually giving
        # up, instead of permanently disabling the tray for the rest of
        # this process's life.
        if not QSystemTrayIcon.isSystemTrayAvailable():
            if attempt < TRAY_SETUP_MAX_RETRIES:
                QTimer.singleShot(
                    TRAY_SETUP_RETRY_MS,
                    lambda: self._setup_tray(attempt + 1),
                )
                return
            msg = (
                "nvcurve-gui: system tray never became available after "
                f"{TRAY_SETUP_MAX_RETRIES} retries "
                f"({TRAY_SETUP_RETRY_MS * TRAY_SETUP_MAX_RETRIES}ms) — "
                "close-to-tray disabled. If your desktop environment does "
                "have a tray/status area, this usually means it doesn't "
                "support the legacy tray protocol Qt uses here (e.g. "
                "GNOME on Wayland without an AppIndicator extension)."
            )
            # Printed to stderr (not just self._log's in-window widget)
            # because with --tray the window — and its log — stays
            # hidden, so this would otherwise be invisible.
            print(msg, file=sys.stderr)
            self._log("ℹ️ System tray not available; close-to-tray disabled.")
            # We never got a tray, so if this was a --tray (autostart)
            # launch that's been sitting hidden waiting for the tray,
            # show the window now rather than leaving the app invisible
            # with no way to reach it.
            if "--tray" in sys.argv and self.isHidden():
                self.show()
            return
        try:
            if attempt > 0:
                print(
                    f"nvcurve-gui: system tray became available after "
                    f"{attempt} retr{'y' if attempt == 1 else 'ies'}.",
                    file=sys.stderr,
                )
            icon = self._make_tray_icon()
            self.setWindowIcon(icon)
            self.tray_icon = QSystemTrayIcon(icon, self)
            self.tray_icon.setToolTip("nvcurve-gui — GPU V/F Curve Tuning")
            self.tray_menu = QMenu()
            self.tray_icon.setContextMenu(self.tray_menu)
            self.tray_icon.activated.connect(self._on_tray_activated)
            self._rebuild_tray_menu()
            self.tray_icon.show()
        except Exception as e:
            # A failure here would otherwise vanish silently (this runs
            # during __init__, well before the log widget is visible in
            # --tray mode) and leave tray_icon as None with no clue why.
            print(f"nvcurve-gui: tray icon setup failed: {e}", file=sys.stderr)
            self._log(f"⚠️ Tray icon setup failed: {e}")
            self.tray_icon = None
            if "--tray" in sys.argv and self.isHidden():
                self.show()

    def _rebuild_tray_menu(self):
        """(Re)populate the tray right-click menu with the saved profiles."""
        if self.tray_menu is None:
            return
        self.tray_menu.clear()

        header = self.tray_menu.addAction("Apply profile:")
        header.setEnabled(False)

        default_name = self._read_default_gpu_profile_name()
        added = 0
        try:
            files = sorted(
                f[:-5] for f in os.listdir(NVCURVE_PROFILES_DIR)
                if f.endswith(".json") and f != f"{SCRATCH_PROFILE}.json"
            )
        except OSError:
            files = []
        for name in files:
            label = f"{name}  ★" if name == default_name else name
            act = self.tray_menu.addAction(label)
            act.triggered.connect(lambda _checked=False, n=name: self._tray_apply_profile(n))
            added += 1
        if added == 0:
            none_act = self.tray_menu.addAction("(no saved profiles)")
            none_act.setEnabled(False)

        self.tray_menu.addSeparator()
        reset_act = self.tray_menu.addAction("↺ Reset Curve")
        reset_act.triggered.connect(self._reset_gpu_curve)

        self.tray_menu.addSeparator()
        show_act = self.tray_menu.addAction("Show window")
        show_act.triggered.connect(self._restore_from_tray)
        quit_act = self.tray_menu.addAction("Quit")
        quit_act.triggered.connect(self._quit_from_tray)

    def _on_tray_activated(self, reason):
        # Left-click / double-click toggles the window; right-click shows menu.
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            if self.isVisible():
                self.hide()
            else:
                self._restore_from_tray()

    def _restore_from_tray(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()
        # The curve on the GPU may have changed since this window was
        # last visible (a profile applied from the tray menu, another
        # tool, a driver/session reset, etc.) — refresh from hardware
        # instead of showing whatever was last computed while hidden.
        self._read_gpu_curve()

    def _quit_from_tray(self):
        self._force_quit = True
        self.close()
        # setQuitOnLastWindowClosed(False) is set so the X button can hide
        # to the tray instead of quitting — but that also means closing
        # the window here does NOT stop the event loop on its own. Quit
        # explicitly, or the process just keeps running invisibly.
        QApplication.instance().quit()

    def _tray_apply_profile(self, name):
        """Apply a saved profile from the tray, with a busy + cooldown guard
        so rapid clicks don't queue up multiple privileged applies."""
        if self._busy or self._tray_cooldown:
            if self.tray_icon:
                self.tray_icon.showMessage(
                    "nvcurve-gui", "Please wait — still applying the previous change.",
                    self._make_tray_icon(), 2000,
                )
            return
        profile_path = os.path.join(NVCURVE_PROFILES_DIR, f"{name}.json")
        if not os.path.isfile(profile_path):
            self._log(f"❌ Profile not found: {name}")
            return

        # start cooldown window
        self._tray_cooldown = True
        QTimer.singleShot(TRAY_APPLY_COOLDOWN_MS, self._clear_tray_cooldown)

        self._log(f"🔻 Tray: applying profile '{name}'…")
        payload = {"op": "apply_named_profile", "name": name}
        self._run_root_helper_command(
            payload,
            f"Profile '{name}' applied.",
            f"Failed to apply profile '{name}'.",
            callback=self._after_apply_read,
        )
        if self.tray_icon:
            self.tray_icon.showMessage(
                "nvcurve-gui", f"Applying '{name}'…", self._make_tray_icon(), 1500,
            )

    def _clear_tray_cooldown(self):
        self._tray_cooldown = False

    def _autoload_default_profile(self):
        """Apply whatever profile is marked default (used on --tray startup)."""
        default_name = self._read_default_gpu_profile_name()
        if not default_name:
            self._log("ℹ️ Autostart: no default profile set — nothing to apply.")
            return
        self._log(f"⚙️ Autostart: applying default profile '{default_name}'…")
        self._run_root_helper_command(
            {"op": "run_gpu_autoload"},
            f"Default profile '{default_name}' applied on startup.",
            "Autostart apply failed.",
            callback=self._after_apply_read,
        )

    # ─── Autostart (.desktop in ~/.config/autostart) ─────────────────
    def _on_autostart_toggled(self, checked):
        try:
            if checked:
                self._write_autostart_entry()
                self._log("✅ Autostart enabled (launches in tray on login).")
            else:
                if os.path.isfile(AUTOSTART_FILE):
                    os.remove(AUTOSTART_FILE)
                self._log("➖ Autostart disabled.")
        except OSError as e:
            self._log(f"⚠️ Could not update autostart: {e}")
            # revert the checkbox to reflect real state
            self.chk_autostart.blockSignals(True)
            self.chk_autostart.setChecked(os.path.isfile(AUTOSTART_FILE))
            self.chk_autostart.blockSignals(False)

    def _write_autostart_entry(self):
        os.makedirs(AUTOSTART_DIR, exist_ok=True)
        # Prefer the installed launcher; fall back to running this file.
        launcher = "/usr/local/bin/nvcurve-gui"
        if os.path.exists(launcher):
            exec_cmd = f"{launcher} --tray"
        else:
            script = os.path.abspath(__file__)
            exec_cmd = f"{sys.executable} {script} --tray"
        content = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=nvcurve-gui\n"
            "Comment=Apply default GPU V/F curve profile and sit in the tray\n"
            f"Exec={exec_cmd}\n"
            "Icon=nvcurve-gui\n"
            "Terminal=false\n"
            "X-GNOME-Autostart-enabled=true\n"
        )
        with open(AUTOSTART_FILE, "w", encoding="utf-8") as f:
            f.write(content)

    def closeEvent(self, event):
        # Close-to-tray: the X button hides to the tray instead of quitting,
        # unless the user chose Quit from the tray menu (or there's no tray).
        if not self._force_quit and self.tray_icon is not None and self.tray_icon.isVisible():
            event.ignore()
            self.hide()
            self.tray_icon.showMessage(
                "nvcurve-gui",
                "Still running in the tray — right-click the icon for profiles.",
                self._make_tray_icon(), 2500,
            )
            return
        try:
            if self.nvml_available and self._nvml is not None:
                self._nvml.nvmlShutdown()
        except Exception:
            pass
        if self.tray_icon is not None:
            self.tray_icon.hide()
        super().closeEvent(event)


def _build_icon_pixmap(size=256):
    """Draws the nvcurve-gui app icon at runtime (no external asset needed).

    Concept: a GPU die viewed from above — a rounded chip body with
    contact pins on two edges, and a glowing V/F curve arcing across the
    die itself, rising through a couple of voltage/frequency "step"
    nodes towards a small spark/bolt accent at the peak. Reads as both
    "GPU" and "curve tuning" at a glance, distinct from a generic
    monitor/display icon.
    """
    from PySide6.QtGui import QPixmap, QPainterPath, QLinearGradient, QRadialGradient
    from PySide6.QtCore import QRectF

    s = size / 64.0  # scale factor from the 64px design grid
    px = QPixmap(size, size)
    px.fill(QColor(0, 0, 0, 0))
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing)

    def r(v):
        return v * s

    # Background tile: rounded square with a subtle vertical gradient so
    # it doesn't read as flat as the old solid-fill tile.
    bg_grad = QLinearGradient(0, 0, 0, r(64))
    bg_grad.setColorAt(0.0, QColor(C_BG3))
    bg_grad.setColorAt(1.0, QColor(C_BG))
    p.setBrush(QBrush(bg_grad))
    p.setPen(QPen(QColor(C_BORDER), r(2)))
    p.drawRoundedRect(QRectF(r(3), r(3), r(58), r(58)), r(14), r(14))

    # Chip die: a smaller rounded square inset within the tile.
    die_rect = QRectF(r(15), r(15), r(34), r(34))
    p.setBrush(QColor(C_BORDER))
    p.setPen(QPen(QColor(C_VDGREY), r(1.5)))
    p.drawRoundedRect(die_rect, r(6), r(6))

    # Contact pins along the top and bottom edges of the die, echoing a
    # real GPU package without needing fine per-pixel detail.
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(C_GREY))
    pin_xs = [r(20), r(26), r(32), r(38), r(44)]
    for x in pin_xs:
        p.drawRect(QRectF(x, r(10), r(2.2), r(6)))
        p.drawRect(QRectF(x, r(48), r(2.2), r(6)))

    # The V/F curve itself, rising across the die like a step/ramp
    # profile — this is the part that says "curve tuning", not just
    # "GPU".
    path = QPainterPath()
    path.moveTo(r(19), r(38))
    path.lineTo(r(26), r(38))
    path.lineTo(r(30), r(28))
    path.lineTo(r(37), r(28))
    path.lineTo(r(41), r(18))
    path.lineTo(r(46), r(18))
    pen = QPen(QColor(C_ORANGE), r(3.2))
    pen.setJoinStyle(Qt.RoundJoin)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawPath(path)

    # Node dots at each step of the curve.
    p.setPen(Qt.NoPen)
    for pt, color in (
        (QPointF(r(19), r(38)), C_CYAN),
        (QPointF(r(30), r(28)), C_YELLOW),
        (QPointF(r(41), r(18)), C_YELLOW),
        (QPointF(r(46), r(18)), C_GREEN),
    ):
        p.setBrush(QColor(color))
        p.drawEllipse(pt, r(2.6), r(2.6))

    # A small glowing spark above the peak node — the "overclocked"
    # accent that distinguishes this from a plain line chart icon.
    glow = QRadialGradient(QPointF(r(46), r(12)), r(7))
    glow.setColorAt(0.0, QColor(C_YELLOW).lighter(130))
    glow.setColorAt(1.0, QColor(255, 255, 255, 0))
    p.setBrush(QBrush(glow))
    p.setPen(Qt.NoPen)
    p.drawEllipse(QPointF(r(46), r(12)), r(7), r(7))
    bolt = QPainterPath()
    bolt.moveTo(r(47.5), r(7))
    bolt.lineTo(r(44), r(12.5))
    bolt.lineTo(r(46.5), r(12.5))
    bolt.lineTo(r(44.5), r(17.5))
    bolt.lineTo(r(49), r(11))
    bolt.lineTo(r(46.5), r(11))
    bolt.closeSubpath()
    p.setBrush(QColor(C_WHITE))
    p.drawPath(bolt)

    p.end()
    return px


def _make_app_icon():
    """Multi-resolution QIcon for the window/taskbar (built once at startup;
    same design as the tray icon, rendered at a size that stays crisp at
    typical taskbar/alt-tab sizes)."""
    icon = QIcon()
    for sz in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(_build_icon_pixmap(sz))
    return icon


def main():
    # Single-instance check happens FIRST, before QApplication or any Qt
    # object exists at all — this is plain OS file locking, so it can
    # never interfere with the platform plugin's native startup (which
    # is what handles X11/Wayland tray & status-notifier registration).
    guard = SingleInstanceGuard()
    is_primary = guard.try_acquire()

    app = QApplication(sys.argv)
    app.setApplicationName("nvcurve-gui")

    if not is_primary:
        # Another instance is already running — ask it to raise its
        # window and exit. We still need a QApplication just to run the
        # local-socket ping below.
        guard.notify_running_instance()
        return

    apply_theme(app)
    # Hiding the window to the tray must not quit the app.
    app.setQuitOnLastWindowClosed(False)
    win = NVCurveWindow()
    win.setWindowIcon(_make_app_icon())
    app.setWindowIcon(win.windowIcon())
    # --tray (autostart on login): start hidden and let _setup_tray()'s
    # retry loop decide. It keeps the window hidden while the tray is
    # still becoming available, and only calls win.show() itself as a
    # last resort if the tray never shows up after retrying. Deciding
    # this here instead, right after construction, would race with
    # those retries and could show the window even though the tray
    # ends up available a moment later.
    if "--tray" not in sys.argv:
        win.show()

    # Only now — window constructed, tray attempted — open the "raise my
    # window" IPC channel for future launches to ping.
    guard.start_ipc_server()
    win.attach_single_instance_guard(guard)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
