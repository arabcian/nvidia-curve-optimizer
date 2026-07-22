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

import json
import os
import subprocess
import sys
import time

from PySide6.QtCore import Qt, QTimer, Signal, QThread, QPointF
from PySide6.QtGui import QPainter, QColor, QFont, QBrush, QKeyEvent
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGroupBox, QLabel, QPushButton, QComboBox, QSpinBox, QSlider,
    QSizePolicy, QMessageBox, QInputDialog,
)
from PySide6.QtCharts import QChart, QChartView, QLineSeries, QScatterSeries, QValueAxis

# ─── Theme ───────────────────────────────────────────────────────────────
C_BG = "#1d2021"
C_BG3 = "#3c3836"
C_BORDER = "#504945"
C_VDGREY = "#665c54"
C_GREY = "#a89984"
C_LIGHT = "#ebdbb2"
C_WHITE = "#fbf1c7"
C_CYAN = "#83a598"
C_ORANGE = "#fe8019"
C_YELLOW = "#fabd2f"
C_GREEN = "#b8bb26"
C_STOP = "#fb4934"

ROOT_HELPER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "root_helper.py")
NVCURVE_PROFILES_DIR = "/etc/nvcurve/profiles"
NVCURVE_CONFIG_PATH = "/etc/nvcurve/config.json"
RUN_DIR = "/run/nvcurve-gui"


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
        self.chart.setBackgroundBrush(QColor(10, 10, 10))
        self.chart.setBackgroundVisible(True)
        self.setChart(self.chart)
        self._create_series()
        self.setInteractive(True)
        self.setMinimumHeight(300)

    def _create_series(self):
        self.chart.removeAllSeries()
        self.line_series = QLineSeries()
        for v, f in self.points:
            self.line_series.append(v, f)
        self.chart.addSeries(self.line_series)

        self.scatter_series = QScatterSeries()
        self.scatter_series.setMarkerSize(7)
        self.scatter_series.setColor(QColor(C_CYAN))
        self.scatter_series.setBorderColor(QColor(C_ORANGE))
        self.scatter_series.clicked.connect(self._on_point_clicked)
        for v, f in self.points:
            self.scatter_series.append(v, f)
        self.chart.addSeries(self.scatter_series)

        self.selected_series = QScatterSeries()
        self.selected_series.setMarkerSize(10)
        self.selected_series.setColor(QColor(C_YELLOW))
        self.selected_series.setBorderColor(QColor(C_WHITE))
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
        self.axis_x.setLabelsColor(QColor(C_LIGHT))
        self.axis_x.setTitleBrush(QBrush(QColor(C_LIGHT)))
        self.chart.addAxis(self.axis_x, Qt.AlignBottom)
        self.line_series.attachAxis(self.axis_x)
        self.scatter_series.attachAxis(self.axis_x)
        self.selected_series.attachAxis(self.axis_x)

        self.axis_y = QValueAxis()
        self.axis_y.setTitleText("Frequency (MHz)")
        self.axis_y.setRange(800, 3000)
        self.axis_y.setTickCount(12)
        self.axis_y.setLabelsColor(QColor(C_LIGHT))
        self.axis_y.setTitleBrush(QBrush(QColor(C_LIGHT)))
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
        self.setStyleSheet(f"background:{C_BG}; color:{C_LIGHT};")

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

        # Auto-read the current V/F curve once on startup so the graph
        # reflects real hardware state immediately instead of the
        # placeholder default_points. Deferred with singleShot so the
        # window is shown first — the pkexec auth prompt this triggers
        # then appears over an already-visible window rather than before
        # anything is drawn.
        QTimer.singleShot(0, self._read_gpu_curve)

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
        self.gpu_temp = SL("Temp: -- °C", color=C_GREY)
        self.gpu_mem_temp = SL("Mem Temp: -- °C", color=C_GREY)
        self.gpu_hotspot_temp = SL("Hotspot: -- °C", color=C_GREY)
        self.gpu_power = SL("Power: -- W", color=C_GREY)
        self.gpu_clock = SL("Clock: -- MHz", color=C_GREY)
        self.gpu_mem_clock = SL("Mem Clock: -- MHz", color=C_GREY)
        for w in (self.gpu_temp, self.gpu_mem_temp, self.gpu_hotspot_temp,
                  self.gpu_power, self.gpu_clock, self.gpu_mem_clock):
            info_layout.addWidget(w)
        info_layout.addStretch()

        self.btn_save_profile = QPushButton("💾 Save As")
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
        profile_toolbar.addStretch()
        self.btn_set_default_profile = QPushButton("⭐ Default")
        self.btn_set_default_profile.setFixedHeight(24)
        self.btn_set_default_profile.setFixedWidth(100)
        self.btn_set_default_profile.clicked.connect(self._toggle_default_gpu_profile)
        profile_toolbar.addWidget(self.btn_set_default_profile)
        self.btn_delete_profile = QPushButton("🗑 Delete")
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

        self.point_index_label = SL("Selected: -", color=C_GREY)
        self.point_voltage_label = SL("Voltage: - mV", color=C_GREY)
        self.point_freq_label = SL("Freq: - MHz", color=C_GREY)
        self.point_offset_label = SL("Offset: - MHz", color=C_GREY)
        self.point_offset_spin = QSpinBox()
        self.point_offset_spin.setRange(-1000, 1000)
        self.point_offset_spin.setValue(0)
        self.point_offset_spin.setSuffix(" MHz")
        self.point_offset_spin.setFixedWidth(90)
        self.point_offset_spin.valueChanged.connect(self._on_point_offset_spin_changed)
        self.point_offset_spin.setEnabled(False)

        for lbl in (self.point_index_label, self.point_voltage_label,
                    self.point_freq_label, self.point_offset_label):
            lbl.setFixedWidth(100)

        info_panel_layout.addWidget(self.point_index_label)
        info_panel_layout.addWidget(self.point_voltage_label)
        info_panel_layout.addWidget(self.point_freq_label)
        info_panel_layout.addWidget(self.point_offset_label)
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
        self.btn_select_all.setFixedHeight(24)
        self.btn_select_all.clicked.connect(self.vf_widget.select_all)
        bottom_buttons_layout.addWidget(self.btn_select_all)
        self.btn_reset_graph = QPushButton("↺ Reset Graph")
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
        self.mem_offset_spin.setRange(-1500, 1500)
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
        self.btn_vram_lock.setFixedHeight(24)
        self.btn_vram_lock.setFixedWidth(34)
        self.btn_vram_lock.setToolTip("Lock VRAM clock to Min–Max now")
        self.btn_vram_lock.clicked.connect(self._apply_vram_memlock)
        control_layout.addWidget(self.btn_vram_lock)
        self.btn_vram_unlock = QPushButton("🔓")
        self.btn_vram_unlock.setFixedHeight(24)
        self.btn_vram_unlock.setFixedWidth(34)
        self.btn_vram_unlock.setToolTip("Unlock VRAM clock")
        self.btn_vram_unlock.clicked.connect(self._reset_vram_memlock)
        control_layout.addWidget(self.btn_vram_unlock)

        control_layout.addStretch()

        self.btn_read_curve = QPushButton("📥 Read Current Curve")
        self.btn_read_curve.setFixedHeight(24)
        self.btn_read_curve.clicked.connect(self._read_gpu_curve)
        control_layout.addWidget(self.btn_read_curve)
        self.btn_apply_offset = QPushButton("🚀 Apply Offsets")
        self.btn_apply_offset.setFixedHeight(24)
        self.btn_apply_offset.clicked.connect(self._apply_gpu_offsets)
        control_layout.addWidget(self.btn_apply_offset)
        self.btn_reset_curve = QPushButton("↺ Reset Curve")
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
        worker = RootHelperWorker(payload_dict, parent=self)
        self._workers.append(worker)

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

        def on_err(err):
            self._log(f"✘ {fail_msg}: {err}")
            if callback:
                callback(None)
            self._workers.remove(worker)

        worker.finished_ok.connect(on_ok)
        worker.finished_err.connect(on_err)
        worker.start()

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
        self._read_offsets = self._point_offsets.copy()
        self._read_core_offset = self._core_offset

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
        mem_off_nvml = mem_off * 2
        vram_lock_max = self.vram_lock_max_spin.value()
        vram_lock_min = self.vram_lock_min_spin.value()
        if vram_lock_max != 0 and vram_lock_min == 0:
            vram_lock_min = vram_lock_max
        return {
            "name": "custom",
            "curve_deltas": offsets,
            "mem_offset_mhz": mem_off_nvml,
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
            mem_offset = mem_offset_raw // 2
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

        self._read_core_offset = self._core_offset
        self._read_offsets = self._point_offsets.copy() if self._point_offsets else {}

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
        profile_name = "ppm"
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
        mem_off_nvml = mem_off * 2

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
            "mem_offset_mhz": mem_off_nvml if mem_off != 0 else None,
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

    def closeEvent(self, event):
        try:
            if self.nvml_available and self._nvml is not None:
                self._nvml.nvmlShutdown()
        except Exception:
            pass
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("nvcurve-gui")
    win = NVCurveWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
