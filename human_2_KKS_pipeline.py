from __future__ import annotations

import random
import re
import shutil
import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import wave
from collections import deque
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

import numpy as np
import sounddevice as sd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from voice_gate_recorder import RecorderConfig, get_input_devices

from PyQt6.QtCore import QObject, QThread, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QAction, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemDelegate, QStyledItemDelegate,
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QMenu, QPlainTextEdit, QPushButton, QScrollArea, QSlider, QSpinBox,
    QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from config.constants import DEFAULT_SOURCE_MODE, MUTEX_NAME
from config.models import AppConfig
from controllers.runtime_controller import (
    apply_live_settings as _apply_live_settings_impl,
    deferred_live_fields as _deferred_live_fields_impl,
    on_any_setting_changed as _on_any_setting_changed_impl,
    on_live_setting_changed as _on_live_setting_changed_impl,
)
from core.io_utils import (
    last_json_line as _last_json_line,
    save_text as _save_text,
    wav_duration_sec as _wav_duration_sec,
    with_utf8_env as _with_utf8_env,
)
from entrypoints.gui_entry import run_gui
from workers.recorder_worker import RecorderWorker
from workers.thread_workers import SeleniumWorker as _SeleniumWorker, TaskWorker as _TaskWorker

CONFIG_FILE = Path(__file__).resolve().parent / "config.json"


class _ConversionTableDelegate(QStyledItemDelegate):
    request_new_row = pyqtSignal()

    def eventFilter(self, editor, event):
        if event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Tab and not (
                event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier)
            ):
                table = self.parent()
                if isinstance(table, QTableWidget):
                    row = table.currentRow()
                    col = table.currentColumn()
                    if row == table.rowCount() - 1 and col == 1:
                        self.commitData.emit(editor)
                        self.closeEditor.emit(editor, QAbstractItemDelegate.EndEditHint.NoHint)
                        self.request_new_row.emit()
                        return True
        return super().eventFilter(editor, event)


# ワーカー: パイプライン（WAV監視 → 文字起こし → フィルター → Grok → SBV2 → KKS）
# ---------------------------------------------------------------------------

from workers.pipeline_worker import PipelineWorker
from PyQt6.QtCore import QEvent

class _NoWheelMixin:
    def wheelEvent(self, event):
        event.ignore()

class _NoWheelSpinBox(_NoWheelMixin, __import__('PyQt6.QtWidgets', fromlist=['QSpinBox']).QSpinBox): pass
class _NoWheelDoubleSpinBox(_NoWheelMixin, __import__('PyQt6.QtWidgets', fromlist=['QDoubleSpinBox']).QDoubleSpinBox): pass
class _NoWheelComboBox(_NoWheelMixin, __import__('PyQt6.QtWidgets', fromlist=['QComboBox']).QComboBox): pass

class _NoWheelPortSpinBox(_NoWheelSpinBox):
    def wheelEvent(self, event):
        event.ignore()

class _NoWheelAlwaysSpinBox(_NoWheelSpinBox):
    def wheelEvent(self, event):
        event.ignore()

class _NoWheelAlwaysDoubleSpinBox(_NoWheelDoubleSpinBox):
    def wheelEvent(self, event):
        event.ignore()

class _NoWheelAlwaysComboBox(_NoWheelComboBox):
    def wheelEvent(self, event):
        event.ignore()


# ---------------------------------------------------------------------------
# メインウィンドウ
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Human → KKS Pipeline")
        self.resize(970, 720)

        self._recorder_thread: Optional[QThread] = None
        self._recorder_worker: Optional[RecorderWorker] = None
        self._pipeline_thread: Optional[QThread] = None
        self._pipeline_worker: Optional[PipelineWorker] = None
        self._running = False
        self._paused = False
        self._active_runtime_cfg: Optional[AppConfig] = None
        self._pending_cfg: Optional[AppConfig] = None
        self._last_deferred_live_fields: tuple[str, ...] = tuple()
        self._fw_test_stream = None
        self._fw_test_chunks: list[np.ndarray] = []
        self._fw_test_sr = 16000
        self._fw_test_worker: Optional[_TaskWorker] = None
        self._sbv2_test_worker: Optional[_TaskWorker] = None
        self._sbv2_test_last_wav: Optional[Path] = None

        self._manual_history: list[str] = []
        self._model_presets: list[dict] = []
        self._loading_config = False
        self._clipboard_shortcuts: list[QShortcut] = []
        self._conversion_table_delegate: Optional[_ConversionTableDelegate] = None

        self._build_ui()
        self._load_config()
        self._install_autosave_hooks()
        self._install_clipboard_support()

    # ---- UI構築 ----

    def _build_ui(self) -> None:
        root = QWidget(self)
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        self._build_recorder_tab()
        self._build_pipeline_tab()
        self._build_test_tab()
        self._build_selenium_tab()
        self._build_transcribe_conversion_tab()
        self._build_filter_tab()
        self._build_conversion_tab()

        # コントロールボタン
        ctrl = QHBoxLayout()
        self.start_btn = QPushButton("▶ 開始")
        self.start_btn.setFixedHeight(44)
        self.start_btn.clicked.connect(self._on_start_stop)
        self.pause_btn = QPushButton("⏸ 一時停止")
        self.pause_btn.setFixedHeight(44)
        self.pause_btn.setEnabled(False)
        self.pause_btn.clicked.connect(self._on_pause_resume)
        ctrl.addWidget(self.start_btn, 2)
        ctrl.addWidget(self.pause_btn, 1)
        layout.addLayout(ctrl)

        # モデルプリセット クイックボタン
        preset_btn_layout = QHBoxLayout()
        self._preset_btns: list[QPushButton] = []
        for i in range(4):
            btn = QPushButton(f"--- ({i+1})")
            btn.setFixedHeight(32)
            btn.setEnabled(False)
            idx = i
            btn.clicked.connect(lambda _, n=idx: self._apply_preset(n))
            preset_btn_layout.addWidget(btn)
            self._preset_btns.append(btn)
        layout.addLayout(preset_btn_layout)

        # 手動テキスト送信
        manual_group = QGroupBox("手動テキスト送信")
        manual_layout = QHBoxLayout(manual_group)
        self.manual_combo = _NoWheelComboBox()
        self.manual_combo.setEditable(True)
        self.manual_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.manual_combo.lineEdit().setPlaceholderText("テキストを入力して送信...")
        self.manual_combo.lineEdit().returnPressed.connect(self._send_manual)
        self.manual_btn = QPushButton("送信")
        self.manual_btn.clicked.connect(self._send_manual)
        manual_layout.addWidget(self.manual_combo, 1)
        manual_layout.addWidget(self.manual_btn)
        layout.addWidget(manual_group)

        # ログ
        self.log_text = QPlainTextEdit()
        self.log_text.setReadOnly(True)
        layout.addWidget(self.log_text, 1)

    def _build_recorder_tab(self) -> None:
        tab = QWidget()
        self.tabs.addTab(tab, "録音設定")
        form = QFormLayout(tab)

        device_row = QHBoxLayout()
        self.device_combo = _NoWheelAlwaysComboBox()
        refresh_btn = QPushButton("更新")
        refresh_btn.clicked.connect(self._reload_devices)
        device_row.addWidget(self.device_combo, 1)
        device_row.addWidget(refresh_btn)
        form.addRow("入力デバイス", device_row)

        self.wav_dir_edit = QLineEdit(str(Path(__file__).resolve().parent / "outputs" / "wav"))
        wav_btn = QPushButton("参照")
        wav_btn.clicked.connect(lambda: self._pick_dir(self.wav_dir_edit, "WAV保存先"))
        form.addRow("WAV保存先", self._hrow(self.wav_dir_edit, wav_btn))

        self.threshold_spin = _NoWheelAlwaysDoubleSpinBox()
        self.threshold_spin.setRange(-90.0, 0.0); self.threshold_spin.setValue(-35.0)
        self.threshold_spin.setDecimals(1); self.threshold_spin.setSuffix(" dBFS")
        form.addRow("閾値", self.threshold_spin)

        self.silence_spin = _NoWheelAlwaysDoubleSpinBox()
        self.silence_spin.setRange(0.1, 10.0); self.silence_spin.setValue(2.0)
        self.silence_spin.setDecimals(1); self.silence_spin.setSuffix(" 秒")
        form.addRow("無音停止秒数", self.silence_spin)

        self.min_dur_spin = _NoWheelAlwaysDoubleSpinBox()
        self.min_dur_spin.setRange(0.1, 30.0); self.min_dur_spin.setValue(3.0)
        self.min_dur_spin.setDecimals(1); self.min_dur_spin.setSuffix(" 秒")
        form.addRow("最小保存秒数", self.min_dur_spin)

        self.pre_roll_spin = _NoWheelAlwaysDoubleSpinBox()
        self.pre_roll_spin.setRange(0.0, 5.0); self.pre_roll_spin.setValue(0.5)
        self.pre_roll_spin.setDecimals(1); self.pre_roll_spin.setSuffix(" 秒")
        form.addRow("開始前バッファ", self.pre_roll_spin)

        self.post_roll_spin = _NoWheelAlwaysDoubleSpinBox()
        self.post_roll_spin.setRange(0.0, 5.0); self.post_roll_spin.setValue(0.5)
        self.post_roll_spin.setDecimals(1); self.post_roll_spin.setSuffix(" 秒")
        form.addRow("停止後余韻", self.post_roll_spin)

        self._reload_devices()

    def _build_pipeline_tab(self) -> None:
        inner = QWidget()
        inner.setMinimumWidth(0)
        scroll = QScrollArea()
        scroll.setWidget(inner)
        scroll.setWidgetResizable(True)
        from PyQt6.QtCore import Qt
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tabs.addTab(scroll, "パイプライン設定")
        form = QFormLayout(inner)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)

        self.kks_root_edit = QLineEdit()
        kks_btn = QPushButton("参照")
        kks_btn.clicked.connect(lambda: self._pick_dir(self.kks_root_edit, "KKSフォルダ"))
        form.addRow("KKSフォルダ", self._hrow(self.kks_root_edit, kks_btn))

        self.output_dir_edit = QLineEdit(str(Path(__file__).resolve().parent / "outputs"))
        out_btn = QPushButton("参照")
        out_btn.clicked.connect(lambda: self._pick_dir(self.output_dir_edit, "出力先"))
        form.addRow("出力先", self._hrow(self.output_dir_edit, out_btn))

        _local_py = str(Path(__file__).resolve().parent / "python" / "python.exe")
        self.faster_python_edit = QLineEdit(_local_py)
        fp_btn = QPushButton("参照")
        fp_btn.clicked.connect(lambda: self._pick_file(self.faster_python_edit, "FasterWhisper Python"))
        form.addRow("FasterWhisper Python", self._hrow(self.faster_python_edit, fp_btn))

        whisper_row = QHBoxLayout()
        self.faster_model_edit = _NoWheelComboBox()
        self.faster_model_edit.setEditable(True)
        self.faster_model_edit.addItems(["large-v3", "large-v2", "large", "medium", "small", "base", "tiny"])
        self.faster_model_edit.setCurrentText("large-v3")
        self.faster_device_combo = _NoWheelComboBox()
        self.faster_device_combo.addItems(["auto", "cuda", "cpu"])
        self.faster_compute_combo = _NoWheelComboBox()
        self.faster_compute_combo.addItems(["int8_float16", "float16", "int8", "float32"])
        self.faster_lang_edit = QLineEdit("ja")
        self.faster_lang_edit.setMaximumWidth(40)
        self.faster_beam_spin = _NoWheelSpinBox()
        self.faster_beam_spin.setRange(1, 10); self.faster_beam_spin.setValue(1)
        whisper_row.addWidget(QLabel("model")); whisper_row.addWidget(self.faster_model_edit)
        whisper_row.addWidget(self.faster_device_combo)
        whisper_row.addWidget(self.faster_compute_combo)
        whisper_row.addWidget(QLabel("lang")); whisper_row.addWidget(self.faster_lang_edit)
        whisper_row.addWidget(QLabel("beam")); whisper_row.addWidget(self.faster_beam_spin)
        w = QWidget(); w.setLayout(whisper_row)
        form.addRow("Whisper", w)

        self.pipeline_python_edit = QLineEdit(_local_py)
        pp_btn = QPushButton("参照")
        pp_btn.clicked.connect(lambda: self._pick_file(self.pipeline_python_edit, "Grok/TTS Python"))
        form.addRow("Grok/TTS Python", self._hrow(self.pipeline_python_edit, pp_btn))

        self.sbv2_root_edit = QLineEdit()
        sbv2_btn = QPushButton("参照")
        sbv2_btn.clicked.connect(lambda: self._pick_dir(self.sbv2_root_edit, "SBV2フォルダ"))
        form.addRow("SBV2フォルダ", self._hrow(self.sbv2_root_edit, sbv2_btn))

        self.video_metadata_edit = QLineEdit("")
        meta_btn = QPushButton("参照")
        meta_btn.clicked.connect(lambda: self._pick_file(self.video_metadata_edit, "動画メタデータJSON"))
        form.addRow("動画メタデータJSON", self._hrow(self.video_metadata_edit, meta_btn))

        sbv2_server_row = QHBoxLayout()
        self.sbv2_server_url_edit = QLineEdit("http://127.0.0.1:5000")
        self.sbv2_auto_start_chk = QCheckBox("自動起動")
        self.sbv2_auto_start_chk.setChecked(True)
        sbv2_server_row.addWidget(self.sbv2_server_url_edit, 1)
        sbv2_server_row.addWidget(self.sbv2_auto_start_chk)
        sbv2_server_w = QWidget(); sbv2_server_w.setLayout(sbv2_server_row)
        form.addRow("SBV2サーバーURL", sbv2_server_w)

        model_row = QHBoxLayout()
        self.model_name_combo = _NoWheelComboBox(); self.model_name_combo.setEditable(True)
        self.model_file_edit = _NoWheelComboBox(); self.model_file_edit.setEditable(True)
        self.model_file_edit.lineEdit().setPlaceholderText("checkpoint file (空=auto)")
        model_refresh_btn = QPushButton("更新")
        model_refresh_btn.clicked.connect(self._reload_models)
        self.model_name_combo.currentTextChanged.connect(self._reload_model_files)
        model_row.addWidget(self.model_name_combo, 1); model_row.addWidget(self.model_file_edit, 1)
        model_row.addWidget(model_refresh_btn)
        m = QWidget(); m.setLayout(model_row)
        form.addRow("SBV2モデル", m)

        # モデルプリセット管理
        preset_row = QHBoxLayout()
        self.preset_name_edit = QLineEdit()
        self.preset_name_edit.setPlaceholderText("プリセット名")
        preset_save_btn = QPushButton("保存")
        preset_save_btn.clicked.connect(self._save_preset)
        self.preset_list_combo = _NoWheelComboBox()
        preset_apply_btn = QPushButton("適用")
        preset_apply_btn.clicked.connect(self._apply_preset_from_combo)
        preset_del_btn = QPushButton("削除")
        preset_del_btn.clicked.connect(self._delete_preset)
        preset_row.addWidget(self.preset_name_edit, 2)
        preset_row.addWidget(preset_save_btn)
        preset_row.addWidget(self.preset_list_combo, 2)
        preset_row.addWidget(preset_apply_btn)
        preset_row.addWidget(preset_del_btn)
        pr = QWidget(); pr.setLayout(preset_row)
        form.addRow("モデルプリセット", pr)

        opt_row = QHBoxLayout()
        self.speaker_edit = QLineEdit("0")
        self.style_edit = QLineEdit("Neutral")
        self.length_spin = _NoWheelAlwaysDoubleSpinBox()
        self.length_spin.setRange(0.1, 3.0); self.length_spin.setValue(1.0); self.length_spin.setDecimals(2)
        self.voice_volume_spin = _NoWheelAlwaysDoubleSpinBox()
        self.voice_volume_spin.setRange(-1.0, 1.0); self.voice_volume_spin.setValue(-1.0); self.voice_volume_spin.setDecimals(2)
        self.voice_pitch_spin = _NoWheelAlwaysDoubleSpinBox()
        self.voice_pitch_spin.setRange(-1.0, 3.0); self.voice_pitch_spin.setValue(-1.0); self.voice_pitch_spin.setDecimals(2)
        self.pipe_edit = QLineEdit("kks_voice_face_events")
        self.main_spin = _NoWheelAlwaysSpinBox(); self.main_spin.setRange(0, 3)
        self.face_spin = _NoWheelAlwaysSpinBox(); self.face_spin.setRange(-1, 500); self.face_spin.setValue(-1)
        self.keep_face_chk = QCheckBox("現在表情維持"); self.keep_face_chk.setChecked(True)
        for label, widget in [("speaker", self.speaker_edit), ("style", self.style_edit),
                               ("length", self.length_spin), ("vol", self.voice_volume_spin),
                               ("pitch", self.voice_pitch_spin), ("pipe", self.pipe_edit),
                               ("main", self.main_spin), ("face", self.face_spin)]:
            opt_row.addWidget(QLabel(label)); opt_row.addWidget(widget)
        opt_row.addWidget(self.keep_face_chk)
        o = QWidget(); o.setLayout(opt_row)
        form.addRow("送信設定", o)

        net_row = QHBoxLayout()
        self.target_host_edit = QLineEdit()
        self.target_host_edit.setPlaceholderText("空欄=ローカルPipe")
        self.target_port_spin = _NoWheelPortSpinBox()
        self.target_port_spin.setRange(1, 65535); self.target_port_spin.setValue(18765)
        self.target_endpoint_edit = QLineEdit("/voice-face-event")
        self.target_token_edit = QLineEdit()
        self.target_token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.remote_http_chk = QCheckBox("HTTP強制")
        net_row.addWidget(QLabel("host")); net_row.addWidget(self.target_host_edit, 2)
        net_row.addWidget(QLabel("port")); net_row.addWidget(self.target_port_spin)
        net_row.addWidget(QLabel("endpoint")); net_row.addWidget(self.target_endpoint_edit)
        net_row.addWidget(QLabel("token")); net_row.addWidget(self.target_token_edit)
        net_row.addWidget(self.remote_http_chk)
        net = QWidget(); net.setLayout(net_row)
        form.addRow("LAN送信", net)

        sub_row = QHBoxLayout()
        self.subtitle_send_chk = QCheckBox("字幕送信"); self.subtitle_send_chk.setChecked(True)
        self.subtitle_host_edit = QLineEdit("127.0.0.1")
        self.subtitle_port_spin = _NoWheelPortSpinBox()
        self.subtitle_port_spin.setRange(1, 65535); self.subtitle_port_spin.setValue(18766)
        self.subtitle_endpoint_edit = QLineEdit("/subtitle-event")
        self.subtitle_token_edit = QLineEdit()
        self.subtitle_token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.subtitle_timeout_spin = _NoWheelSpinBox()
        self.subtitle_timeout_spin.setRange(1, 30); self.subtitle_timeout_spin.setValue(5)
        sub_row.addWidget(self.subtitle_send_chk)
        sub_row.addWidget(QLabel("host")); sub_row.addWidget(self.subtitle_host_edit, 2)
        sub_row.addWidget(QLabel("port")); sub_row.addWidget(self.subtitle_port_spin)
        sub_row.addWidget(QLabel("endpoint")); sub_row.addWidget(self.subtitle_endpoint_edit)
        sub_row.addWidget(QLabel("token")); sub_row.addWidget(self.subtitle_token_edit)
        sub_row.addWidget(QLabel("timeout")); sub_row.addWidget(self.subtitle_timeout_spin)
        sub = QWidget(); sub.setLayout(sub_row)
        form.addRow("字幕送信", sub)

        ext_row = QHBoxLayout()
        self.source_mode_combo = _NoWheelComboBox()
        self.source_mode_combo.addItems(["external", "mic", "both"])
        self.source_mode_combo.setCurrentText(DEFAULT_SOURCE_MODE)
        self.external_text_chk = QCheckBox("外部受信")
        self.external_text_chk.setChecked(True)
        self.external_text_host_edit = QLineEdit("127.0.0.1")
        self.external_text_port_spin = _NoWheelPortSpinBox()
        self.external_text_port_spin.setRange(1, 65535); self.external_text_port_spin.setValue(18767)
        self.external_text_endpoint_edit = QLineEdit("/manual-text")
        self.external_text_token_edit = QLineEdit()
        self.external_text_token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.external_text_dedupe_spin = _NoWheelSpinBox()
        self.external_text_dedupe_spin.setRange(10, 10000); self.external_text_dedupe_spin.setValue(1024)
        ext_row.addWidget(QLabel("mode")); ext_row.addWidget(self.source_mode_combo)
        ext_row.addWidget(self.external_text_chk)
        ext_row.addWidget(QLabel("host")); ext_row.addWidget(self.external_text_host_edit, 2)
        ext_row.addWidget(QLabel("port")); ext_row.addWidget(self.external_text_port_spin)
        ext_row.addWidget(QLabel("endpoint")); ext_row.addWidget(self.external_text_endpoint_edit)
        ext_row.addWidget(QLabel("token")); ext_row.addWidget(self.external_text_token_edit)
        ext_row.addWidget(QLabel("dedupe")); ext_row.addWidget(self.external_text_dedupe_spin)
        ext = QWidget(); ext.setLayout(ext_row)
        form.addRow("外部テキスト受信", ext)

    def _build_test_tab(self) -> None:
        tab = QWidget()
        self.tabs.addTab(tab, "テスト")
        layout = QVBoxLayout(tab)

        fw_group = QGroupBox("FasterWhisper テスト")
        fw_layout = QVBoxLayout(fw_group)
        fw_layout.addWidget(QLabel("ボタンを押している間だけ録音。離すと文字起こしします。"))
        self.fw_test_hold_btn = QPushButton("押して話す（離して判定）")
        self.fw_test_hold_btn.pressed.connect(self._fw_test_start_record)
        self.fw_test_hold_btn.released.connect(self._fw_test_stop_record)
        fw_layout.addWidget(self.fw_test_hold_btn)
        self.fw_test_status_label = QLabel("待機")
        fw_layout.addWidget(self.fw_test_status_label)
        self.fw_test_result_edit = QPlainTextEdit()
        self.fw_test_result_edit.setReadOnly(True)
        self.fw_test_result_edit.setPlaceholderText("ここに文字起こし結果が表示されます。")
        fw_layout.addWidget(self.fw_test_result_edit, 1)
        layout.addWidget(fw_group)

        sbv2_group = QGroupBox("SBV2 テスト")
        sbv2_layout = QVBoxLayout(sbv2_group)
        self.sbv2_test_text_edit = QPlainTextEdit()
        self.sbv2_test_text_edit.setPlaceholderText("SBV2で再生したいテキストを入力")
        sbv2_layout.addWidget(self.sbv2_test_text_edit)

        face_row = QHBoxLayout()
        self.sbv2_test_keep_face_chk = QCheckBox("現在表情維持")
        self.sbv2_test_keep_face_chk.setChecked(True)
        self.sbv2_test_keep_face_chk.toggled.connect(self._on_sbv2_test_keep_face_toggled)
        self.sbv2_test_face_spin = _NoWheelAlwaysSpinBox()
        self.sbv2_test_face_spin.setRange(-1, 500)
        self.sbv2_test_face_spin.setValue(-1)
        self.sbv2_test_face_spin.setEnabled(False)
        self.sbv2_test_face_spin.valueChanged.connect(self._on_sbv2_test_face_changed)
        face_row.addWidget(self.sbv2_test_keep_face_chk)
        face_row.addWidget(QLabel("face"))
        face_row.addWidget(self.sbv2_test_face_spin)
        face_row.addStretch()
        sbv2_layout.addLayout(face_row)

        vol_row = QHBoxLayout()
        vol_row.addWidget(QLabel("音量"))
        self.sbv2_test_volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.sbv2_test_volume_slider.setRange(0, 100)
        self.sbv2_test_volume_slider.setValue(100)
        self.sbv2_test_volume_slider.valueChanged.connect(self._on_sbv2_test_volume_changed)
        self.sbv2_test_volume_label = QLabel("100%")
        vol_row.addWidget(self.sbv2_test_volume_slider, 1)
        vol_row.addWidget(self.sbv2_test_volume_label)
        sbv2_layout.addLayout(vol_row)

        btn_row = QHBoxLayout()
        self.sbv2_test_run_btn = QPushButton("SBV2テスト実行")
        self.sbv2_test_run_btn.clicked.connect(self._run_sbv2_test)
        self.sbv2_test_play_btn = QPushButton("最後の音声をGUI再生")
        self.sbv2_test_play_btn.clicked.connect(self._play_last_sbv2_test)
        btn_row.addWidget(self.sbv2_test_run_btn)
        btn_row.addWidget(self.sbv2_test_play_btn)
        btn_row.addStretch()
        sbv2_layout.addLayout(btn_row)

        self.sbv2_test_status_label = QLabel("待機")
        sbv2_layout.addWidget(self.sbv2_test_status_label)
        layout.addWidget(sbv2_group, 1)

    def _on_sbv2_test_keep_face_toggled(self, checked: bool) -> None:
        self.sbv2_test_face_spin.setEnabled(not checked)

    def _on_sbv2_test_face_changed(self, value: int) -> None:
        if value >= 0 and self.sbv2_test_keep_face_chk.isChecked():
            self.sbv2_test_keep_face_chk.setChecked(False)

    def _on_sbv2_test_volume_changed(self, value: int) -> None:
        self.sbv2_test_volume_label.setText(f"{value}%")

    def _fw_test_audio_callback(self, indata, frames, time_info, status) -> None:
        if status:
            return
        self._fw_test_chunks.append(indata.copy())

    @staticmethod
    def _write_wav_float32_mono(path: Path, pcm: np.ndarray, sample_rate: int) -> None:
        pcm = np.asarray(pcm, dtype=np.float32).reshape(-1)
        clipped = np.clip(pcm, -1.0, 1.0)
        int16_pcm = (clipped * 32767.0).astype(np.int16)
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(int16_pcm.tobytes())

    def _fw_test_start_record(self) -> None:
        if self._fw_test_worker is not None and self._fw_test_worker.isRunning():
            self.fw_test_status_label.setText("文字起こし中...")
            return
        if self._fw_test_stream is not None:
            return
        self._fw_test_chunks = []
        device = self.device_combo.currentData()
        try:
            self._fw_test_stream = sd.InputStream(
                samplerate=self._fw_test_sr,
                channels=1,
                dtype="float32",
                device=device,
                callback=self._fw_test_audio_callback,
            )
            self._fw_test_stream.start()
            self.fw_test_hold_btn.setText("録音中（離して停止）")
            self.fw_test_status_label.setText("録音中...")
        except Exception as exc:
            self._fw_test_stream = None
            self.fw_test_hold_btn.setText("押して話す（離して判定）")
            self.fw_test_status_label.setText(f"録音失敗: {exc}")
            self._append_log(f"[fw-test] record start failed: {exc}")

    def _fw_test_stop_record(self) -> None:
        if self._fw_test_stream is None:
            return
        try:
            self._fw_test_stream.stop()
            self._fw_test_stream.close()
        except Exception:
            pass
        finally:
            self._fw_test_stream = None

        self.fw_test_hold_btn.setText("押して話す（離して判定）")
        if not self._fw_test_chunks:
            self.fw_test_status_label.setText("録音データなし")
            return

        pcm = np.concatenate(self._fw_test_chunks, axis=0).reshape(-1)
        self._fw_test_chunks = []
        duration = len(pcm) / float(self._fw_test_sr)
        if duration < 0.2:
            self.fw_test_status_label.setText("録音が短すぎます")
            return

        out_root = Path(self.output_dir_edit.text().strip()).expanduser().resolve() / "tests" / "fasterwhisper"
        wav_path = out_root / f"hold_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')[:-3]}.wav"
        self._write_wav_float32_mono(wav_path, pcm, self._fw_test_sr)
        self.fw_test_status_label.setText("文字起こし中...")
        self._append_log(f"[fw-test] recorded {wav_path.name} ({duration:.2f}s)")
        self._fw_test_worker = _TaskWorker(lambda: self._run_fw_test_transcribe(wav_path))
        self._fw_test_worker.result_ready.connect(self._on_fw_test_transcribe_done)
        self._fw_test_worker.error_occurred.connect(self._on_fw_test_transcribe_error)
        self._fw_test_worker.start()

    def _run_fw_test_transcribe(self, wav_path: Path) -> dict:
        cfg = self._build_config()
        script = Path(__file__).resolve().parent / "run_transcribe_one_wav.py"
        if not script.exists():
            raise FileNotFoundError(f"script not found: {script}")
        cmd = [
            str(cfg.faster_python), str(script),
            "--audio", str(wav_path),
            "--model", cfg.faster_model,
            "--device", cfg.faster_device,
            "--compute-type", cfg.faster_compute,
            "--language", cfg.faster_language,
            "--beam-size", str(cfg.faster_beam),
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=420,
            env=_with_utf8_env(),
        )
        payload = _last_json_line(proc.stdout or "")
        payload["audio_path"] = str(wav_path)
        payload["returncode"] = proc.returncode
        if proc.returncode != 0 and payload.get("ok", False):
            payload["ok"] = False
            payload["error"] = (proc.stderr or proc.stdout or "").strip()
        return payload

    def _on_fw_test_transcribe_done(self, payload: object) -> None:
        self._fw_test_worker = None
        data = payload if isinstance(payload, dict) else {}
        if data.get("ok"):
            text = str(data.get("text", "")).strip()
            self.fw_test_result_edit.setPlainText(text or "(空テキスト)")
            self.fw_test_status_label.setText("完了")
            self._append_log(f"[fw-test] done: {text[:80]}")
        else:
            err = str(data.get("error", "unknown error"))
            self.fw_test_result_edit.setPlainText("")
            self.fw_test_status_label.setText("失敗")
            self._append_log(f"[fw-test] failed: {err}")

    def _on_fw_test_transcribe_error(self, err: str) -> None:
        self._fw_test_worker = None
        self.fw_test_status_label.setText("失敗")
        self._append_log(f"[fw-test] worker error: {err}")

    @staticmethod
    def _is_local_kks_running() -> bool:
        ps = (
            "$p=Get-Process -ErrorAction SilentlyContinue | "
            "Where-Object { $_.ProcessName -like '*KoikatsuSunshine*' -or $_.ProcessName -like '*CharaStudio*' }; "
            "if($p){'1'}else{'0'}"
        )
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
            )
            return proc.returncode == 0 and (proc.stdout or "").strip().startswith("1")
        except Exception:
            return False

    @staticmethod
    def _load_wav_as_float(path: Path) -> tuple[np.ndarray, int]:
        with wave.open(str(path), "rb") as wf:
            channels = wf.getnchannels()
            width = wf.getsampwidth()
            rate = wf.getframerate()
            frames = wf.readframes(wf.getnframes())
        if width == 2:
            data = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        elif width == 1:
            data = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
        elif width == 4:
            data = np.frombuffer(frames, dtype=np.int32).astype(np.float32) / 2147483648.0
        else:
            raise RuntimeError(f"unsupported wav sample width: {width}")
        if channels > 1:
            data = data.reshape(-1, channels)
        return data, rate

    def _play_wav_in_gui(self, wav_path: Path) -> bool:
        try:
            data, sample_rate = self._load_wav_as_float(wav_path)
            volume = float(self.sbv2_test_volume_slider.value()) / 100.0
            data = np.clip(data * volume, -1.0, 1.0)
            sd.stop()
            sd.play(data, sample_rate, blocking=False)
            return True
        except Exception as exc:
            self._append_log(f"[sbv2-test] gui play failed: {exc}")
            return False

    def _play_last_sbv2_test(self) -> None:
        if self._sbv2_test_last_wav is None or (not self._sbv2_test_last_wav.exists()):
            self.sbv2_test_status_label.setText("再生可能な音声がありません")
            return
        if self._play_wav_in_gui(self._sbv2_test_last_wav):
            self.sbv2_test_status_label.setText("GUI再生中")

    def _resolve_event_sender_script(self, cfg: AppConfig) -> Path:
        local = Path(__file__).resolve().parent / "send_voice_face_event.ps1"
        if local.exists():
            return local
        return cfg.kks_root / "work" / "tools" / "voice_face_event_pipe_tester" / "send_voice_face_event.ps1"

    def _send_sbv2_test_event(self, cfg: AppConfig, wav_path: Path) -> tuple[bool, str]:
        sender_ps1 = self._resolve_event_sender_script(cfg)
        if not sender_ps1.exists():
            return False, f"sender not found: {sender_ps1}"

        cmd = [
            "powershell", "-ExecutionPolicy", "Bypass", "-File", str(sender_ps1),
            "-PipeName", cfg.pipe_name,
            "-Main", str(cfg.main_index),
            "-AudioPath", str(wav_path),
        ]
        if cfg.remote_http or cfg.target_host.strip():
            cmd.append("-RemoteHttp")
        if cfg.target_host.strip():
            cmd.extend(["-TargetHost", cfg.target_host.strip()])
            cmd.extend(["-TargetPort", str(cfg.target_port)])
            cmd.extend(["-TargetEndpoint", cfg.target_endpoint])
            if cfg.target_token.strip():
                cmd.extend(["-TargetToken", cfg.target_token.strip()])

        if self.sbv2_test_keep_face_chk.isChecked():
            cmd.append("-KeepCurrentFace")
        else:
            face = int(self.sbv2_test_face_spin.value())
            if face >= 0:
                cmd.extend(["-Face", str(face)])
            else:
                cmd.append("-KeepCurrentFace")

        volume = float(self.sbv2_test_volume_slider.value()) / 100.0
        cmd.extend(["-Volume", f"{volume:.2f}"])
        if cfg.voice_pitch >= 0:
            cmd.extend(["-Pitch", str(cfg.voice_pitch)])

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            env=_with_utf8_env(),
        )
        detail = (proc.stdout or proc.stderr or "").strip()
        return proc.returncode == 0, detail

    def _run_sbv2_test_task(self, cfg: AppConfig, text: str) -> dict:
        script = Path(__file__).resolve().parent / "run_grok_tts_event.py"
        if not script.exists():
            raise FileNotFoundError(f"script not found: {script}")
        cmd = [
            str(cfg.pipeline_python), str(script),
            "--response-text", text,
            "--sbv2-root", str(cfg.sbv2_root),
            "--model-name", cfg.sbv2_model_name,
            "--speaker", cfg.sbv2_speaker,
            "--style", cfg.sbv2_style,
            "--length", str(cfg.sbv2_length),
            "--output-dir", str(cfg.output_dir / "grok_tts_outputs"),
            "--pipe-name", cfg.pipe_name,
            "--main", str(cfg.main_index),
            "--no-send-event",
        ]
        if cfg.sbv2_model_file:
            cmd.extend(["--model-file", cfg.sbv2_model_file])
        if cfg.sbv2_server_url:
            cmd.extend(["--sbv2-server-url", cfg.sbv2_server_url])
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=420,
            env=_with_utf8_env(),
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or "").strip())
        payload = _last_json_line(proc.stdout or "")
        if not payload.get("ok"):
            raise RuntimeError(str(payload.get("error", "sbv2 test failed")))
        payload["returncode"] = proc.returncode
        return payload

    def _run_sbv2_test(self) -> None:
        if self._sbv2_test_worker is not None and self._sbv2_test_worker.isRunning():
            self.sbv2_test_status_label.setText("実行中...")
            return
        text = self.sbv2_test_text_edit.toPlainText().strip()
        if not text:
            self.sbv2_test_status_label.setText("テキスト未入力")
            return
        try:
            cfg = self._build_config()
        except Exception as exc:
            self.sbv2_test_status_label.setText("設定エラー")
            self._append_log(f"[sbv2-test] config error: {exc}")
            return
        self.sbv2_test_status_label.setText("音声生成中...")
        self._sbv2_test_worker = _TaskWorker(lambda: self._run_sbv2_test_task(cfg, text))
        self._sbv2_test_worker.result_ready.connect(self._on_sbv2_test_done)
        self._sbv2_test_worker.error_occurred.connect(self._on_sbv2_test_error)
        self._sbv2_test_worker.start()

    def _on_sbv2_test_done(self, payload: object) -> None:
        self._sbv2_test_worker = None
        data = payload if isinstance(payload, dict) else {}
        merged_wav = Path(str(data.get("merged_wav", ""))).resolve()
        if not merged_wav.exists():
            self.sbv2_test_status_label.setText("音声ファイルなし")
            self._append_log("[sbv2-test] merged wav not found")
            return

        self._sbv2_test_last_wav = merged_wav
        try:
            cfg = self._active_runtime_cfg if self._active_runtime_cfg is not None else self._build_config()
        except Exception as exc:
            self.sbv2_test_status_label.setText("設定エラー")
            self._append_log(f"[sbv2-test] config error: {exc}")
            return
        if self._is_local_kks_running():
            ok, detail = self._send_sbv2_test_event(cfg, merged_wav)
            if ok:
                self.sbv2_test_status_label.setText("KKS送信完了")
                self._append_log("[sbv2-test] sent to KKS")
                return
            self._append_log(f"[sbv2-test] KKS send failed: {detail}")

        if self._play_wav_in_gui(merged_wav):
            self.sbv2_test_status_label.setText("KKS未起動: GUI再生中")
            self._append_log(f"[sbv2-test] local play: {merged_wav.name}")
        else:
            self.sbv2_test_status_label.setText("GUI再生失敗")

    def _on_sbv2_test_error(self, err: str) -> None:
        self._sbv2_test_worker = None
        self.sbv2_test_status_label.setText("失敗")
        self._append_log(f"[sbv2-test] error: {err}")

    def _build_conversion_tab(self) -> None:
        tab = QWidget()
        self.tabs.addTab(tab, "変換辞書")
        layout = QVBoxLayout(tab)
        layout.addWidget(QLabel("TTS前に適用するテキスト変換（上から順に適用）:"))

        self.conversion_table = QTableWidget(0, 3)
        self.conversion_table.setHorizontalHeaderLabels(["変換前", "変換後", "表示適用"])
        self.conversion_table.horizontalHeader().setStretchLastSection(True)
        self.conversion_table.setColumnWidth(0, 250)
        self.conversion_table.setColumnWidth(1, 250)
        self.conversion_table.setColumnWidth(2, 110)
        self._conversion_table_delegate = _ConversionTableDelegate(self.conversion_table)
        self._conversion_table_delegate.request_new_row.connect(self._conv_add_row)
        self.conversion_table.setItemDelegate(self._conversion_table_delegate)
        self.conversion_table.installEventFilter(self)
        layout.addWidget(self.conversion_table, 1)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("行を追加")
        add_btn.clicked.connect(self._conv_add_row)
        del_btn = QPushButton("選択行を削除")
        del_btn.clicked.connect(self._conv_del_row)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(del_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

    def _conv_add_row(self) -> None:
        table = self.conversion_table
        table.blockSignals(True)
        row = table.rowCount()
        table.insertRow(row)
        table.setItem(row, 0, QTableWidgetItem(""))
        table.setItem(row, 1, QTableWidgetItem(""))
        table.setItem(row, 2, self._new_display_apply_item(False))
        table.blockSignals(False)
        table.setCurrentCell(row, 0)
        table.scrollToItem(table.item(row, 0))
        table.editItem(table.item(row, 0))
        self._on_any_setting_changed()

    def _conv_del_row(self) -> None:
        table = self.conversion_table
        rows = sorted({idx.row() for idx in table.selectedIndexes()}, reverse=True)
        for row in rows:
            table.removeRow(row)
        self._on_any_setting_changed()

    @staticmethod
    def _new_display_apply_item(checked: bool) -> QTableWidgetItem:
        item = QTableWidgetItem("")
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        return item

    def _build_transcribe_conversion_tab(self) -> None:
        tab = QWidget()
        self.tabs.addTab(tab, "文字起こし変換")
        layout = QVBoxLayout(tab)
        layout.addWidget(QLabel("FasterWhisper結果に適用するテキスト変換（Grok送信用/表示用を分離）:"))

        self.transcribe_conversion_table = QTableWidget(0, 4)
        self.transcribe_conversion_table.setHorizontalHeaderLabels(["変換前", "Grok送信用", "表示用", "表示適用"])
        self.transcribe_conversion_table.horizontalHeader().setStretchLastSection(True)
        self.transcribe_conversion_table.setColumnWidth(0, 220)
        self.transcribe_conversion_table.setColumnWidth(1, 220)
        self.transcribe_conversion_table.setColumnWidth(2, 220)
        self.transcribe_conversion_table.setColumnWidth(3, 110)
        self.transcribe_conversion_table.itemChanged.connect(self._on_transcribe_conv_item_changed)
        self.transcribe_conversion_table.installEventFilter(self)
        layout.addWidget(self.transcribe_conversion_table, 1)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("行を追加")
        add_btn.clicked.connect(self._transcribe_conv_add_row)
        del_btn = QPushButton("選択行を削除")
        del_btn.clicked.connect(self._transcribe_conv_del_row)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(del_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

    def _transcribe_conv_add_row(self) -> None:
        table = self.transcribe_conversion_table
        table.blockSignals(True)
        row = table.rowCount()
        table.insertRow(row)
        table.setItem(row, 0, QTableWidgetItem(""))
        table.setItem(row, 1, QTableWidgetItem(""))
        table.setItem(row, 2, QTableWidgetItem(""))
        table.setItem(row, 3, self._new_display_apply_item(True))
        table.blockSignals(False)
        table.setCurrentCell(row, 0)
        table.scrollToItem(table.item(row, 0))
        table.editItem(table.item(row, 0))
        self._on_live_setting_changed()

    def _transcribe_conv_del_row(self) -> None:
        table = self.transcribe_conversion_table
        rows = sorted({idx.row() for idx in table.selectedIndexes()}, reverse=True)
        for row in rows:
            table.removeRow(row)
        self._on_live_setting_changed()

    def _on_transcribe_conv_item_changed(self, _item: QTableWidgetItem) -> None:
        if self._loading_config:
            return
        self._on_live_setting_changed()

    def _build_selenium_tab(self) -> None:
        tab = QWidget()
        self.tabs.addTab(tab, "Selenium")
        layout = QVBoxLayout(tab)

        # プロファイル選択
        profile_row = QHBoxLayout()
        profile_row.addWidget(QLabel("Chromeプロファイル:"))
        self.chrome_profile_combo = QComboBox()
        profile_row.addWidget(self.chrome_profile_combo, 1)
        refresh_btn = QPushButton("更新")
        refresh_btn.clicked.connect(self._refresh_chrome_profiles)
        profile_row.addWidget(refresh_btn)
        layout.addLayout(profile_row)

        # ポート設定
        port_row = QHBoxLayout()
        port_row.addWidget(QLabel("デバッグポート:"))
        self.chrome_port_spin = QSpinBox()
        self.chrome_port_spin.setRange(1024, 65535)
        self.chrome_port_spin.setValue(9222)
        port_row.addWidget(self.chrome_port_spin)
        self.chrome_headless_chk = QCheckBox("ヘッドレス")
        port_row.addWidget(self.chrome_headless_chk)
        port_row.addStretch()
        layout.addLayout(port_row)

        # ボタン行
        btn_row = QHBoxLayout()
        self.chrome_launch_btn = QPushButton("Chrome起動")
        self.chrome_launch_btn.clicked.connect(self._do_chrome_launch)
        btn_row.addWidget(self.chrome_launch_btn)

        self.chrome_connect_btn = QPushButton("Selenium接続")
        self.chrome_connect_btn.clicked.connect(self._do_chrome_launch)
        self.chrome_connect_btn.setEnabled(False)
        btn_row.addWidget(self.chrome_connect_btn)

        self.chrome_close_btn = QPushButton("Chrome終了")
        self.chrome_close_btn.clicked.connect(self._do_chrome_close)
        self.chrome_close_btn.setEnabled(False)
        btn_row.addWidget(self.chrome_close_btn)
        layout.addLayout(btn_row)

        # テストボタン
        test_row = QHBoxLayout()
        self.chrome_test_btn = QPushButton("Grokを開く（テスト）")
        self.chrome_test_btn.clicked.connect(self._do_chrome_test_grok)
        self.chrome_test_btn.setEnabled(False)
        test_row.addWidget(self.chrome_test_btn)
        layout.addLayout(test_row)

        # ステータス
        self.chrome_status_label = QLabel("")
        layout.addWidget(self.chrome_status_label)

        layout.addStretch()

        # 初期化
        self._chrome_driver = None
        self._refresh_chrome_profiles()

    def _refresh_chrome_profiles(self) -> None:
        self.chrome_profile_combo.clear()
        try:
            from chrome_debug import get_profiles
            profiles = get_profiles()
            for p in profiles:
                display = f"{p['profile_dir']}: {p['email'] or '(未ログイン)'}"
                self.chrome_profile_combo.addItem(display, p["profile_dir"])
            self.chrome_status_label.setText(f"{len(profiles)}個のプロファイルを検出")
        except Exception as e:
            self.chrome_status_label.setText(f"エラー: {e}")

    def _is_chrome_debug_running(self, port: int) -> bool:
        """指定ポートでデバッグChromeが起動済みか確認"""
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2.0) as resp:
                return resp.status == 200
        except Exception:
            return False

    def _find_grok_tab(self) -> bool:
        """既存タブからgrok.comを探してアクティブにする"""
        if not self._chrome_driver:
            return False
        try:
            for handle in self._chrome_driver.window_handles:
                self._chrome_driver.switch_to.window(handle)
                if "grok" in self._chrome_driver.current_url.lower():
                    self.chrome_status_label.setText(f"Grokタブ検出: {self._chrome_driver.current_url}")
                    return True
        except Exception:
            pass
        return False

    def _do_chrome_launch(self) -> None:
        port = self.chrome_port_spin.value()
        headless = self.chrome_headless_chk.isChecked()
        profile_dir = str(self.chrome_profile_combo.currentData() or "").strip()
        self.chrome_launch_btn.setEnabled(False)
        self.chrome_status_label.setText("Chrome起動中...")

        def _task(**kwargs):
            from chrome_debug import launch_chrome, get_driver
            already_running = False
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2.0) as resp:
                    already_running = resp.status == 200
            except Exception:
                pass
            if already_running:
                return "existing", get_driver(port=port)
            else:
                driver = launch_chrome(port=port, headless=headless, profile_dir=profile_dir)
                return "launched", driver

        self._chrome_tab_worker = _SeleniumWorker(_task)
        self._chrome_tab_worker.result_ready.connect(self._on_chrome_tab_done)
        self._chrome_tab_worker.error_occurred.connect(self._on_chrome_tab_error)
        self._chrome_tab_worker.start()

    def _on_chrome_tab_done(self, status, driver) -> None:
        self._chrome_driver = driver
        self.chrome_close_btn.setEnabled(True)
        self.chrome_test_btn.setEnabled(True)
        self.chrome_connect_btn.setEnabled(False)
        if self._find_grok_tab():
            pass  # ステータスは_find_grok_tab内で設定済み
        elif status == "existing":
            self.chrome_status_label.setText("既存Chrome接続完了（Grokタブなし）")
        else:
            self.chrome_status_label.setText("Chrome起動＋Selenium接続完了（Grokタブなし）")

    def _on_chrome_tab_error(self, err) -> None:
        self.chrome_status_label.setText(f"起動エラー: {err}")
        self.chrome_launch_btn.setEnabled(True)

    def _do_chrome_close(self) -> None:
        try:
            from chrome_debug import close_chrome
            close_chrome()
            self._chrome_driver = None
            self.chrome_status_label.setText("Chrome終了")
            self.chrome_launch_btn.setEnabled(True)
            self.chrome_connect_btn.setEnabled(False)
            self.chrome_close_btn.setEnabled(False)
            self.chrome_test_btn.setEnabled(False)
        except Exception as e:
            self.chrome_status_label.setText(f"終了エラー: {e}")

    def _do_chrome_test_grok(self) -> None:
        if self._chrome_driver:
            try:
                self._chrome_driver.get("https://grok.com")
                self.chrome_status_label.setText("Grokを開きました")
            except Exception as e:
                self.chrome_status_label.setText(f"エラー: {e}")

    def _build_filter_tab(self) -> None:
        tab = QWidget()
        self.tabs.addTab(tab, "フィルター")
        layout = QVBoxLayout(tab)
        layout.addWidget(QLabel("除外するフレーズ（1行1フレーズ、部分一致で除外）:"))
        self.filter_edit = QPlainTextEdit()
        self.filter_edit.setPlainText(
            "ありがとうございました\n"
            "ご視聴ありがとうございました\n"
            "チャンネル登録よろしくお願いします\n"
            "高評価よろしくお願いします\n"
            "字幕は自動生成されています\n"
            "お疲れ様でした\n"
            "視聴ありがとうございました\n"
            "ご視聴ありがとう\n"
            "MBC\n"
            "NHK\n"
        )
        layout.addWidget(self.filter_edit, 1)

    # ---- ヘルパー ----

    def _hrow(self, edit: QLineEdit, btn: QPushButton) -> QWidget:
        row = QHBoxLayout(); row.addWidget(edit, 1); row.addWidget(btn)
        w = QWidget(); w.setLayout(row)
        return w

    def _pick_dir(self, edit: QLineEdit, title: str) -> None:
        selected = QFileDialog.getExistingDirectory(self, title, edit.text().strip() or str(Path.cwd()))
        if selected:
            edit.setText(selected)

    def _pick_file(self, edit: QLineEdit, title: str) -> None:
        current = edit.text().strip()
        start = str(Path(current).parent) if current else str(Path.cwd())
        selected, _ = QFileDialog.getOpenFileName(self, title, start, "All Files (*)")
        if selected:
            edit.setText(selected)

    def _reload_models(self) -> None:
        sbv2_root = Path(self.sbv2_root_edit.text().strip()).expanduser()
        model_assets = sbv2_root / "model_assets"
        prev = self.model_name_combo.currentText().strip()
        self.model_name_combo.clear()
        if not model_assets.is_dir():
            return
        names = sorted(
            d.name for d in model_assets.iterdir()
            if d.is_dir() and (d / "config.json").exists()
        )
        for name in names:
            self.model_name_combo.addItem(name)
        if prev:
            idx = self.model_name_combo.findText(prev)
            if idx >= 0:
                self.model_name_combo.setCurrentIndex(idx)
            else:
                self.model_name_combo.setEditText(prev)
        self._reload_model_files()

    def _reload_model_files(self) -> None:
        sbv2_root = Path(self.sbv2_root_edit.text().strip()).expanduser()
        model_name = self.model_name_combo.currentText().strip()
        prev = self.model_file_edit.currentText().strip()
        self.model_file_edit.clear()
        self.model_file_edit.addItem("")
        if model_name:
            model_dir = sbv2_root / "model_assets" / model_name
            if model_dir.is_dir():
                files = sorted(p.name for p in model_dir.glob("*.safetensors"))
                for f in files:
                    self.model_file_edit.addItem(f)
        if prev:
            idx = self.model_file_edit.findText(prev)
            if idx >= 0:
                self.model_file_edit.setCurrentIndex(idx)
            else:
                self.model_file_edit.setEditText(prev)

    @staticmethod
    def _strip_device_index(label: str) -> str:
        """'[2] Microphone (...)' → 'Microphone (...)'"""
        return re.sub(r'^\[\d+\]\s*', '', label)

    def _select_device_by_name(self, saved_label: str) -> bool:
        target = self._strip_device_index(saved_label)
        for i in range(self.device_combo.count()):
            if self._strip_device_index(self.device_combo.itemText(i)) == target:
                self.device_combo.setCurrentIndex(i)
                return True
        return False

    def _reload_devices(self) -> None:
        prev_label = self.device_combo.currentText()
        self.device_combo.clear()
        self.device_combo.addItem("System default", None)
        for idx, label in get_input_devices():
            self.device_combo.addItem(label, idx)
        if prev_label and prev_label != "System default":
            self._select_device_by_name(prev_label)

    def _append_log(self, msg: str) -> None:
        self.log_text.appendPlainText(msg)

    def _install_clipboard_support(self) -> None:
        for table in (self.conversion_table, self.transcribe_conversion_table):
            table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            table.customContextMenuRequested.connect(lambda pos, t=table: self._show_table_context_menu(t, pos))

        for seq, action in [
            ("Ctrl+C", "copy"),
            ("Ctrl+V", "paste"),
            ("Ctrl+X", "cut"),
            ("Ctrl+A", "select_all"),
        ]:
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.ShortcutContext.WindowShortcut)
            sc.activated.connect(lambda a=action: self._handle_clipboard_shortcut(a))
            self._clipboard_shortcuts.append(sc)

    def _show_table_context_menu(self, table: QTableWidget, pos) -> None:
        table.setFocus()
        menu = QMenu(table)
        act_copy = QAction("コピー", menu)
        act_paste = QAction("ペースト", menu)
        act_cut = QAction("切り取り", menu)
        act_select_all = QAction("すべて選択", menu)
        act_copy.triggered.connect(lambda: self._handle_clipboard_shortcut("copy"))
        act_paste.triggered.connect(lambda: self._handle_clipboard_shortcut("paste"))
        act_cut.triggered.connect(lambda: self._handle_clipboard_shortcut("cut"))
        act_select_all.triggered.connect(lambda: self._handle_clipboard_shortcut("select_all"))
        menu.addAction(act_copy)
        menu.addAction(act_paste)
        menu.addAction(act_cut)
        menu.addSeparator()
        menu.addAction(act_select_all)
        menu.exec(table.viewport().mapToGlobal(pos))

    @staticmethod
    def _focused_table_widget() -> Optional[QTableWidget]:
        w = QApplication.focusWidget()
        while w is not None:
            if isinstance(w, QTableWidget):
                return w
            w = w.parentWidget()
        return None

    @staticmethod
    def _focused_text_widget():
        w = QApplication.focusWidget()
        if isinstance(w, (QLineEdit, QPlainTextEdit)):
            return w
        if isinstance(w, QComboBox) and w.isEditable():
            return w.lineEdit()
        while w is not None:
            if isinstance(w, (QLineEdit, QPlainTextEdit)):
                return w
            if isinstance(w, QComboBox) and w.isEditable():
                return w.lineEdit()
            w = w.parentWidget()
        return None

    def _handle_clipboard_shortcut(self, action: str) -> None:
        tw = self._focused_text_widget()
        if tw is not None:
            if action == "copy":
                tw.copy()
                return
            if action == "paste":
                tw.paste()
                return
            if action == "cut":
                tw.cut()
                return
            if action == "select_all":
                tw.selectAll()
                return

        table = self._focused_table_widget()
        if table is None:
            return
        if action == "copy":
            self._table_copy_selected(table)
        elif action == "paste":
            self._table_paste_from_clipboard(table)
        elif action == "cut":
            self._table_cut_selected(table)
        elif action == "select_all":
            table.selectAll()

    def _table_copy_selected(self, table: QTableWidget) -> None:
        ranges = table.selectedRanges()
        if not ranges:
            return
        r = ranges[0]
        lines: list[str] = []
        for row in range(r.topRow(), r.bottomRow() + 1):
            cols: list[str] = []
            for col in range(r.leftColumn(), r.rightColumn() + 1):
                item = table.item(row, col)
                if item is None:
                    cols.append("")
                elif item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                    cols.append("1" if item.checkState() == Qt.CheckState.Checked else "0")
                else:
                    cols.append(item.text())
            lines.append("\t".join(cols))
        QApplication.clipboard().setText("\n".join(lines))

    def _table_cut_selected(self, table: QTableWidget) -> None:
        self._table_copy_selected(table)
        self._table_clear_selected(table)

    def _table_clear_selected(self, table: QTableWidget) -> None:
        indices = list({(idx.row(), idx.column()) for idx in table.selectedIndexes()})
        if not indices:
            return
        table.blockSignals(True)
        try:
            for row, col in indices:
                item = table.item(row, col)
                if item is None:
                    item = QTableWidgetItem("")
                    table.setItem(row, col, item)
                if item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                    item.setCheckState(Qt.CheckState.Unchecked)
                if item.flags() & Qt.ItemFlag.ItemIsEditable:
                    item.setText("")
        finally:
            table.blockSignals(False)
        self._on_live_setting_changed()

    @staticmethod
    def _parse_bool_text(text: str) -> Optional[bool]:
        v = (text or "").strip().lower()
        if v in ("1", "true", "yes", "on", "checked"):
            return True
        if v in ("0", "false", "no", "off", "unchecked"):
            return False
        return None

    def _table_paste_from_clipboard(self, table: QTableWidget) -> None:
        clip = QApplication.clipboard().text()
        if not clip:
            return
        start_row = table.currentRow() if table.currentRow() >= 0 else 0
        start_col = table.currentColumn() if table.currentColumn() >= 0 else 0
        rows = clip.splitlines()
        table.blockSignals(True)
        try:
            for r_ofs, line in enumerate(rows):
                row = start_row + r_ofs
                if row >= table.rowCount():
                    break
                cols = line.split("\t")
                for c_ofs, raw in enumerate(cols):
                    col = start_col + c_ofs
                    if col >= table.columnCount():
                        break
                    item = table.item(row, col)
                    if item is None:
                        item = QTableWidgetItem("")
                        table.setItem(row, col, item)
                    bool_val = self._parse_bool_text(raw)
                    if (item.flags() & Qt.ItemFlag.ItemIsUserCheckable) and (bool_val is not None):
                        item.setCheckState(Qt.CheckState.Checked if bool_val else Qt.CheckState.Unchecked)
                        continue
                    if item.flags() & Qt.ItemFlag.ItemIsEditable:
                        item.setText(raw)
        finally:
            table.blockSignals(False)
        self._on_live_setting_changed()

    def eventFilter(self, obj, event):
        conversion_table = getattr(self, "conversion_table", None)
        transcribe_table = getattr(self, "transcribe_conversion_table", None)

        if conversion_table is not None and obj is conversion_table and event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Tab and not (
                event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier)
            ):
                row = conversion_table.currentRow()
                col = conversion_table.currentColumn()
                if row == conversion_table.rowCount() - 1 and col == 1:
                    self._conv_add_row()
                    return True
        if transcribe_table is not None and obj is transcribe_table and event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Tab and not (
                event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier)
            ):
                row = transcribe_table.currentRow()
                col = transcribe_table.currentColumn()
                last_col = transcribe_table.columnCount() - 1
                if row == transcribe_table.rowCount() - 1 and col == last_col:
                    self._transcribe_conv_add_row()
                    return True
        return super().eventFilter(obj, event)

    @staticmethod
    def _deferred_live_fields(cfg_prev: AppConfig, cfg_now: AppConfig) -> list[str]:
        return _deferred_live_fields_impl(cfg_prev, cfg_now)

    def _apply_live_settings(self, cfg: AppConfig) -> None:
        _apply_live_settings_impl(self, cfg)

    def _on_live_setting_changed(self, *_args) -> None:
        _on_live_setting_changed_impl(self, *_args)

    def _on_any_setting_changed(self, *_args) -> None:
        _on_any_setting_changed_impl(self, *_args)

    def _install_autosave_hooks(self) -> None:
        # 録音設定
        self.device_combo.currentIndexChanged.connect(self._on_any_setting_changed)
        self.wav_dir_edit.textChanged.connect(self._on_any_setting_changed)
        self.threshold_spin.valueChanged.connect(self._on_any_setting_changed)
        self.silence_spin.valueChanged.connect(self._on_any_setting_changed)
        self.min_dur_spin.valueChanged.connect(self._on_any_setting_changed)
        self.pre_roll_spin.valueChanged.connect(self._on_any_setting_changed)
        self.post_roll_spin.valueChanged.connect(self._on_any_setting_changed)

        # パイプライン設定
        self.kks_root_edit.textChanged.connect(self._on_any_setting_changed)
        self.output_dir_edit.textChanged.connect(self._on_any_setting_changed)
        self.faster_python_edit.textChanged.connect(self._on_any_setting_changed)
        self.faster_model_edit.currentTextChanged.connect(self._on_any_setting_changed)
        self.faster_device_combo.currentTextChanged.connect(self._on_any_setting_changed)
        self.faster_compute_combo.currentTextChanged.connect(self._on_any_setting_changed)
        self.faster_lang_edit.textChanged.connect(self._on_any_setting_changed)
        self.faster_beam_spin.valueChanged.connect(self._on_any_setting_changed)
        self.pipeline_python_edit.textChanged.connect(self._on_any_setting_changed)
        self.sbv2_root_edit.textChanged.connect(self._on_any_setting_changed)
        self.video_metadata_edit.textChanged.connect(self._on_any_setting_changed)
        self.sbv2_server_url_edit.textChanged.connect(self._on_any_setting_changed)
        self.sbv2_auto_start_chk.toggled.connect(self._on_any_setting_changed)
        self.model_name_combo.currentTextChanged.connect(self._on_any_setting_changed)
        self.model_file_edit.currentTextChanged.connect(self._on_any_setting_changed)
        self.speaker_edit.textChanged.connect(self._on_any_setting_changed)
        self.style_edit.textChanged.connect(self._on_any_setting_changed)
        self.length_spin.valueChanged.connect(self._on_any_setting_changed)
        self.voice_volume_spin.valueChanged.connect(self._on_any_setting_changed)
        self.voice_pitch_spin.valueChanged.connect(self._on_any_setting_changed)
        self.pipe_edit.textChanged.connect(self._on_any_setting_changed)
        self.main_spin.valueChanged.connect(self._on_any_setting_changed)
        self.face_spin.valueChanged.connect(self._on_any_setting_changed)
        self.keep_face_chk.toggled.connect(self._on_any_setting_changed)
        self.target_host_edit.textChanged.connect(self._on_any_setting_changed)
        self.target_port_spin.valueChanged.connect(self._on_any_setting_changed)
        self.target_endpoint_edit.textChanged.connect(self._on_any_setting_changed)
        self.target_token_edit.textChanged.connect(self._on_any_setting_changed)
        self.remote_http_chk.toggled.connect(self._on_any_setting_changed)
        self.subtitle_send_chk.toggled.connect(self._on_any_setting_changed)
        self.subtitle_host_edit.textChanged.connect(self._on_any_setting_changed)
        self.subtitle_port_spin.valueChanged.connect(self._on_any_setting_changed)
        self.subtitle_endpoint_edit.textChanged.connect(self._on_any_setting_changed)
        self.subtitle_token_edit.textChanged.connect(self._on_any_setting_changed)
        self.subtitle_timeout_spin.valueChanged.connect(self._on_any_setting_changed)
        self.source_mode_combo.currentTextChanged.connect(self._on_any_setting_changed)
        self.external_text_chk.toggled.connect(self._on_any_setting_changed)
        self.external_text_host_edit.textChanged.connect(self._on_any_setting_changed)
        self.external_text_port_spin.valueChanged.connect(self._on_any_setting_changed)
        self.external_text_endpoint_edit.textChanged.connect(self._on_any_setting_changed)
        self.external_text_token_edit.textChanged.connect(self._on_any_setting_changed)
        self.external_text_dedupe_spin.valueChanged.connect(self._on_any_setting_changed)

        # フィルター / 変換
        self.filter_edit.textChanged.connect(self._on_any_setting_changed)
        self.conversion_table.itemChanged.connect(self._on_any_setting_changed)
        self.chrome_profile_combo.currentIndexChanged.connect(self._on_any_setting_changed)
        self.chrome_port_spin.valueChanged.connect(self._on_any_setting_changed)
        self.chrome_headless_chk.toggled.connect(self._on_any_setting_changed)

    @staticmethod
    def _is_recorder_needed(cfg: AppConfig) -> bool:
        mode = (cfg.source_mode or DEFAULT_SOURCE_MODE).strip().lower()
        return mode in ("mic", "both")

    # ---- 設定 ----

    def _build_config(self) -> AppConfig:
        filter_phrases = [l for l in self.filter_edit.toPlainText().splitlines() if l.strip()]
        transcribe_conversion_dict = []
        for row in range(self.transcribe_conversion_table.rowCount()):
            from_item = self.transcribe_conversion_table.item(row, 0)
            grok_to_item = self.transcribe_conversion_table.item(row, 1)
            display_to_item = self.transcribe_conversion_table.item(row, 2)
            display_item = self.transcribe_conversion_table.item(row, 3)
            from_str = (from_item.text() if from_item else "").strip()
            grok_to = (grok_to_item.text() if grok_to_item else "").strip()
            display_to = (display_to_item.text() if display_to_item else "").strip()
            if from_str:
                display_apply = bool(display_item and display_item.checkState() == Qt.CheckState.Checked)
                transcribe_conversion_dict.append({
                    "from": from_str,
                    "to_grok": grok_to,
                    "to_display": display_to,
                    "display_apply": display_apply,
                })
        conversion_dict = []
        for row in range(self.conversion_table.rowCount()):
            from_item = self.conversion_table.item(row, 0)
            to_item = self.conversion_table.item(row, 1)
            display_item = self.conversion_table.item(row, 2)
            from_str = (from_item.text() if from_item else "").strip()
            to_str = (to_item.text() if to_item else "").strip()
            if from_str:
                display_apply = bool(display_item and display_item.checkState() == Qt.CheckState.Checked)
                conversion_dict.append({"from": from_str, "to": to_str, "display_apply": display_apply})
        return AppConfig(
            wav_dir=Path(self.wav_dir_edit.text().strip()).expanduser().resolve(),
            threshold_dbfs=float(self.threshold_spin.value()),
            silence_seconds=float(self.silence_spin.value()),
            min_duration_seconds=float(self.min_dur_spin.value()),
            pre_roll_seconds=float(self.pre_roll_spin.value()),
            post_roll_seconds=float(self.post_roll_spin.value()),
            device=self.device_combo.currentData(),
            kks_root=Path(self.kks_root_edit.text().strip()).expanduser().resolve(),
            output_dir=Path(self.output_dir_edit.text().strip()).expanduser().resolve(),
            faster_python=Path(self.faster_python_edit.text().strip()).expanduser().resolve(),
            faster_model=self.faster_model_edit.currentText().strip() or "large-v3",
            faster_device=self.faster_device_combo.currentText().strip(),
            faster_compute=self.faster_compute_combo.currentText().strip(),
            faster_language=self.faster_lang_edit.text().strip() or "ja",
            faster_beam=max(1, int(self.faster_beam_spin.value())),
            pipeline_python=Path(self.pipeline_python_edit.text().strip()).expanduser().resolve(),
            sbv2_root=Path(self.sbv2_root_edit.text().strip()).expanduser().resolve(),
            sbv2_model_name=self.model_name_combo.currentText().strip(),
            sbv2_model_file=self.model_file_edit.currentText().strip(),
            sbv2_speaker=self.speaker_edit.text().strip() or "0",
            sbv2_style=self.style_edit.text().strip() or "Neutral",
            sbv2_length=float(self.length_spin.value()),
            voice_volume=float(self.voice_volume_spin.value()),
            voice_pitch=float(self.voice_pitch_spin.value()),
            pipe_name=self.pipe_edit.text().strip() or "kks_voice_face_events",
            target_host=self.target_host_edit.text().strip(),
            target_port=int(self.target_port_spin.value()),
            target_endpoint=self.target_endpoint_edit.text().strip() or "/voice-face-event",
            target_token=self.target_token_edit.text().strip(),
            remote_http=bool(self.remote_http_chk.isChecked()),
            subtitle_send_enabled=bool(self.subtitle_send_chk.isChecked()),
            subtitle_target_host=self.subtitle_host_edit.text().strip() or "127.0.0.1",
            subtitle_target_port=int(self.subtitle_port_spin.value()),
            subtitle_endpoint=self.subtitle_endpoint_edit.text().strip() or "/subtitle-event",
            subtitle_token=self.subtitle_token_edit.text().strip(),
            subtitle_timeout_sec=float(self.subtitle_timeout_spin.value()),
            main_index=int(self.main_spin.value()),
            face=int(self.face_spin.value()),
            keep_current_face=bool(self.keep_face_chk.isChecked()),
            source_mode=self.source_mode_combo.currentText().strip().lower() or DEFAULT_SOURCE_MODE,
            external_text_enabled=bool(self.external_text_chk.isChecked()),
            external_text_host=self.external_text_host_edit.text().strip() or "127.0.0.1",
            external_text_port=int(self.external_text_port_spin.value()),
            external_text_endpoint=self.external_text_endpoint_edit.text().strip() or "/manual-text",
            external_text_token=self.external_text_token_edit.text().strip(),
            external_text_dedupe_max=int(self.external_text_dedupe_spin.value()),
            transcribe_server_port=int(
                json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("transcribe_server_port", 18760)
                if CONFIG_FILE.exists() else 18760
            ),
            sbv2_server_url=self.sbv2_server_url_edit.text().strip() or "http://127.0.0.1:5000",
            sbv2_server_auto_start=self.sbv2_auto_start_chk.isChecked(),
            video_metadata_path=(
                Path(self.video_metadata_edit.text().strip()).expanduser().resolve()
                if self.video_metadata_edit.text().strip()
                else None
            ),
            filter_phrases=filter_phrases,
            transcribe_conversion_dict=transcribe_conversion_dict,
            conversion_dict=conversion_dict,
        )

    def _save_config(self, cfg: Optional[AppConfig] = None) -> Optional[AppConfig]:
        if cfg is None:
            try:
                cfg = self._build_config()
            except Exception:
                return None
        data = {
            "device_name": self.device_combo.currentText(),
            "wav_dir": str(cfg.wav_dir), "threshold_dbfs": cfg.threshold_dbfs,
            "silence_seconds": cfg.silence_seconds, "min_duration_seconds": cfg.min_duration_seconds,
            "pre_roll_seconds": cfg.pre_roll_seconds, "post_roll_seconds": cfg.post_roll_seconds,
            "kks_root": str(cfg.kks_root), "output_dir": str(cfg.output_dir),
            "faster_python": str(cfg.faster_python), "faster_model": cfg.faster_model,
            "faster_device": cfg.faster_device, "faster_compute": cfg.faster_compute,
            "faster_language": cfg.faster_language, "faster_beam": cfg.faster_beam,
            "pipeline_python": str(cfg.pipeline_python), "sbv2_root": str(cfg.sbv2_root),
            "sbv2_model_name": cfg.sbv2_model_name, "sbv2_model_file": cfg.sbv2_model_file,
            "sbv2_speaker": cfg.sbv2_speaker, "sbv2_style": cfg.sbv2_style,
            "sbv2_length": cfg.sbv2_length, "voice_volume": cfg.voice_volume,
            "voice_pitch": cfg.voice_pitch, "pipe_name": cfg.pipe_name,
            "target_host": cfg.target_host, "target_port": cfg.target_port,
            "target_endpoint": cfg.target_endpoint, "target_token": cfg.target_token,
            "remote_http": cfg.remote_http, "subtitle_send_enabled": cfg.subtitle_send_enabled,
            "subtitle_target_host": cfg.subtitle_target_host,
            "subtitle_target_port": cfg.subtitle_target_port,
            "subtitle_endpoint": cfg.subtitle_endpoint, "subtitle_token": cfg.subtitle_token,
            "subtitle_timeout_sec": cfg.subtitle_timeout_sec, "main_index": cfg.main_index,
            "face": cfg.face, "keep_current_face": cfg.keep_current_face,
            "source_mode": cfg.source_mode,
            "external_text_enabled": cfg.external_text_enabled,
            "external_text_host": cfg.external_text_host,
            "external_text_port": cfg.external_text_port,
            "external_text_endpoint": cfg.external_text_endpoint,
            "external_text_token": cfg.external_text_token,
            "external_text_dedupe_max": cfg.external_text_dedupe_max,
            "transcribe_server_port": cfg.transcribe_server_port,
            "sbv2_server_url": cfg.sbv2_server_url,
            "sbv2_server_auto_start": cfg.sbv2_server_auto_start,
            "video_metadata_path": str(cfg.video_metadata_path) if cfg.video_metadata_path else "",
            "filter_phrases": cfg.filter_phrases,
            "transcribe_conversion_dict": cfg.transcribe_conversion_dict,
            "conversion_dict": cfg.conversion_dict,
            "manual_history": self._manual_history[:50],
            "model_presets": self._model_presets,
            "chrome_debug_port": self.chrome_port_spin.value(),
            "chrome_headless": self.chrome_headless_chk.isChecked(),
            "chrome_profile": self.chrome_profile_combo.currentData() or "",
        }
        CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return cfg

    def _load_config(self) -> None:
        if not CONFIG_FILE.exists():
            return
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            return
        self._loading_config = True
        try:
            def s(key, widget_val): return str(data.get(key, widget_val))
            def f(key, widget_val): return float(data.get(key, widget_val))
            def i(key, widget_val): return int(data.get(key, widget_val))
            def b(key, widget_val): return bool(data.get(key, widget_val))
            device_name = data.get("device_name", "")
            if device_name and device_name != "System default":
                self._select_device_by_name(device_name)
            self.wav_dir_edit.setText(s("wav_dir", self.wav_dir_edit.text()))
            self.threshold_spin.setValue(f("threshold_dbfs", self.threshold_spin.value()))
            self.silence_spin.setValue(f("silence_seconds", self.silence_spin.value()))
            self.min_dur_spin.setValue(f("min_duration_seconds", self.min_dur_spin.value()))
            self.pre_roll_spin.setValue(f("pre_roll_seconds", self.pre_roll_spin.value()))
            self.post_roll_spin.setValue(f("post_roll_seconds", self.post_roll_spin.value()))
            self.kks_root_edit.setText(s("kks_root", self.kks_root_edit.text()))
            self.output_dir_edit.setText(s("output_dir", self.output_dir_edit.text()))
            self.faster_python_edit.setText(s("faster_python", self.faster_python_edit.text()))
            self.faster_model_edit.setCurrentText(s("faster_model", self.faster_model_edit.currentText()))
            self.faster_device_combo.setCurrentText(s("faster_device", self.faster_device_combo.currentText()))
            self.faster_compute_combo.setCurrentText(s("faster_compute", self.faster_compute_combo.currentText()))
            self.faster_lang_edit.setText(s("faster_language", self.faster_lang_edit.text()))
            self.faster_beam_spin.setValue(i("faster_beam", self.faster_beam_spin.value()))
            self.pipeline_python_edit.setText(s("pipeline_python", self.pipeline_python_edit.text()))
            self.sbv2_root_edit.setText(s("sbv2_root", self.sbv2_root_edit.text()))
            self._reload_models()
            self.model_name_combo.setEditText(s("sbv2_model_name", ""))
            self.model_file_edit.setEditText(s("sbv2_model_file", ""))
            self.speaker_edit.setText(s("sbv2_speaker", self.speaker_edit.text()))
            self.style_edit.setText(s("sbv2_style", self.style_edit.text()))
            self.length_spin.setValue(f("sbv2_length", self.length_spin.value()))
            self.voice_volume_spin.setValue(f("voice_volume", self.voice_volume_spin.value()))
            self.voice_pitch_spin.setValue(f("voice_pitch", self.voice_pitch_spin.value()))
            self.pipe_edit.setText(s("pipe_name", self.pipe_edit.text()))
            self.target_host_edit.setText(s("target_host", self.target_host_edit.text()))
            self.target_port_spin.setValue(i("target_port", self.target_port_spin.value()))
            self.target_endpoint_edit.setText(s("target_endpoint", self.target_endpoint_edit.text()))
            self.target_token_edit.setText(s("target_token", ""))
            self.remote_http_chk.setChecked(b("remote_http", False))
            self.subtitle_send_chk.setChecked(b("subtitle_send_enabled", True))
            self.subtitle_host_edit.setText(s("subtitle_target_host", self.subtitle_host_edit.text()))
            self.subtitle_port_spin.setValue(i("subtitle_target_port", self.subtitle_port_spin.value()))
            self.subtitle_endpoint_edit.setText(s("subtitle_endpoint", self.subtitle_endpoint_edit.text()))
            self.subtitle_token_edit.setText(s("subtitle_token", ""))
            self.subtitle_timeout_spin.setValue(i("subtitle_timeout_sec", self.subtitle_timeout_spin.value()))
            self.main_spin.setValue(i("main_index", self.main_spin.value()))
            self.face_spin.setValue(i("face", self.face_spin.value()))
            self.keep_face_chk.setChecked(b("keep_current_face", True))
            self.source_mode_combo.setCurrentText(s("source_mode", DEFAULT_SOURCE_MODE))
            self.external_text_chk.setChecked(b("external_text_enabled", True))
            self.external_text_host_edit.setText(s("external_text_host", self.external_text_host_edit.text()))
            self.external_text_port_spin.setValue(i("external_text_port", self.external_text_port_spin.value()))
            self.external_text_endpoint_edit.setText(s("external_text_endpoint", self.external_text_endpoint_edit.text()))
            self.external_text_token_edit.setText(s("external_text_token", ""))
            self.external_text_dedupe_spin.setValue(i("external_text_dedupe_max", self.external_text_dedupe_spin.value()))
            # transcribe_server_port は UI 非公開、config.json のみで管理
            self.sbv2_server_url_edit.setText(s("sbv2_server_url", "http://127.0.0.1:5000"))
            self.sbv2_auto_start_chk.setChecked(b("sbv2_server_auto_start", True))
            self.video_metadata_edit.setText(s("video_metadata_path", self.video_metadata_edit.text()))
            # Selenium設定
            self.chrome_port_spin.setValue(i("chrome_debug_port", 9222))
            self.chrome_headless_chk.setChecked(b("chrome_headless", False))
            saved_profile = s("chrome_profile", "")
            if saved_profile:
                for idx in range(self.chrome_profile_combo.count()):
                    if self.chrome_profile_combo.itemData(idx) == saved_profile:
                        self.chrome_profile_combo.setCurrentIndex(idx)
                        break
            phrases = data.get("filter_phrases", [])
            if phrases:
                self.filter_edit.setPlainText("\n".join(phrases))
            stt_conv = data.get("transcribe_conversion_dict", [])
            self.transcribe_conversion_table.setRowCount(0)
            for entry in stt_conv:
                row = self.transcribe_conversion_table.rowCount()
                self.transcribe_conversion_table.insertRow(row)
                self.transcribe_conversion_table.setItem(row, 0, QTableWidgetItem(entry.get("from", "")))
                # backward compatible: old schema had only "to"
                to_grok = str(entry.get("to_grok", entry.get("to", "")))
                to_display = str(entry.get("to_display", entry.get("to", "")))
                self.transcribe_conversion_table.setItem(row, 1, QTableWidgetItem(to_grok))
                self.transcribe_conversion_table.setItem(row, 2, QTableWidgetItem(to_display))
                self.transcribe_conversion_table.setItem(row, 3, self._new_display_apply_item(bool(entry.get("display_apply", True))))
            conv = data.get("conversion_dict", [])
            self.conversion_table.setRowCount(0)
            for entry in conv:
                row = self.conversion_table.rowCount()
                self.conversion_table.insertRow(row)
                self.conversion_table.setItem(row, 0, QTableWidgetItem(entry.get("from", "")))
                self.conversion_table.setItem(row, 1, QTableWidgetItem(entry.get("to", "")))
                self.conversion_table.setItem(row, 2, self._new_display_apply_item(bool(entry.get("display_apply", False))))
            self._model_presets = [p for p in data.get("model_presets", []) if isinstance(p, dict) and p.get("name")]
            self._refresh_preset_ui()
            history = data.get("manual_history", [])
            self._manual_history = list(history)[:50]
            self.manual_combo.clear()
            for h in self._manual_history:
                self.manual_combo.addItem(h)
        finally:
            self._loading_config = False

    # ---- モデルプリセット ----

    def _refresh_preset_ui(self) -> None:
        self.preset_list_combo.clear()
        for p in self._model_presets:
            self.preset_list_combo.addItem(p.get("name", ""))
        for i, btn in enumerate(self._preset_btns):
            if i < len(self._model_presets):
                name = self._model_presets[i].get("name", f"({i+1})")
                btn.setText(name)
                btn.setEnabled(True)
            else:
                btn.setText(f"--- ({i+1})")
                btn.setEnabled(False)

    def _save_preset(self) -> None:
        name = self.preset_name_edit.text().strip()
        if not name:
            return
        preset = {
            "name": name,
            "model_name": self.model_name_combo.currentText().strip(),
            "model_file": self.model_file_edit.currentText().strip(),
            "speaker": self.speaker_edit.text().strip(),
            "style": self.style_edit.text().strip(),
        }
        # 同名なら上書き
        for i, p in enumerate(self._model_presets):
            if p.get("name") == name:
                self._model_presets[i] = preset
                self._refresh_preset_ui()
                self._save_config()
                return
        self._model_presets.append(preset)
        self._refresh_preset_ui()
        self._save_config()

    def _apply_preset(self, idx: int) -> None:
        if idx < 0 or idx >= len(self._model_presets):
            return
        p = self._model_presets[idx]
        self.model_name_combo.setEditText(p.get("model_name", ""))
        self._reload_model_files()
        self.model_file_edit.setEditText(p.get("model_file", ""))
        self.speaker_edit.setText(p.get("speaker", "0"))
        self.style_edit.setText(p.get("style", "Neutral"))
        # 実行中のワーカーにも即時反映
        if self._pipeline_worker is not None:
            self._pipeline_worker._cfg.sbv2_model_name = p.get("model_name", "")
            self._pipeline_worker._cfg.sbv2_model_file = p.get("model_file", "")
            self._pipeline_worker._cfg.sbv2_speaker    = p.get("speaker", "0")
            self._pipeline_worker._cfg.sbv2_style      = p.get("style", "Neutral")
            name = p.get("name", f"({idx+1})")
            self._append_log(f"[preset] モデル切替 → {name}")

    def _apply_preset_from_combo(self) -> None:
        self._apply_preset(self.preset_list_combo.currentIndex())

    def _delete_preset(self) -> None:
        idx = self.preset_list_combo.currentIndex()
        if 0 <= idx < len(self._model_presets):
            self._model_presets.pop(idx)
            self._refresh_preset_ui()
            self._save_config()

    # ---- イベントハンドラ ----

    def _on_start_stop(self) -> None:
        if self._running:
            self._stop_all()
        else:
            self._start_all()

    def _on_pause_resume(self) -> None:
        if not self._paused:
            self._paused = True
            self.pause_btn.setText("▶ 再開")
            self._stop_recorder()
            if self._pipeline_worker:
                self._pipeline_worker.pause()
            self._append_log("[info] 一時停止")
        else:
            self._paused = False
            self.pause_btn.setText("⏸ 一時停止")
            if self._pipeline_worker:
                self._pipeline_worker.resume()
            try:
                cfg = self._build_config()
            except Exception as exc:
                self._append_log(f"[error] {exc}")
                return
            if self._is_recorder_needed(cfg):
                self._start_recorder(cfg)
            self._append_log("[info] 再開")

    def _start_all(self) -> None:
        try:
            cfg = self._build_config()
        except Exception as exc:
            self._append_log(f"[error] {exc}")
            return
        self._save_config(cfg)
        self._running = True
        self._paused = False
        self._active_runtime_cfg = cfg
        self._last_deferred_live_fields = tuple()
        self.start_btn.setText("■ 停止")
        self.pause_btn.setEnabled(True)

        # Selenium自動起動（未接続の場合）→ Workerで非同期実行後にパイプライン起動
        if self._chrome_driver is None:
            port = self.chrome_port_spin.value()
            headless = self.chrome_headless_chk.isChecked()
            self._append_log("[selenium] バックグラウンドで起動中...")
            self._pending_cfg = cfg

            def _selenium_task(**kwargs):
                from chrome_debug import launch_chrome, get_driver
                already_running = False
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2.0) as resp:
                        already_running = resp.status == 200
                except Exception:
                    pass
                if already_running:
                    return "existing", get_driver(port=port)
                else:
                    profile_dir = str(self.chrome_profile_combo.currentData() or "").strip()
                    driver = launch_chrome(port=port, headless=headless, profile_dir=profile_dir)
                    return "launched", driver

            self._selenium_worker = _SeleniumWorker(_selenium_task)
            self._selenium_worker.result_ready.connect(self._on_selenium_worker_done)
            self._selenium_worker.error_occurred.connect(self._on_selenium_worker_error)
            self._selenium_worker.start()
            return

        self._continue_start_pipeline(cfg)

    def _on_selenium_worker_done(self, status, driver) -> None:
        if driver:
            self._chrome_driver = driver
            if self._find_grok_tab():
                self._append_log("[selenium] Grokタブ検出")
            else:
                self._append_log("[selenium] 接続完了（Grokタブなし）")
            self.chrome_close_btn.setEnabled(True)
            self.chrome_test_btn.setEnabled(True)
            if status == "launched":
                self.chrome_launch_btn.setEnabled(False)
        else:
            self._append_log("[selenium] プロファイル未選択、スキップ")
        if self._pending_cfg is None:
            self._append_log("[error] pending config missing")
            return
        self._continue_start_pipeline(self._pending_cfg)
        self._pending_cfg = None

    def _on_selenium_worker_error(self, err) -> None:
        self._append_log(f"[selenium] 起動失敗: {err}")
        if self._pending_cfg is None:
            self._append_log("[error] pending config missing")
            return
        self._continue_start_pipeline(self._pending_cfg)
        self._pending_cfg = None

    def _continue_start_pipeline(self, cfg: "AppConfig") -> None:
        # パイプライン起動
        self._pipeline_thread = QThread(self)
        self._pipeline_worker = PipelineWorker(cfg)
        self._active_runtime_cfg = cfg
        self._pipeline_worker.moveToThread(self._pipeline_thread)
        self._pipeline_thread.started.connect(self._pipeline_worker.run)
        self._pipeline_worker.log.connect(self._append_log)
        self._pipeline_worker.error.connect(self._on_pipeline_error)
        self._pipeline_worker.finished.connect(self._pipeline_worker.deleteLater)
        self._pipeline_thread.finished.connect(self._pipeline_thread.deleteLater)
        self._pipeline_thread.start()

        # 録音起動
        if self._is_recorder_needed(cfg):
            self._start_recorder(cfg)
        else:
            self._append_log("[info] source_mode=external: 録音は起動しません")
        self._append_log("[info] 開始")

    def _start_recorder(self, cfg: AppConfig) -> None:
        device_label = self.device_combo.currentText()
        self._append_log(f"[recorder] device index={cfg.device} label={device_label!r}")
        rec_cfg = RecorderConfig(
            output_dir=cfg.wav_dir,
            sample_rate=16000, block_ms=100,
            threshold_dbfs=cfg.threshold_dbfs,
            silence_seconds=cfg.silence_seconds,
            min_duration_seconds=cfg.min_duration_seconds,
            device=cfg.device,
            pre_roll_seconds=cfg.pre_roll_seconds,
            post_roll_seconds=cfg.post_roll_seconds,
            tcp_host="", tcp_port=17890, tcp_token="", tcp_timeout_seconds=20.0,
            external_control_enabled=False, external_control_host="127.0.0.1",
            external_control_port=17911, external_control_token="",
        )
        self._recorder_thread = QThread(self)
        self._recorder_worker = RecorderWorker(rec_cfg)
        self._recorder_worker.moveToThread(self._recorder_thread)
        self._recorder_thread.started.connect(self._recorder_worker.run)
        self._recorder_worker.log.connect(self._append_log)
        self._recorder_worker.error.connect(lambda s: self._append_log(f"[error] 録音: {s}"))
        self._recorder_worker.finished.connect(self._recorder_worker.deleteLater)
        self._recorder_thread.finished.connect(self._recorder_thread.deleteLater)
        self._recorder_thread.start()

    def _stop_recorder(self) -> None:
        if self._recorder_worker:
            self._recorder_worker.stop()
        if self._recorder_thread:
            self._recorder_thread.quit()
            self._recorder_thread.wait(2000)
        self._recorder_thread = None
        self._recorder_worker = None

    def _stop_all(self) -> None:
        self._running = False
        self._paused = False
        self._active_runtime_cfg = None
        self._pending_cfg = None
        self._last_deferred_live_fields = tuple()
        self.start_btn.setText("▶ 開始")
        self.pause_btn.setEnabled(False)
        self.pause_btn.setText("⏸ 一時停止")
        self._stop_recorder()
        if self._pipeline_worker:
            self._pipeline_worker.stop()
        if self._pipeline_thread:
            self._pipeline_thread.quit()
            self._pipeline_thread.wait(3000)
        self._pipeline_thread = None
        self._pipeline_worker = None
        self._append_log("[info] 停止")

    def _send_manual(self) -> None:
        text = self.manual_combo.currentText().strip()
        if not text:
            return
        if not self._pipeline_worker:
            self._append_log("[warn] パイプラインが起動していません")
            return
        self._pipeline_worker.send_text(text)
        self._append_log(f"[手動] {text}")
        # 履歴に追加（重複除去、先頭に挿入、上限50件）
        if text in self._manual_history:
            self._manual_history.remove(text)
        self._manual_history.insert(0, text)
        self._manual_history = self._manual_history[:50]
        idx = self.manual_combo.findText(text)
        if idx >= 0:
            self.manual_combo.removeItem(idx)
        self.manual_combo.insertItem(0, text)
        while self.manual_combo.count() > 50:
            self.manual_combo.removeItem(self.manual_combo.count() - 1)
        self.manual_combo.lineEdit().clear()

    def _on_pipeline_error(self, stack: str) -> None:
        self._append_log("[error] パイプライン例外")
        self._append_log(stack)

    def closeEvent(self, event) -> None:
        try:
            self._save_config()
        except Exception:
            pass
        try:
            if self._fw_test_stream is not None:
                self._fw_test_stream.stop()
                self._fw_test_stream.close()
                self._fw_test_stream = None
            sd.stop()
        except Exception:
            pass
        self._stop_all()
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------

def main() -> int:
    return run_gui(MainWindow, mutex_name=MUTEX_NAME)


if __name__ == "__main__":
    raise SystemExit(main())
