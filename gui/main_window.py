from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import wave
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import sounddevice as sd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from voice_gate_recorder import RecorderConfig, get_input_devices

from PyQt6.QtCore import QObject, QEvent, QThread, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemDelegate,
    QStyledItemDelegate,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from config.constants import DEFAULT_SOURCE_MODE
from config.models import AppConfig
from controllers.runtime_controller import (
    apply_live_settings as _apply_live_settings_impl,
    deferred_live_fields as _deferred_live_fields_impl,
    on_any_setting_changed as _on_any_setting_changed_impl,
    on_live_setting_changed as _on_live_setting_changed_impl,
)
from controllers.preset_controller import (
    load_active_presets as _load_active_presets_impl,
    load_presets as _load_presets_impl,
    new_preset_id as _new_preset_id,
    normalize_dictionary_entries as _normalize_dictionary_entries,
    normalize_transcription_entries as _normalize_transcription_entries,
    now_iso8601 as _now_iso8601,
    save_active_presets as _save_active_presets_impl,
    save_presets as _save_presets_impl,
)
from controllers.settings_controller import (
    build_config as _build_config_impl,
    load_config as _load_config_impl,
    save_config as _save_config_impl,
)
from core.io_utils import (
    last_json_line as _last_json_line,
    save_text as _save_text,
    wav_duration_sec as _wav_duration_sec,
    with_utf8_env as _with_utf8_env,
)
from workers.pipeline_worker import PipelineWorker
from workers.recorder_worker import RecorderWorker
from workers.thread_workers import SeleniumWorker as _SeleniumWorker, TaskWorker as _TaskWorker

CONFIG_FILE = PROJECT_ROOT / "config.json"
PRESET_CONFIG_DIR = PROJECT_ROOT / "config"
TRANSCRIPTION_PRESETS_FILE = PRESET_CONFIG_DIR / "transcription_presets.json"
DICTIONARY_PRESETS_FILE = PRESET_CONFIG_DIR / "dictionary_presets.json"
ACTIVE_PRESETS_FILE = PRESET_CONFIG_DIR / "active_presets.json"


class _ConversionTableDelegate(QStyledItemDelegate):
    request_new_row = pyqtSignal()

    def __init__(self, parent=None, last_editable_col: int = -1):
        super().__init__(parent)
        self._last_editable_col = last_editable_col  # -1 = 自動（columnCount-1）

    def eventFilter(self, editor, event):
        if event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Tab and not (
                event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier)
            ):
                table = self.parent()
                if isinstance(table, QTableWidget):
                    row = table.currentRow()
                    col = table.currentColumn()
                    if row < 0 or col < 0:
                        return super().eventFilter(editor, event)
                    last_col = self._last_editable_col if self._last_editable_col >= 0 else max(0, table.columnCount() - 1)
                    if col == last_col:
                        self.commitData.emit(editor)
                        self.closeEditor.emit(editor, QAbstractItemDelegate.EndEditHint.NoHint)
                        if row == table.rowCount() - 1:
                            self.request_new_row.emit()
                        else:
                            next_item = table.item(row + 1, 0)
                            if next_item is not None:
                                table.setCurrentCell(row + 1, 0)
                                table.editItem(next_item)
                        return True
        return super().eventFilter(editor, event)


class _CheckStateSortItem(QTableWidgetItem):
    """チェック状態で並び替え可能なテーブル項目。"""

    def __lt__(self, other):
        if isinstance(other, QTableWidgetItem):
            self_value = 1 if self.checkState() == Qt.CheckState.Checked else 0
            other_value = 1 if other.checkState() == Qt.CheckState.Checked else 0
            return self_value < other_value
        return super().__lt__(other)


class _FilterTypeDelegate(QStyledItemDelegate):
    _choices = [
        ("部分一致", "partial"),
        ("完全一致", "exact"),
        ("正規表現", "regex"),
    ]

    def createEditor(self, parent, option, index):
        combo = _NoWheelComboBox(parent)
        for label, value in self._choices:
            combo.addItem(label, value)
        # 1クリック編集時に即ドロップダウンを開く（重複表示防止のためここに一本化）
        QTimer.singleShot(0, combo.showPopup)
        return combo

    def setEditorData(self, editor, index):
        if not isinstance(editor, QComboBox):
            return
        data = index.data(Qt.ItemDataRole.UserRole)
        if data is None:
            text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
            data = "partial"
            for label, value in self._choices:
                if text == label:
                    data = value
                    break
        idx = editor.findData(data)
        editor.setCurrentIndex(idx if idx >= 0 else 0)

    def setModelData(self, editor, model, index):
        if not isinstance(editor, QComboBox):
            return
        label = editor.currentText()
        value = editor.currentData()
        model.setData(index, label, Qt.ItemDataRole.DisplayRole)
        model.setData(index, value, Qt.ItemDataRole.UserRole)


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
        self._run_session_id: Optional[str] = None
        self._fw_test_stream = None
        self._fw_test_chunks: list[np.ndarray] = []
        self._fw_test_sr = 16000
        self._fw_test_worker: Optional[_TaskWorker] = None
        self._fw_test_temp_wav: Optional[Path] = None
        self._fw_test_guard_active = False
        self._fw_test_guard_prev_paused = False
        self._sbv2_test_worker: Optional[_TaskWorker] = None
        self._sbv2_test_last_wav: Optional[Path] = None
        self._sbv2_test_last_run_dir: Optional[Path] = None
        self._sbv2_test_no_send = False

        self._manual_history: list[str] = []
        self._model_presets: list[dict] = []
        self._transcribe_conversion_presets: list[dict] = []
        self._dictionary_presets: list[dict] = []
        self._active_transcribe_preset_id = ""
        self._active_dictionary_preset_id = ""
        self._loading_config = False
        self._clipboard_shortcuts: list[QShortcut] = []
        self._conversion_table_delegate: Optional[_ConversionTableDelegate] = None
        self._transcribe_conversion_table_delegate: Optional[_ConversionTableDelegate] = None
        self._conversion_order_seq = 0
        self._transcribe_conversion_order_seq = 0
        self._filter_order_seq = 0
        self._face_preset_rows: list[dict] = []

        self._build_ui()
        self._load_conversion_presets()
        self._load_config()
        self._install_autosave_hooks()
        self._install_clipboard_support()

    # ---- UI構築 ----

    def _build_ui(self) -> None:
        from PyQt6.QtWidgets import QSplitter
        central = QWidget(self)
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(4, 4, 4, 4)
        root_layout.setSpacing(4)

        # タブとログをスプリッターで上下分割
        from PyQt6.QtWidgets import QSplitter
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)
        root_layout.addWidget(splitter, 1)

        self.tabs = QTabWidget()
        self.tabs.setMinimumSize(0, 0)
        splitter.addWidget(self.tabs)
        self._build_recorder_tab()
        self._build_pipeline_tab()
        self._build_test_tab()
        self._build_selenium_tab()
        self._build_transcribe_conversion_tab()
        self._build_filter_tab()
        self._build_conversion_tab()

        self.log_text = QPlainTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMinimumHeight(40)
        splitter.addWidget(self.log_text)
        splitter.setSizes([520, 160])

        # コントロールボタン（常に表示）
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
        root_layout.addLayout(ctrl)

        # モデルプリセット クイックボタン（常に表示）
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
        root_layout.addLayout(preset_btn_layout)

        # 手動テキスト送信（常に表示）
        manual_group = QGroupBox("手動テキスト送信")
        manual_layout = QHBoxLayout(manual_group)
        self.manual_combo = _NoWheelComboBox()
        self.manual_combo.setEditable(True)
        self.manual_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.manual_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.manual_combo.setMinimumContentsLength(12)
        # 履歴はドロップダウンで選べるようにしつつ、入力中の自動補完は無効化する
        self.manual_combo.setCompleter(None)
        self.manual_combo.lineEdit().setPlaceholderText("テキストを入力して送信...")
        self.manual_combo.lineEdit().returnPressed.connect(self._send_manual)
        self.manual_btn = QPushButton("送信")
        self.manual_btn.clicked.connect(self._send_manual)
        manual_layout.addWidget(self.manual_combo, 1)
        manual_layout.addWidget(self.manual_btn)
        root_layout.addWidget(manual_group)
        self._reset_manual_input()

    def _build_recorder_tab(self) -> None:
        inner = QWidget()
        scroll = QScrollArea()
        scroll.setWidget(inner)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tabs.addTab(scroll, "録音設定")
        form = QFormLayout(inner)

        device_row = QHBoxLayout()
        self.device_combo = _NoWheelAlwaysComboBox()
        self.device_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.device_combo.setMinimumContentsLength(12)
        refresh_btn = QPushButton("更新")
        refresh_btn.clicked.connect(self._reload_devices)
        device_row.addWidget(self.device_combo, 1)
        device_row.addWidget(refresh_btn)
        form.addRow("入力デバイス", device_row)

        self.wav_dir_edit = QLineEdit(str(PROJECT_ROOT / "outputs" / "wav"))
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

        ptt_row = QHBoxLayout()
        self.vr_ptt_enabled_chk = QCheckBox("KKS VR右Bホールド録音")
        self.vr_ptt_enabled_chk.setChecked(False)
        self.vr_ptt_host_edit = QLineEdit("127.0.0.1")
        self.vr_ptt_host_edit.setMaximumWidth(130)
        self.vr_ptt_port_spin = _NoWheelSpinBox()
        self.vr_ptt_port_spin.setRange(1, 65535)
        self.vr_ptt_port_spin.setValue(17911)
        self.vr_ptt_timeout_spin = _NoWheelAlwaysDoubleSpinBox()
        self.vr_ptt_timeout_spin.setRange(0.2, 10.0)
        self.vr_ptt_timeout_spin.setValue(1.5)
        self.vr_ptt_timeout_spin.setDecimals(1)
        self.vr_ptt_timeout_spin.setSuffix(" 秒")
        self.vr_ptt_token_edit = QLineEdit()
        self.vr_ptt_token_edit.setMaximumWidth(130)
        ptt_row.addWidget(self.vr_ptt_enabled_chk)
        ptt_row.addWidget(QLabel("bind"))
        ptt_row.addWidget(self.vr_ptt_host_edit)
        ptt_row.addWidget(QLabel("port"))
        ptt_row.addWidget(self.vr_ptt_port_spin)
        ptt_row.addWidget(QLabel("timeout"))
        ptt_row.addWidget(self.vr_ptt_timeout_spin)
        ptt_row.addWidget(QLabel("token"))
        ptt_row.addWidget(self.vr_ptt_token_edit)
        ptt_row.addStretch(1)
        ptt_w = QWidget(); ptt_w.setLayout(ptt_row)
        form.addRow("VRプッシュトーク", ptt_w)

        self._reload_devices()

    def _build_pipeline_tab(self) -> None:
        inner = QWidget()
        inner.setMinimumWidth(900)
        scroll = QScrollArea()
        scroll.setWidget(inner)
        scroll.setWidgetResizable(True)
        from PyQt6.QtCore import Qt
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.tabs.addTab(scroll, "パイプライン設定")
        form = QFormLayout(inner)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)

        self.kks_root_edit = QLineEdit()
        kks_btn = QPushButton("参照")
        kks_btn.clicked.connect(lambda: self._pick_dir(self.kks_root_edit, "KKSフォルダ"))
        form.addRow("KKSフォルダ", self._hrow(self.kks_root_edit, kks_btn))

        self.output_dir_edit = QLineEdit(str(PROJECT_ROOT / "outputs"))
        out_btn = QPushButton("参照")
        out_btn.clicked.connect(lambda: self._pick_dir(self.output_dir_edit, "出力先"))
        form.addRow("出力先", self._hrow(self.output_dir_edit, out_btn))

        save_row = QHBoxLayout()
        self.save_faster_text_chk = QCheckBox("Whisperテキスト保存")
        self.save_faster_text_chk.setChecked(True)
        self.save_source_wav_chk = QCheckBox("元WAV保存")
        self.save_source_wav_chk.setChecked(False)
        self.save_sbv2_text_chk = QCheckBox("SBV2入力テキスト保存")
        self.save_sbv2_text_chk.setChecked(True)
        self.save_sbv2_wav_chk = QCheckBox("SBV2生成WAV保存")
        self.save_sbv2_wav_chk.setChecked(False)
        save_row.addWidget(self.save_faster_text_chk)
        save_row.addWidget(self.save_source_wav_chk)
        save_row.addWidget(self.save_sbv2_text_chk)
        save_row.addWidget(self.save_sbv2_wav_chk)
        save_row.addStretch(1)
        save_w = QWidget(); save_w.setLayout(save_row)
        form.addRow("保存設定", save_w)

        behavior_row = QHBoxLayout()
        self.max_response_chars_enabled_chk = QCheckBox("応答文字数制限")
        self.max_response_chars_enabled_chk.setChecked(True)
        self.max_response_chars_spin = _NoWheelSpinBox()
        self.max_response_chars_spin.setRange(1, 20000)
        self.max_response_chars_spin.setValue(3000)
        self.max_response_chars_spin.setSuffix(" 文字")
        self.max_response_chars_spin.setEnabled(True)
        self.max_response_chars_enabled_chk.toggled.connect(self.max_response_chars_spin.setEnabled)
        self.diagnostic_log_enabled_chk = QCheckBox("入力診断ログ")
        self.diagnostic_log_enabled_chk.setChecked(False)
        behavior_row.addWidget(self.max_response_chars_enabled_chk)
        behavior_row.addWidget(self.max_response_chars_spin)
        behavior_row.addWidget(self.diagnostic_log_enabled_chk)
        behavior_row.addStretch(1)
        behavior_w = QWidget(); behavior_w.setLayout(behavior_row)
        form.addRow("動作設定", behavior_w)

        _local_py = str(PROJECT_ROOT / "python" / "python.exe")
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

        llm_row = QHBoxLayout()
        self.llm_backend_combo = _NoWheelComboBox()
        self.llm_backend_combo.addItem("Grokブラウザ", "grok_browser")
        self.llm_backend_combo.addItem("ローカルLLM(OpenAI互換)", "local_openai")
        self.llm_base_url_edit = QLineEdit("http://127.0.0.1:1234/v1")
        self.llm_base_url_edit.setPlaceholderText("LM Studio: http://127.0.0.1:1234/v1")
        self.llm_model_edit = QLineEdit("")
        self.llm_model_edit.setPlaceholderText("model id")
        llm_row.addWidget(QLabel("backend"))
        llm_row.addWidget(self.llm_backend_combo)
        llm_row.addWidget(QLabel("base"))
        llm_row.addWidget(self.llm_base_url_edit, 1)
        llm_row.addWidget(QLabel("model"))
        llm_row.addWidget(self.llm_model_edit, 1)
        llm_w = QWidget(); llm_w.setLayout(llm_row)
        form.addRow("LLM", llm_w)

        llm_detail_row = QHBoxLayout()
        self.llm_api_key_edit = QLineEdit("lm-studio")
        self.llm_api_key_edit.setPlaceholderText("local API key")
        self.llm_temperature_spin = _NoWheelAlwaysDoubleSpinBox()
        self.llm_temperature_spin.setRange(0.0, 2.0)
        self.llm_temperature_spin.setDecimals(2)
        self.llm_temperature_spin.setSingleStep(0.05)
        self.llm_temperature_spin.setValue(0.7)
        self.llm_max_tokens_spin = _NoWheelSpinBox()
        self.llm_max_tokens_spin.setRange(1, 32768)
        self.llm_max_tokens_spin.setValue(512)
        self.llm_timeout_spin = _NoWheelAlwaysDoubleSpinBox()
        self.llm_timeout_spin.setRange(1.0, 600.0)
        self.llm_timeout_spin.setDecimals(1)
        self.llm_timeout_spin.setValue(120.0)
        self.llm_timeout_spin.setSuffix(" 秒")
        llm_detail_row.addWidget(QLabel("key"))
        llm_detail_row.addWidget(self.llm_api_key_edit, 1)
        llm_detail_row.addWidget(QLabel("temp"))
        llm_detail_row.addWidget(self.llm_temperature_spin)
        llm_detail_row.addWidget(QLabel("max_tokens"))
        llm_detail_row.addWidget(self.llm_max_tokens_spin)
        llm_detail_row.addWidget(QLabel("timeout"))
        llm_detail_row.addWidget(self.llm_timeout_spin)
        llm_detail_w = QWidget(); llm_detail_w.setLayout(llm_detail_row)
        form.addRow("LLM詳細", llm_detail_w)

        self.llm_system_prompt_edit = QPlainTextEdit()
        self.llm_system_prompt_edit.setPlaceholderText("ローカルLLM用system prompt（Grokブラウザ時は未使用）")
        self.llm_system_prompt_edit.setMaximumHeight(70)
        form.addRow("LLM system", self.llm_system_prompt_edit)

        self.sbv2_root_edit = QLineEdit()
        sbv2_btn = QPushButton("参照")
        sbv2_btn.clicked.connect(lambda: self._pick_dir(self.sbv2_root_edit, "SBV2フォルダ"))
        form.addRow("SBV2フォルダ", self._hrow(self.sbv2_root_edit, sbv2_btn))

        self.video_metadata_edit = QLineEdit("")
        meta_btn = QPushButton("参照")
        meta_btn.clicked.connect(lambda: self._pick_file(self.video_metadata_edit, "動画メタデータJSON"))
        form.addRow("動画メタデータJSON", self._hrow(self.video_metadata_edit, meta_btn))

        sbv2_server_row = QHBoxLayout()
        self.sbv2_mode_combo = _NoWheelComboBox()
        self.sbv2_mode_combo.addItem("自動", "auto")
        self.sbv2_mode_combo.addItem("HTTPサーバー", "http")
        self.sbv2_mode_combo.addItem("ローカル実行", "local")
        self.sbv2_mode_combo.setCurrentIndex(0)
        self.sbv2_server_url_edit = QLineEdit("http://127.0.0.1:5000")
        self.sbv2_auto_start_chk = QCheckBox("自動起動")
        self.sbv2_auto_start_chk.setChecked(True)
        sbv2_server_row.addWidget(QLabel("mode"))
        sbv2_server_row.addWidget(self.sbv2_mode_combo)
        sbv2_server_row.addWidget(self.sbv2_server_url_edit, 1)
        sbv2_server_row.addWidget(self.sbv2_auto_start_chk)
        sbv2_server_w = QWidget(); sbv2_server_w.setLayout(sbv2_server_row)
        form.addRow("SBV2サーバーURL", sbv2_server_w)
        self.sbv2_mode_combo.currentIndexChanged.connect(self._on_sbv2_mode_changed)
        self._on_sbv2_mode_changed()

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

        face_mode_row = QHBoxLayout()
        self.face_mode_combo = _NoWheelComboBox()
        self.face_mode_combo.addItem("ゲームプリセット", "game_preset")
        self.face_mode_combo.addItem("FacePreset Name", "preset_name")
        self.face_mode_combo.setCurrentIndex(0)
        self.face_preset_name_combo = _NoWheelComboBox()
        self.face_preset_name_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.face_preset_random_chk = QCheckBox("ランダム送信")
        self.face_preset_reload_btn = QPushButton("更新")
        self.face_preset_reload_btn.clicked.connect(self._reload_face_preset_names)
        face_mode_row.addWidget(QLabel("mode"))
        face_mode_row.addWidget(self.face_mode_combo)
        face_mode_row.addWidget(QLabel("presetName"))
        face_mode_row.addWidget(self.face_preset_name_combo, 2)
        face_mode_row.addWidget(self.face_preset_random_chk)
        face_mode_row.addWidget(self.face_preset_reload_btn)
        fm = QWidget(); fm.setLayout(face_mode_row)
        form.addRow("表情送信方式", fm)
        self.face_mode_combo.currentIndexChanged.connect(self._on_face_send_mode_changed)
        self.face_preset_random_chk.toggled.connect(self._on_face_send_mode_changed)
        self._reload_face_preset_names(keep_selection=False)
        self._on_face_send_mode_changed()

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
        scroll = QScrollArea(); scroll.setWidget(tab); scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tabs.addTab(scroll, "テスト")
        layout = QVBoxLayout(tab)

        fw_group = QGroupBox("FasterWhisper テスト")
        fw_layout = QVBoxLayout(fw_group)
        fw_layout.addWidget(QLabel("ボタンを押している間だけ録音。離すと文字起こしします。"))
        self.fw_test_hold_btn = QPushButton("押して話す（離して判定）")
        self.fw_test_hold_btn.pressed.connect(self._fw_test_start_record)
        self.fw_test_hold_btn.released.connect(self._fw_test_stop_record)
        fw_layout.addWidget(self.fw_test_hold_btn)
        self.fw_test_text_edit = QPlainTextEdit()
        self.fw_test_text_edit.setPlaceholderText("手入力テキスト（FasterWhisper結果の代わりに変換テスト）")
        self.fw_test_text_edit.setMaximumHeight(84)
        fw_layout.addWidget(self.fw_test_text_edit)
        fw_text_row = QHBoxLayout()
        self.fw_test_text_btn = QPushButton("入力テキストでテスト")
        self.fw_test_text_btn.clicked.connect(self._run_fw_test_manual_text)
        fw_text_row.addWidget(self.fw_test_text_btn)
        fw_text_row.addStretch()
        fw_layout.addLayout(fw_text_row)
        self.fw_test_status_label = QLabel("待機")
        fw_layout.addWidget(self.fw_test_status_label)
        fw_result_row = QHBoxLayout()
        fw_raw_panel, self.fw_test_original_edit = self._make_labeled_result_edit("原文", "文字起こしの生テキスト")
        fw_send_panel, self.fw_test_send_edit = self._make_labeled_result_edit(
            "送信用",
            "変換後（Grok送信用）",
            action_text="送信",
            action_callback=self._send_fw_test_send_text,
        )
        fw_disp_panel, self.fw_test_display_edit = self._make_labeled_result_edit("表示用", "変換後（表示用）")
        fw_result_row.addWidget(fw_raw_panel, 1)
        fw_result_row.addWidget(fw_send_panel, 1)
        fw_result_row.addWidget(fw_disp_panel, 1)
        fw_layout.addLayout(fw_result_row, 1)
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
        self.sbv2_test_presend_btn = QPushButton("送信前テスト")
        self.sbv2_test_presend_btn.clicked.connect(self._run_sbv2_presend_test)
        self.sbv2_test_run_btn = QPushButton("SBV2テスト実行")
        self.sbv2_test_run_btn.clicked.connect(self._run_sbv2_test)
        self.sbv2_test_play_btn = QPushButton("最後の音声をGUI再生")
        self.sbv2_test_play_btn.clicked.connect(self._play_last_sbv2_test)
        btn_row.addWidget(self.sbv2_test_presend_btn)
        btn_row.addWidget(self.sbv2_test_run_btn)
        btn_row.addWidget(self.sbv2_test_play_btn)
        btn_row.addStretch()
        sbv2_layout.addLayout(btn_row)

        self.sbv2_test_status_label = QLabel("待機")
        sbv2_layout.addWidget(self.sbv2_test_status_label)
        sbv2_result_row = QHBoxLayout()
        sbv2_raw_panel, self.sbv2_test_original_edit = self._make_labeled_result_edit("原文", "SBV2入力の元テキスト")
        sbv2_send_panel, self.sbv2_test_send_edit = self._make_labeled_result_edit("送信用", "変換後（SBV2へ渡す）")
        sbv2_disp_panel, self.sbv2_test_display_edit = self._make_labeled_result_edit("表示用", "変換後（字幕/表示）")
        sbv2_result_row.addWidget(sbv2_raw_panel, 1)
        sbv2_result_row.addWidget(sbv2_send_panel, 1)
        sbv2_result_row.addWidget(sbv2_disp_panel, 1)
        sbv2_layout.addLayout(sbv2_result_row, 1)
        layout.addWidget(sbv2_group, 1)

    def _on_sbv2_test_keep_face_toggled(self, checked: bool) -> None:
        self.sbv2_test_face_spin.setEnabled(not checked)

    def _default_face_preset_json_path(self) -> Path:
        kks_root_text = self.kks_root_edit.text().strip()
        if kks_root_text:
            root = Path(kks_root_text).expanduser()
        else:
            try:
                root = PROJECT_ROOT.parents[2]
            except Exception:
                root = PROJECT_ROOT
        return (root / "BepInEx" / "plugins" / "StudioFacePresetTool" / "StudioFacePresets.json").resolve()

    def _reload_face_preset_names(self, *_args, keep_selection: bool = True) -> None:
        combo = self.face_preset_name_combo
        prev_data = combo.currentData() if keep_selection else None
        prev_name = ""
        prev_id = ""
        if isinstance(prev_data, dict):
            prev_name = str(prev_data.get("name", "")).strip()
            prev_id = str(prev_data.get("id", "")).strip()

        preset_path = self._default_face_preset_json_path()
        rows: list[dict] = []
        err = ""
        try:
            if preset_path.exists():
                raw = json.loads(preset_path.read_text(encoding="utf-8"))
                presets = raw.get("Presets", []) if isinstance(raw, dict) else []
                if isinstance(presets, list):
                    for entry in presets:
                        if not isinstance(entry, dict):
                            continue
                        name = str(entry.get("Name", "")).strip()
                        preset_id = str(entry.get("Id", "")).strip()
                        if name:
                            rows.append({"name": name, "id": preset_id})
            else:
                err = f"not found: {preset_path}"
        except Exception as exc:
            err = str(exc)

        self._face_preset_rows = rows
        name_counts: dict[str, int] = {}
        for row in rows:
            name = str(row.get("name", "")).strip()
            if not name:
                continue
            name_counts[name] = int(name_counts.get(name, 0)) + 1

        combo.blockSignals(True)
        combo.clear()
        if rows:
            for row in rows:
                name = str(row.get("name", "")).strip()
                preset_id = str(row.get("id", "")).strip()
                if not name:
                    continue
                if name_counts.get(name, 0) > 1 and preset_id:
                    label = f"{name} ({preset_id[:8]})"
                else:
                    label = name
                combo.addItem(label, {"name": name, "id": preset_id})
        else:
            combo.addItem("(FacePreset未検出)", {"name": "", "id": ""})
        combo.blockSignals(False)

        if keep_selection and (prev_name or prev_id):
            self._select_face_preset(prev_name, prev_id)
        elif rows:
            combo.setCurrentIndex(0)
        else:
            combo.setCurrentIndex(0)

        if hasattr(self, "log_text"):
            if err:
                self._append_log(f"[face-preset] load failed: {err}")
            else:
                self._append_log(f"[face-preset] loaded count={len(rows)} path={preset_path}")

    def _select_face_preset(self, preset_name: str, preset_id: str = "") -> None:
        combo = self.face_preset_name_combo
        target_name = str(preset_name or "").strip()
        target_id = str(preset_id or "").strip()
        if combo.count() <= 0:
            return
        if not target_name and not target_id:
            combo.setCurrentIndex(0)
            return
        for idx in range(combo.count()):
            data = combo.itemData(idx)
            if not isinstance(data, dict):
                continue
            row_name = str(data.get("name", "")).strip()
            row_id = str(data.get("id", "")).strip()
            if target_id and row_id and row_id == target_id:
                combo.setCurrentIndex(idx)
                return
            if target_name and row_name == target_name:
                combo.setCurrentIndex(idx)
                return
        combo.setCurrentIndex(0)

    def _on_face_send_mode_changed(self, *_args) -> None:
        mode = str(self.face_mode_combo.currentData() or self.face_mode_combo.currentText()).strip().lower()
        use_preset_name = mode in ("preset_name", "preset_id")
        random_enabled = bool(self.face_preset_random_chk.isChecked())
        self.face_spin.setEnabled(not use_preset_name)
        self.keep_face_chk.setEnabled(not use_preset_name)
        self.face_preset_name_combo.setEnabled(use_preset_name and (not random_enabled))
        self.face_preset_random_chk.setEnabled(use_preset_name)
        self.face_preset_reload_btn.setEnabled(use_preset_name and (not random_enabled))

    @staticmethod
    def _normalize_sbv2_mode(value: object) -> str:
        token = str(value or "").strip().lower()
        if token in ("auto", "http", "local"):
            return token
        return "auto"

    @classmethod
    def _sbv2_use_http_mode(cls, cfg: AppConfig) -> bool:
        mode = cls._normalize_sbv2_mode(getattr(cfg, "sbv2_mode", "auto"))
        if mode == "http":
            return True
        if mode == "local":
            return False
        return bool((cfg.sbv2_server_url or "").strip())

    def _on_sbv2_mode_changed(self, *_args) -> None:
        mode = self._normalize_sbv2_mode(
            self.sbv2_mode_combo.currentData() if self.sbv2_mode_combo.currentData() is not None else self.sbv2_mode_combo.currentText()
        )
        use_http_controls = mode in ("auto", "http")
        self.sbv2_server_url_edit.setEnabled(use_http_controls)
        self.sbv2_auto_start_chk.setEnabled(use_http_controls)

    def _on_sbv2_test_face_changed(self, value: int) -> None:
        if value >= 0 and self.sbv2_test_keep_face_chk.isChecked():
            self.sbv2_test_keep_face_chk.setChecked(False)

    def _on_sbv2_test_volume_changed(self, value: int) -> None:
        self.sbv2_test_volume_label.setText(f"{value}%")

    @staticmethod
    def _parse_enabled_value(value: object, default: bool = True) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            token = value.strip().lower()
            if token in ("", "0", "false", "off", "no"):
                return False
            if token in ("1", "true", "on", "yes"):
                return True
        return default

    @staticmethod
    def _apply_text_conversion_rules(text: str, rules: list[dict], mode: str) -> str:
        converted = str(text or "")
        target = "display" if str(mode).strip().lower() == "display" else "send"
        ordered_rules: list[tuple[int, str, dict]] = []
        for idx, row in enumerate(list(rules or [])):
            if not isinstance(row, dict):
                continue
            if not MainWindow._parse_enabled_value(row.get("enabled", True), True):
                continue
            src = str(row.get("from", ""))
            if not src:
                continue
            ordered_rules.append((idx, src, row))
        ordered_rules.sort(key=lambda x: (-len(x[1]), x[0]))

        for _idx, src, row in ordered_rules:
            if target == "display":
                if not bool(row.get("display_apply", True)):
                    continue
                if "to_display" in row:
                    dst = str(row.get("to_display", ""))
                elif "to" in row:
                    dst = str(row.get("to", ""))
                else:
                    dst = str(row.get("to_grok", row.get("to_sbv2", "")))
            else:
                if "to_grok" in row:
                    dst = str(row.get("to_grok", ""))
                elif "to_sbv2" in row:
                    dst = str(row.get("to_sbv2", ""))
                elif "to" in row:
                    dst = str(row.get("to", ""))
                else:
                    dst = str(row.get("to_display", ""))
            converted = converted.replace(src, dst)
        return converted

    @staticmethod
    def _make_labeled_result_edit(
        title: str,
        placeholder: str,
        action_text: str = "",
        action_callback: Optional[Callable[[], None]] = None,
    ) -> tuple[QWidget, QPlainTextEdit]:
        panel = QWidget()
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        header = QHBoxLayout()
        header.addWidget(QLabel(title))
        if action_text and action_callback is not None:
            action_btn = QPushButton(action_text)
            action_btn.setMaximumWidth(84)
            action_btn.clicked.connect(action_callback)
            header.addWidget(action_btn)
        else:
            spacer_btn = QLabel("")
            spacer_btn.setFixedHeight(QPushButton().sizeHint().height())
            header.addWidget(spacer_btn)
        header.addStretch()
        panel_layout.addLayout(header)
        edit = QPlainTextEdit()
        edit.setReadOnly(True)
        edit.setPlaceholderText(placeholder)
        edit.setMinimumHeight(edit.minimumSizeHint().height() + 22)
        panel_layout.addWidget(edit, 1)
        return panel, edit

    def _send_fw_test_send_text(self) -> None:
        text = self.fw_test_send_edit.toPlainText().strip()
        if not text or text == "(空テキスト)":
            self.fw_test_status_label.setText("送信用テキストが空です")
            return
        if self._send_text_to_pipeline(text, "fw-test送信"):
            self.fw_test_status_label.setText("送信完了（送信用）")

    def _enter_fw_test_guard(self) -> None:
        if self._fw_test_guard_active:
            return
        self._fw_test_guard_active = True
        self._fw_test_guard_prev_paused = bool(self._paused)
        if (not self._running) or self._fw_test_guard_prev_paused:
            return
        if self._pipeline_worker is not None:
            self._pipeline_worker.pause()
        self._stop_recorder()
        self._append_log("[test-guard] FWテスト中: 本録音を一時無効化")

    def _leave_fw_test_guard(self) -> None:
        if not self._fw_test_guard_active:
            return
        was_paused = self._fw_test_guard_prev_paused
        self._fw_test_guard_active = False
        self._fw_test_guard_prev_paused = False
        if (not self._running) or was_paused:
            return
        if self._pipeline_worker is not None:
            self._pipeline_worker.resume()
        try:
            cfg = self._build_config()
            if self._is_recorder_needed(cfg):
                self._start_recorder(cfg)
        except Exception as exc:
            self._append_log(f"[test-guard] FWテスト復帰失敗: {exc}")
            return
        self._append_log("[test-guard] FWテスト終了: 本録音を復帰")

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

    def _current_session_id(self) -> str:
        sid = str(self._run_session_id or "").strip()
        if sid:
            return sid
        return datetime.now().strftime("%Y%m%d_%H%M%S")

    def _cleanup_fw_test_wav(self, payload: Optional[dict] = None) -> None:
        try:
            if self.save_source_wav_chk.isChecked():
                self._fw_test_temp_wav = None
                return
        except Exception:
            pass

        target: Optional[Path] = None
        if isinstance(payload, dict):
            audio_path = str(payload.get("audio_path", "")).strip()
            if audio_path:
                try:
                    target = Path(audio_path).resolve()
                except Exception:
                    target = None
        if target is None:
            target = self._fw_test_temp_wav
        self._fw_test_temp_wav = None
        if target is None:
            return
        try:
            target.unlink(missing_ok=True)
            self._append_log(f"[fw-test] temp wav cleaned: {target.name}")
        except Exception as exc:
            self._append_log(f"[fw-test] temp wav cleanup failed: {exc}")

    def _fw_test_start_record(self) -> None:
        if self._fw_test_worker is not None and self._fw_test_worker.isRunning():
            self.fw_test_status_label.setText("文字起こし中...")
            return
        if self._fw_test_stream is not None:
            return
        self._enter_fw_test_guard()
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
            self._leave_fw_test_guard()

    def _fw_test_stop_record(self) -> None:
        if self._fw_test_stream is None:
            self._leave_fw_test_guard()
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
            self._leave_fw_test_guard()
            return

        pcm = np.concatenate(self._fw_test_chunks, axis=0).reshape(-1)
        self._fw_test_chunks = []
        duration = len(pcm) / float(self._fw_test_sr)
        if duration < 0.2:
            self.fw_test_status_label.setText("録音が短すぎます")
            self._leave_fw_test_guard()
            return

        session_dir = f"session_{self._current_session_id()}"
        out_root = Path(self.output_dir_edit.text().strip()).expanduser().resolve() / "tests" / "fasterwhisper" / session_dir
        wav_path = out_root / f"hold_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')[:-3]}.wav"
        self._write_wav_float32_mono(wav_path, pcm, self._fw_test_sr)
        self._fw_test_temp_wav = wav_path
        self.fw_test_status_label.setText("文字起こし中...")
        self._append_log(f"[fw-test] recorded {wav_path} ({duration:.2f}s)")
        self._leave_fw_test_guard()
        self._fw_test_worker = _TaskWorker(lambda: self._run_fw_test_transcribe(wav_path))
        self._fw_test_worker.result_ready.connect(self._on_fw_test_transcribe_done)
        self._fw_test_worker.error_occurred.connect(self._on_fw_test_transcribe_error)
        self._fw_test_worker.start()

    def _run_fw_test_manual_text(self) -> None:
        if self._fw_test_worker is not None and self._fw_test_worker.isRunning():
            self.fw_test_status_label.setText("文字起こし中...")
            return
        raw_text = self.fw_test_text_edit.toPlainText().strip()
        if not raw_text:
            self.fw_test_status_label.setText("入力テキストが空です")
            return
        try:
            cfg = self._build_config()
        except Exception as exc:
            self.fw_test_status_label.setText("設定エラー")
            self._append_log(f"[fw-test] text config error: {exc}")
            return

        text_send = self._apply_text_conversion_rules(raw_text, cfg.transcribe_conversion_dict, mode="send").strip()
        text_display = self._apply_text_conversion_rules(raw_text, cfg.transcribe_conversion_dict, mode="display").strip()
        self.fw_test_original_edit.setPlainText(raw_text or "(空テキスト)")
        self.fw_test_send_edit.setPlainText(text_send or "(空テキスト)")
        self.fw_test_display_edit.setPlainText(text_display or "(空テキスト)")
        self.fw_test_status_label.setText("完了（手入力）")
        self._append_log(f"[fw-test] text original: {raw_text[:80]}")
        self._append_log(f"[fw-test] text send: {text_send[:80]}")
        self._append_log(f"[fw-test] text display: {text_display[:80]}")

    def _run_fw_test_transcribe(self, wav_path: Path) -> dict:
        cfg = self._build_config()
        script = PROJECT_ROOT / "run_transcribe_one_wav.py"
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
        raw_text = str(payload.get("text", "")).strip()
        payload["text_original"] = raw_text
        payload["text_send"] = self._apply_text_conversion_rules(raw_text, cfg.transcribe_conversion_dict, mode="send")
        payload["text_display"] = self._apply_text_conversion_rules(raw_text, cfg.transcribe_conversion_dict, mode="display")
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
            text_original = str(data.get("text_original", data.get("text", ""))).strip()
            text_send = str(data.get("text_send", text_original)).strip()
            text_display = str(data.get("text_display", text_original)).strip()
            self.fw_test_original_edit.setPlainText(text_original or "(空テキスト)")
            self.fw_test_send_edit.setPlainText(text_send or "(空テキスト)")
            self.fw_test_display_edit.setPlainText(text_display or "(空テキスト)")
            self.fw_test_status_label.setText("完了")
            self._append_log(f"[fw-test] original: {text_original[:80]}")
            self._append_log(f"[fw-test] send: {text_send[:80]}")
            self._append_log(f"[fw-test] display: {text_display[:80]}")
        else:
            err = str(data.get("error", "unknown error"))
            self.fw_test_original_edit.setPlainText("")
            self.fw_test_send_edit.setPlainText("")
            self.fw_test_display_edit.setPlainText("")
            self.fw_test_status_label.setText("失敗")
            self._append_log(f"[fw-test] failed: {err}")
        self._cleanup_fw_test_wav(data)

    def _on_fw_test_transcribe_error(self, err: str) -> None:
        self._fw_test_worker = None
        self.fw_test_status_label.setText("失敗")
        self._append_log(f"[fw-test] worker error: {err}")
        self._cleanup_fw_test_wav()

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

    def _cleanup_sbv2_test_run_dir(self, run_dir: Optional[Path]) -> None:
        if run_dir is None:
            return
        try:
            safe_dir = run_dir.resolve()
        except Exception:
            return
        try:
            # 安全ガード: grok_tts_outputs/grok_tts_* 以外は削除しない
            if safe_dir.name.startswith("grok_tts_") and safe_dir.parent.name == "grok_tts_outputs":
                shutil.rmtree(safe_dir, ignore_errors=True)
                self._append_log(f"[sbv2-test] temp run cleaned: {safe_dir.name}")
        except Exception as exc:
            self._append_log(f"[sbv2-test] temp run cleanup failed: {exc}")

    def _run_sbv2_test_task(
        self,
        cfg: AppConfig,
        text: str,
        no_send_event: bool = False,
        keep_current_face_override: Optional[bool] = None,
        face_override: Optional[int] = None,
    ) -> dict:
        script = PROJECT_ROOT / "run_grok_tts_event.py"
        if not script.exists():
            raise FileNotFoundError(f"script not found: {script}")
        response_limit = max(3000, max(1, int(cfg.max_response_chars))) if cfg.max_response_chars_enabled else 0
        cmd = [
            str(cfg.pipeline_python), str(script),
            "--response-text", text,
            "--max-response-chars", str(response_limit),
            "--sbv2-root", str(cfg.sbv2_root),
            "--model-name", cfg.sbv2_model_name,
            "--speaker", cfg.sbv2_speaker,
            "--style", cfg.sbv2_style,
            "--length", str(cfg.sbv2_length),
            "--output-dir", str(cfg.output_dir / "grok_tts_outputs"),
            "--pipe-name", cfg.pipe_name,
            "--main", str(cfg.main_index),
        ]
        sender_ps1 = PROJECT_ROOT / "send_voice_face_event.ps1"
        if sender_ps1.exists():
            cmd.extend(["--event-sender", str(sender_ps1)])
        target_host = cfg.target_host.strip()
        if cfg.remote_http and target_host:
            cmd.append("--remote-http")
        if target_host:
            cmd.extend([
                "--target-host", target_host,
                "--target-port", str(cfg.target_port),
                "--target-endpoint", cfg.target_endpoint,
            ])
            if cfg.target_token.strip():
                cmd.extend(["--target-token", cfg.target_token.strip()])
        face_send_mode = str(getattr(cfg, "face_send_mode", "game_preset") or "game_preset").strip().lower()
        if face_send_mode not in ("game_preset", "preset_name", "preset_id"):
            face_send_mode = "game_preset"
        if face_send_mode == "preset_id":
            face_send_mode = "preset_name"
        cmd.extend(["--face-send-mode", face_send_mode])

        if face_send_mode == "preset_name":
            preset_name = str(getattr(cfg, "face_preset_name", "") or "").strip()
            preset_id = str(getattr(cfg, "face_preset_id", "") or "").strip()
            preset_random = bool(getattr(cfg, "face_preset_random", False))
            if preset_random:
                cmd.append("--face-preset-random")
                preset_name = ""
                preset_id = ""
            if preset_name:
                cmd.extend(["--face-preset-name", preset_name])
            if preset_id:
                cmd.extend(["--face-preset-id", preset_id])
            if (not preset_random) and (not preset_name) and (not preset_id):
                raise RuntimeError("face_send_mode=preset_name ですが face_preset_name が未設定です")
        else:
            effective_keep_current_face = (
                bool(keep_current_face_override)
                if keep_current_face_override is not None
                else bool(cfg.keep_current_face)
            )
            effective_face = int(face_override) if face_override is not None else int(cfg.face)
            if effective_keep_current_face:
                cmd.append("--keep-current-face")
            elif effective_face >= 0:
                cmd.extend(["--face", str(effective_face)])
        if cfg.voice_volume >= 0:
            cmd.extend(["--voice-volume", str(cfg.voice_volume)])
        if cfg.voice_pitch >= 0:
            cmd.extend(["--voice-pitch", str(cfg.voice_pitch)])
        if cfg.sbv2_model_file:
            cmd.extend(["--model-file", cfg.sbv2_model_file])
        if self._sbv2_use_http_mode(cfg) and cfg.sbv2_server_url:
            cmd.extend(["--sbv2-server-url", cfg.sbv2_server_url])
        if cfg.conversion_dict:
            cmd.extend(["--conversion-json", json.dumps(list(cfg.conversion_dict), ensure_ascii=False)])
        if no_send_event:
            cmd.append("--no-send-event")
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
        response_original = str(payload.get("response_original", text))
        payload.setdefault("response_original", response_original)
        payload.setdefault("response", self._apply_text_conversion_rules(response_original, cfg.conversion_dict, mode="send"))
        payload.setdefault("response_display", self._apply_text_conversion_rules(response_original, cfg.conversion_dict, mode="display"))
        payload["no_send_event"] = bool(no_send_event)
        payload["returncode"] = proc.returncode
        return payload

    def _run_sbv2_presend_test(self) -> None:
        self._run_sbv2_test(no_send_event=True)

    def _run_sbv2_test(self, *, no_send_event: bool = False) -> None:
        if self._sbv2_test_worker is not None and self._sbv2_test_worker.isRunning():
            self.sbv2_test_status_label.setText("実行中...")
            return
        text = self.sbv2_test_text_edit.toPlainText().strip()
        if not text:
            self.sbv2_test_status_label.setText("テキスト未入力")
            return
        if self._pipeline_worker is None:
            self.sbv2_test_status_label.setText("先に開始してください")
            self._append_log("[sbv2-test] pipeline not running")
            return

        cfg = self._active_runtime_cfg
        if cfg is None:
            self.sbv2_test_status_label.setText("実行設定がありません")
            self._append_log("[sbv2-test] active runtime config missing")
            return

        test_keep_face = bool(self.sbv2_test_keep_face_chk.isChecked())
        test_face = int(self.sbv2_test_face_spin.value())
        face_send_mode = str(getattr(cfg, "face_send_mode", "game_preset") or "game_preset").strip().lower()
        if face_send_mode not in ("game_preset", "preset_name", "preset_id"):
            face_send_mode = "game_preset"
        if face_send_mode == "preset_id":
            face_send_mode = "preset_name"
        face_preset_name = str(getattr(cfg, "face_preset_name", "") or "").strip()
        face_preset_id = str(getattr(cfg, "face_preset_id", "") or "").strip()
        face_preset_random = bool(getattr(cfg, "face_preset_random", False))
        target_host = cfg.target_host.strip() or "(pipe-local)"
        self._append_log(
            "[sbv2-test] use-sbv2-pre-step "
            f"pipe={cfg.pipe_name} main={cfg.main_index} "
            f"face_mode={face_send_mode} face_preset_name={face_preset_name or '(empty)'} "
            f"face_preset_id={face_preset_id or '(empty)'} random={int(face_preset_random)} "
            f"test_face={test_face} test_keep_face={test_keep_face} "
            f"base_face={cfg.face} base_keep_face={cfg.keep_current_face} "
            f"vol={cfg.voice_volume} pitch={cfg.voice_pitch} "
            f"target={target_host}:{cfg.target_port}{cfg.target_endpoint} "
            f"max_response_chars={cfg.max_response_chars} enabled={int(cfg.max_response_chars_enabled)}"
        )
        request_limit = max(3000, max(1, int(cfg.max_response_chars))) if cfg.max_response_chars_enabled else 0
        self._append_log(
            f"[sbv2-test][grok-limit] request text_len={len(text)} max={request_limit}"
        )

        self._sbv2_test_no_send = bool(no_send_event)
        self.sbv2_test_status_label.setText("送信前テスト実行中..." if no_send_event else "SBV2テスト実行中...")
        self._sbv2_test_worker = _TaskWorker(
            lambda: self._run_sbv2_test_task(
                cfg,
                text,
                no_send_event,
                keep_current_face_override=test_keep_face,
                face_override=test_face,
            )
        )
        self._sbv2_test_worker.result_ready.connect(self._on_sbv2_test_done)
        self._sbv2_test_worker.error_occurred.connect(self._on_sbv2_test_error)
        self._sbv2_test_worker.start()

    def _on_sbv2_test_done(self, payload: object) -> None:
        self._sbv2_test_worker = None
        no_send_event = self._sbv2_test_no_send
        self._sbv2_test_no_send = False
        data = payload if isinstance(payload, dict) else {}
        raw_len = int(data.get("response_raw_length", 0) or 0)
        capped_len = int(data.get("response_capped_length", 0) or 0)
        truncated = bool(data.get("response_truncated", False))
        max_chars = int(data.get("max_response_chars", 0) or 0)
        line_count = int(data.get("line_count", 0) or 0)
        self._append_log(
            f"[sbv2-test][grok-limit] max={max_chars} raw_len={raw_len} capped_len={capped_len} truncated={int(truncated)}"
        )
        if truncated:
            self._append_log(f"[sbv2-test][grok-limit] truncated_chars={max(0, raw_len - capped_len)}")
        self._append_log(f"[sbv2-test][grok-limit] line_count={line_count}")
        self._append_log(
            "[sbv2-test][event-face] "
            f"mode={str(data.get('event_face_send_mode', '') or '').strip() or '(unknown)'} "
            f"sent={int(bool(data.get('event_sent', False)))} "
            f"preset_name={str(data.get('event_face_preset_name', '') or '').strip() or '(empty)'} "
            f"preset_id={str(data.get('event_face_preset_id', '') or '').strip() or '(empty)'} "
            f"random={int(bool(data.get('event_face_preset_random', False)))} "
            f"picked_name={str(data.get('event_face_selected_name', '') or '').strip() or '(empty)'} "
            f"picked_id={str(data.get('event_face_selected_id', '') or '').strip() or '(empty)'} "
            f"face={int(data.get('event_face', -1) or -1)} "
            f"keep_current_face={int(bool(data.get('event_keep_current_face', False)))}"
        )
        event_stderr = str(data.get("event_stderr", "") or "").strip()
        if event_stderr:
            self._append_log(f"[sbv2-test][event-face][stderr] {event_stderr[:240]}")
        response_original = str(data.get("response_original", "")).strip()
        response_send = str(data.get("response", response_original)).strip()
        response_display = str(data.get("response_display", response_original)).strip()
        self.sbv2_test_original_edit.setPlainText(response_original or "(空テキスト)")
        self.sbv2_test_send_edit.setPlainText(response_send or "(空テキスト)")
        self.sbv2_test_display_edit.setPlainText(response_display or "(空テキスト)")
        self._append_log(f"[sbv2-test] original: {response_original[:80]}")
        self._append_log(f"[sbv2-test] send: {response_send[:80]}")
        self._append_log(f"[sbv2-test] display: {response_display[:80]}")
        merged_wav_raw = str(data.get("merged_wav", "") or "").strip()
        merged_wav = Path(merged_wav_raw).resolve() if merged_wav_raw else None
        line_wav_values = data.get("line_wavs", [])
        if not isinstance(line_wav_values, list):
            line_wav_values = []
        line_wavs = [
            Path(str(path)).resolve()
            for path in line_wav_values
            if str(path).strip()
        ]
        audio_probe = merged_wav if merged_wav is not None and merged_wav.exists() else next((path for path in line_wavs if path.exists()), None)
        if audio_probe is None:
            self.sbv2_test_status_label.setText("音声ファイルなし")
            self._append_log("[sbv2-test] audio wav not found")
            return

        current_run_dir = audio_probe.parent.parent if audio_probe.parent.name == "parts" else audio_probe.parent
        previous_run_dir = self._sbv2_test_last_run_dir
        self._sbv2_test_last_wav = audio_probe
        self._sbv2_test_last_run_dir = current_run_dir
        if previous_run_dir is not None and previous_run_dir != current_run_dir:
            self._cleanup_sbv2_test_run_dir(previous_run_dir)
        if no_send_event:
            if merged_wav is not None and merged_wav.exists() and self._play_wav_in_gui(merged_wav):
                self.sbv2_test_status_label.setText("送信前テスト完了: GUI再生中")
            else:
                self.sbv2_test_status_label.setText("送信前テスト完了")
            self._append_log(f"[sbv2-test] no-send-event mode: generated {audio_probe.name}")
            return

        try:
            cfg = self._active_runtime_cfg if self._active_runtime_cfg is not None else self._build_config()
        except Exception as exc:
            self.sbv2_test_status_label.setText("設定エラー")
            self._append_log(f"[sbv2-test] config error: {exc}")
            return
        if not bool(data.get("event_sent", True)):
            self._append_log("[sbv2-test] event_sent=false")
            if merged_wav is not None and merged_wav.exists() and self._play_wav_in_gui(merged_wav):
                self.sbv2_test_status_label.setText("KKS送信失敗: GUI再生中")
                self._append_log(f"[sbv2-test] local play: {merged_wav.name}")
            else:
                self.sbv2_test_status_label.setText("KKS送信失敗")
            return

        try:
            female_hold = float(data.get("total_wav_duration", 0.0) or 0.0)
        except (TypeError, ValueError):
            female_hold = 0.0
        if female_hold <= 0.0 and merged_wav is not None:
            female_hold = _wav_duration_sec(str(merged_wav))
        sequence_sent = bool(data.get("sequence_sent", False))
        sequence_session_id = str(data.get("sequence_session_id", "") or "").strip()
        if response_display:
            self._append_log(f"[sbv2-test] main-send text='{response_display[:80]}' hold={female_hold:.2f}s")
            worker = self._pipeline_worker
            if worker is not None and not sequence_sent:
                worker._send_subtitle(response_display, audio_probe.name, "StackFemale", hold_seconds=female_hold)
            elif sequence_sent:
                self._append_log("[sbv2-test] line subtitles handled by VoiceFaceEventBridge")
            else:
                self._append_log("[sbv2-test] pipeline worker missing: subtitle skipped")
            delay = female_hold if female_hold else 0.0
            if worker is not None:
                worker._schedule_response_text(response_display, cfg.main_index, delay, session_id=sequence_session_id)
            else:
                self._append_log("[sbv2-test] pipeline worker missing: response_text skipped")
        self.sbv2_test_status_label.setText("KKS送信完了")
        self._append_log("[sbv2-test] audio sent + main-send subtitle/response_text")

    def _log_sbv2_health_snapshot(self, reason: str) -> None:
        try:
            cfg = self._active_runtime_cfg if self._active_runtime_cfg is not None else self._build_config()
        except Exception as exc:
            self._append_log(f"[sbv2-diag] {reason} config error: {exc}")
            return

        base_url = (cfg.sbv2_server_url or "").strip()
        if not self._sbv2_use_http_mode(cfg):
            self._append_log(f"[sbv2-diag] {reason} sbv2_mode=local: health check skipped")
        elif not base_url:
            self._append_log(f"[sbv2-diag] {reason} sbv2_server_url is empty")
        else:
            health_url = base_url.rstrip("/") + "/models/info"
            try:
                with urllib.request.urlopen(health_url, timeout=2.0) as resp:
                    status = getattr(resp, "status", 200)
                    body = resp.read(256)
                self._append_log(f"[sbv2-diag] {reason} health_ok status={status} bytes={len(body)}")
            except Exception as exc:
                self._append_log(f"[sbv2-diag] {reason} health_ng: {exc}")

        worker = self._pipeline_worker
        if worker is not None and hasattr(worker, "_emit_sbv2_diagnostics"):
            try:
                worker._emit_sbv2_diagnostics(f"gui_{reason}")
            except Exception as exc:
                self._append_log(f"[sbv2-diag] {reason} worker-diag failed: {exc}")

    def _on_sbv2_test_error(self, err: str) -> None:
        self._sbv2_test_worker = None
        self._sbv2_test_no_send = False
        self.sbv2_test_status_label.setText("失敗")
        self.sbv2_test_original_edit.setPlainText("")
        self.sbv2_test_send_edit.setPlainText("")
        self.sbv2_test_display_edit.setPlainText("")
        self._append_log(f"[sbv2-test] error: {err}")
        lower = (err or "").lower()
        if ("10054" in err) or ("10061" in err) or ("connection reset" in lower) or ("connection refused" in lower):
            self._log_sbv2_health_snapshot("sbv2_test_error")

    def _build_conversion_tab(self) -> None:
        tab = QWidget()
        scroll = QScrollArea(); scroll.setWidget(tab); scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tabs.addTab(scroll, "変換辞書")
        layout = QVBoxLayout(tab)
        layout.addWidget(QLabel("TTS前に適用するテキスト変換（上から順に適用）:"))

        tool_row = QHBoxLayout()
        tool_row.addWidget(QLabel("検索"))
        self.conversion_search_edit = QLineEdit()
        self.conversion_search_edit.setPlaceholderText("キーワード（空白区切りでAND検索）")
        self.conversion_search_edit.textChanged.connect(self._apply_conversion_table_search)
        tool_row.addWidget(self.conversion_search_edit, 1)
        tool_row.addWidget(QLabel("並び替え"))
        self.conversion_sort_combo = _NoWheelAlwaysComboBox()
        self.conversion_sort_combo.addItem("登録順", 5)
        self.conversion_sort_combo.addItem("名前順", 1)
        self.conversion_sort_combo.addItem("有効", 0)
        self.conversion_sort_combo.addItem("SBV2送信用", 2)
        self.conversion_sort_combo.addItem("表示用", 3)
        self.conversion_sort_combo.addItem("表示適用", 4)
        tool_row.addWidget(self.conversion_sort_combo)
        conv_sort_asc = QPushButton("昇順")
        conv_sort_desc = QPushButton("降順")
        conv_sort_asc.clicked.connect(lambda: self._sort_conversion_table(Qt.SortOrder.AscendingOrder))
        conv_sort_desc.clicked.connect(lambda: self._sort_conversion_table(Qt.SortOrder.DescendingOrder))
        tool_row.addWidget(conv_sort_asc)
        tool_row.addWidget(conv_sort_desc)
        layout.addLayout(tool_row)

        preset_row = QHBoxLayout()
        self.conversion_preset_name_edit = QLineEdit()
        self.conversion_preset_name_edit.setPlaceholderText("辞書プリセット名")
        conversion_preset_save_btn = QPushButton("プリセット保存")
        conversion_preset_save_btn.clicked.connect(self._save_dictionary_preset)
        self.conversion_preset_combo = _NoWheelComboBox()
        conversion_preset_apply_btn = QPushButton("適用")
        conversion_preset_apply_btn.clicked.connect(self._apply_dictionary_preset_from_combo)
        conversion_preset_del_btn = QPushButton("削除")
        conversion_preset_del_btn.clicked.connect(self._delete_dictionary_preset)
        preset_row.addWidget(self.conversion_preset_name_edit, 2)
        preset_row.addWidget(conversion_preset_save_btn)
        preset_row.addWidget(self.conversion_preset_combo, 2)
        preset_row.addWidget(conversion_preset_apply_btn)
        preset_row.addWidget(conversion_preset_del_btn)
        layout.addLayout(preset_row)

        self.conversion_table = QTableWidget(0, 6)
        self.conversion_table.setHorizontalHeaderLabels(["有効", "変換前", "SBV2送信用", "表示用", "表示適用", "登録順"])
        conv_header = self.conversion_table.horizontalHeader()
        conv_header.setStretchLastSection(False)
        conv_header.setSectionResizeMode(0, conv_header.ResizeMode.Interactive)
        conv_header.setSectionResizeMode(1, conv_header.ResizeMode.Interactive)
        conv_header.setSectionResizeMode(2, conv_header.ResizeMode.Interactive)
        conv_header.setSectionResizeMode(3, conv_header.ResizeMode.Interactive)
        conv_header.setSectionResizeMode(4, conv_header.ResizeMode.Interactive)
        conv_header.setSectionResizeMode(5, conv_header.ResizeMode.Fixed)
        self.conversion_table.setColumnWidth(0, 62)
        self.conversion_table.setColumnWidth(4, 110)
        self.conversion_table.setColumnHidden(5, True)
        self.conversion_table.setSortingEnabled(True)
        self._conversion_table_delegate = _ConversionTableDelegate(self.conversion_table, last_editable_col=4)
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

    def _sort_conversion_table(self, order: Qt.SortOrder) -> None:
        col = int(self.conversion_sort_combo.currentData())
        self.conversion_table.sortItems(col, order)
        self._apply_conversion_table_search()

    def _apply_conversion_table_search(self) -> None:
        query = self.conversion_search_edit.text()
        tokens = [t for t in query.lower().split() if t]
        table = self.conversion_table
        for row in range(table.rowCount()):
            parts = [
                "有効" if table.item(row, 0) and table.item(row, 0).checkState() == Qt.CheckState.Checked else "無効",
                (table.item(row, 1).text() if table.item(row, 1) else ""),
                (table.item(row, 2).text() if table.item(row, 2) else ""),
                (table.item(row, 3).text() if table.item(row, 3) else ""),
                "表示適用" if table.item(row, 4) and table.item(row, 4).checkState() == Qt.CheckState.Checked else "表示無効",
            ]
            haystack = " ".join(parts).lower()
            visible = all(token in haystack for token in tokens)
            table.setRowHidden(row, not visible)

    def _focus_table_item_later(
        self,
        table: QTableWidget,
        *,
        focus_col: int,
        order_col: int,
        order_value: str,
        edit: bool = False,
    ) -> None:
        def _focus() -> None:
            try:
                target_item: Optional[QTableWidgetItem] = None
                for row in range(table.rowCount()):
                    order_item = table.item(row, order_col)
                    if order_item is None:
                        continue
                    if order_item.text() != order_value:
                        continue
                    target_item = table.item(row, focus_col)
                    break
                if target_item is None:
                    return
                table.setCurrentItem(target_item)
                table.scrollToItem(target_item, QTableWidget.ScrollHint.PositionAtCenter)
                if edit and (target_item.flags() & Qt.ItemFlag.ItemIsEditable):
                    table.editItem(target_item)
            except RuntimeError:
                # ウィンドウ破棄タイミングなどでC++側が消えた場合は無視する
                return

        QTimer.singleShot(0, _focus)

    def _conv_add_row(
        self,
        from_text: str = "",
        to_sbv2: str = "",
        to_display: str = "",
        display_apply: bool = True,
        enabled: bool = True,
        order_index: Optional[int] = None,
        *,
        start_edit: bool = True,
    ) -> None:
        table = self.conversion_table
        sorting_enabled = table.isSortingEnabled()
        if sorting_enabled:
            table.setSortingEnabled(False)
        table.blockSignals(True)
        row = table.rowCount()
        table.insertRow(row)
        order_value = int(order_index) if order_index is not None else self._conversion_order_seq
        self._conversion_order_seq = max(self._conversion_order_seq, order_value + 1)
        enabled_item = self._new_enabled_item(enabled)
        from_item = QTableWidgetItem(from_text)
        to_sbv2_item = QTableWidgetItem(to_sbv2)
        to_display_item = QTableWidgetItem(to_display)
        display_item = self._new_display_apply_item(display_apply)
        order_item = QTableWidgetItem(f"{order_value:08d}")
        order_item.setFlags(order_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        table.setItem(row, 0, enabled_item)
        table.setItem(row, 1, from_item)
        table.setItem(row, 2, to_sbv2_item)
        table.setItem(row, 3, to_display_item)
        table.setItem(row, 4, display_item)
        table.setItem(row, 5, order_item)
        table.blockSignals(False)
        if sorting_enabled:
            table.setSortingEnabled(True)
        if start_edit:
            idx = self.conversion_sort_combo.findData(0)
            if idx >= 0:
                self.conversion_sort_combo.setCurrentIndex(idx)
            self._sort_conversion_table(Qt.SortOrder.AscendingOrder)
        needs_search_clear = bool(start_edit and self.conversion_search_edit.text().strip())
        if needs_search_clear:
            self.conversion_search_edit.clear()
        else:
            self._apply_conversion_table_search()
        self._focus_table_item_later(
            table,
            focus_col=1,
            order_col=5,
            order_value=order_item.text(),
            edit=bool(start_edit and not from_text),
        )
        self._on_any_setting_changed()

    def _conv_del_row(self) -> None:
        table = self.conversion_table
        rows = sorted({idx.row() for idx in table.selectedIndexes()}, reverse=True)
        for row in rows:
            table.removeRow(row)
        self._apply_conversion_table_search()
        self._on_any_setting_changed()

    @staticmethod
    def _new_enabled_item(checked: bool) -> QTableWidgetItem:
        item = _CheckStateSortItem("")
        item.setFlags((item.flags() | Qt.ItemFlag.ItemIsUserCheckable) & ~Qt.ItemFlag.ItemIsEditable)
        item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        return item

    @staticmethod
    def _new_display_apply_item(checked: bool) -> QTableWidgetItem:
        item = _CheckStateSortItem("")
        item.setFlags((item.flags() | Qt.ItemFlag.ItemIsUserCheckable) & ~Qt.ItemFlag.ItemIsEditable)
        item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        return item

    def _build_transcribe_conversion_tab(self) -> None:
        tab = QWidget()
        scroll = QScrollArea(); scroll.setWidget(tab); scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tabs.addTab(scroll, "文字起こし変換")
        layout = QVBoxLayout(tab)
        layout.addWidget(QLabel("FasterWhisper結果に適用するテキスト変換（Grok送信用/表示用を分離）:"))

        tool_row = QHBoxLayout()
        tool_row.addWidget(QLabel("検索"))
        self.transcribe_conversion_search_edit = QLineEdit()
        self.transcribe_conversion_search_edit.setPlaceholderText("キーワード（空白区切りでAND検索）")
        self.transcribe_conversion_search_edit.textChanged.connect(self._apply_transcribe_conversion_table_search)
        tool_row.addWidget(self.transcribe_conversion_search_edit, 1)
        tool_row.addWidget(QLabel("並び替え"))
        self.transcribe_conversion_sort_combo = _NoWheelAlwaysComboBox()
        self.transcribe_conversion_sort_combo.addItem("登録順", 5)
        self.transcribe_conversion_sort_combo.addItem("名前順", 1)
        self.transcribe_conversion_sort_combo.addItem("有効", 0)
        self.transcribe_conversion_sort_combo.addItem("Grok送信用", 2)
        self.transcribe_conversion_sort_combo.addItem("表示用", 3)
        self.transcribe_conversion_sort_combo.addItem("表示適用", 4)
        tool_row.addWidget(self.transcribe_conversion_sort_combo)
        trans_sort_asc = QPushButton("昇順")
        trans_sort_desc = QPushButton("降順")
        trans_sort_asc.clicked.connect(lambda: self._sort_transcribe_conversion_table(Qt.SortOrder.AscendingOrder))
        trans_sort_desc.clicked.connect(lambda: self._sort_transcribe_conversion_table(Qt.SortOrder.DescendingOrder))
        tool_row.addWidget(trans_sort_asc)
        tool_row.addWidget(trans_sort_desc)
        layout.addLayout(tool_row)

        preset_row = QHBoxLayout()
        self.transcribe_preset_name_edit = QLineEdit()
        self.transcribe_preset_name_edit.setPlaceholderText("文字起こしプリセット名")
        transcribe_preset_save_btn = QPushButton("プリセット保存")
        transcribe_preset_save_btn.clicked.connect(self._save_transcribe_conversion_preset)
        self.transcribe_preset_combo = _NoWheelComboBox()
        transcribe_preset_apply_btn = QPushButton("適用")
        transcribe_preset_apply_btn.clicked.connect(self._apply_transcribe_conversion_preset_from_combo)
        transcribe_preset_del_btn = QPushButton("削除")
        transcribe_preset_del_btn.clicked.connect(self._delete_transcribe_conversion_preset)
        preset_row.addWidget(self.transcribe_preset_name_edit, 2)
        preset_row.addWidget(transcribe_preset_save_btn)
        preset_row.addWidget(self.transcribe_preset_combo, 2)
        preset_row.addWidget(transcribe_preset_apply_btn)
        preset_row.addWidget(transcribe_preset_del_btn)
        layout.addLayout(preset_row)

        self.transcribe_conversion_table = QTableWidget(0, 6)
        self.transcribe_conversion_table.setHorizontalHeaderLabels(["有効", "変換前", "Grok送信用", "表示用", "表示適用", "登録順"])
        trans_header = self.transcribe_conversion_table.horizontalHeader()
        trans_header.setStretchLastSection(False)
        trans_header.setSectionResizeMode(0, trans_header.ResizeMode.Interactive)
        trans_header.setSectionResizeMode(1, trans_header.ResizeMode.Interactive)
        trans_header.setSectionResizeMode(2, trans_header.ResizeMode.Interactive)
        trans_header.setSectionResizeMode(3, trans_header.ResizeMode.Interactive)
        trans_header.setSectionResizeMode(4, trans_header.ResizeMode.Interactive)
        trans_header.setSectionResizeMode(5, trans_header.ResizeMode.Fixed)
        self.transcribe_conversion_table.setColumnWidth(0, 62)
        self.transcribe_conversion_table.setColumnWidth(4, 110)
        self.transcribe_conversion_table.setColumnHidden(5, True)
        self.transcribe_conversion_table.setSortingEnabled(True)
        self._transcribe_conversion_table_delegate = _ConversionTableDelegate(self.transcribe_conversion_table, last_editable_col=4)
        self._transcribe_conversion_table_delegate.request_new_row.connect(self._transcribe_conv_add_row)
        self.transcribe_conversion_table.setItemDelegate(self._transcribe_conversion_table_delegate)
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

    def _sort_transcribe_conversion_table(self, order: Qt.SortOrder) -> None:
        col = int(self.transcribe_conversion_sort_combo.currentData())
        self.transcribe_conversion_table.sortItems(col, order)
        self._apply_transcribe_conversion_table_search()

    def _apply_transcribe_conversion_table_search(self) -> None:
        query = self.transcribe_conversion_search_edit.text()
        tokens = [t for t in query.lower().split() if t]
        table = self.transcribe_conversion_table
        for row in range(table.rowCount()):
            parts = [
                "有効" if table.item(row, 0) and table.item(row, 0).checkState() == Qt.CheckState.Checked else "無効",
                (table.item(row, 1).text() if table.item(row, 1) else ""),
                (table.item(row, 2).text() if table.item(row, 2) else ""),
                (table.item(row, 3).text() if table.item(row, 3) else ""),
                "表示適用" if table.item(row, 4) and table.item(row, 4).checkState() == Qt.CheckState.Checked else "表示無効",
            ]
            haystack = " ".join(parts).lower()
            visible = all(token in haystack for token in tokens)
            table.setRowHidden(row, not visible)

    def _transcribe_conv_add_row(
        self,
        from_text: str = "",
        to_grok: str = "",
        to_display: str = "",
        display_apply: bool = True,
        enabled: bool = True,
        order_index: Optional[int] = None,
        *,
        start_edit: bool = True,
    ) -> None:
        table = self.transcribe_conversion_table
        sorting_enabled = table.isSortingEnabled()
        if sorting_enabled:
            table.setSortingEnabled(False)
        table.blockSignals(True)
        row = table.rowCount()
        table.insertRow(row)
        order_value = int(order_index) if order_index is not None else self._transcribe_conversion_order_seq
        self._transcribe_conversion_order_seq = max(self._transcribe_conversion_order_seq, order_value + 1)
        enabled_item = self._new_enabled_item(enabled)
        from_item = QTableWidgetItem(from_text)
        to_grok_item = QTableWidgetItem(to_grok)
        to_display_item = QTableWidgetItem(to_display)
        display_item = self._new_display_apply_item(display_apply)
        order_item = QTableWidgetItem(f"{order_value:08d}")
        order_item.setFlags(order_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        table.setItem(row, 0, enabled_item)
        table.setItem(row, 1, from_item)
        table.setItem(row, 2, to_grok_item)
        table.setItem(row, 3, to_display_item)
        table.setItem(row, 4, display_item)
        table.setItem(row, 5, order_item)
        table.blockSignals(False)
        if sorting_enabled:
            table.setSortingEnabled(True)
        if start_edit:
            idx = self.transcribe_conversion_sort_combo.findData(0)
            if idx >= 0:
                self.transcribe_conversion_sort_combo.setCurrentIndex(idx)
            self._sort_transcribe_conversion_table(Qt.SortOrder.AscendingOrder)
        needs_search_clear = bool(start_edit and self.transcribe_conversion_search_edit.text().strip())
        if needs_search_clear:
            self.transcribe_conversion_search_edit.clear()
        else:
            self._apply_transcribe_conversion_table_search()
        self._focus_table_item_later(
            table,
            focus_col=1,
            order_col=5,
            order_value=order_item.text(),
            edit=bool(start_edit and not from_text),
        )
        self._on_live_setting_changed()

    def _transcribe_conv_del_row(self) -> None:
        table = self.transcribe_conversion_table
        rows = sorted({idx.row() for idx in table.selectedIndexes()}, reverse=True)
        for row in rows:
            table.removeRow(row)
        self._apply_transcribe_conversion_table_search()
        self._on_live_setting_changed()

    def _on_transcribe_conv_item_changed(self, _item: QTableWidgetItem) -> None:
        if self._loading_config:
            return
        self._apply_transcribe_conversion_table_search()
        self._on_live_setting_changed()

    def _build_selenium_tab(self) -> None:
        tab = QWidget()
        scroll = QScrollArea(); scroll.setWidget(tab); scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tabs.addTab(scroll, "Selenium")
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

        # ステータスログ（コピー可能）
        self.chrome_log_text = QPlainTextEdit()
        self.chrome_log_text.setReadOnly(True)
        self.chrome_log_text.setMaximumBlockCount(200)
        layout.addWidget(self.chrome_log_text, 1)
        # 後方互換: chrome_status_label への書き込みをログ欄に転送
        self.chrome_status_label = type("_LabelProxy", (), {
            "setText": lambda self_proxy, text: self._chrome_log(text),
            "text": lambda self_proxy: "",
        })()

        # 初期化
        self._chrome_driver = None
        self._refresh_chrome_profiles()

    def _chrome_log(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.chrome_log_text.appendPlainText(f"[{stamp}] {text}")

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
        scroll = QScrollArea(); scroll.setWidget(tab); scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tabs.addTab(scroll, "フィルター")
        layout = QVBoxLayout(tab)

        tool_row = QHBoxLayout()
        tool_row.addWidget(QLabel("検索"))
        self.filter_search_edit = QLineEdit()
        self.filter_search_edit.setPlaceholderText("キーワード（空白区切りでAND検索）")
        self.filter_search_edit.textChanged.connect(self._apply_filter_table_search)
        tool_row.addWidget(self.filter_search_edit, 1)
        tool_row.addWidget(QLabel("並び替え"))
        self.filter_sort_combo = _NoWheelAlwaysComboBox()
        self.filter_sort_combo.addItem("登録順", 3)
        self.filter_sort_combo.addItem("名前順", 1)
        self.filter_sort_combo.addItem("有効", 0)
        self.filter_sort_combo.addItem("種別", 2)
        tool_row.addWidget(self.filter_sort_combo)
        filter_sort_asc = QPushButton("昇順")
        filter_sort_desc = QPushButton("降順")
        filter_sort_asc.clicked.connect(lambda: self._sort_filter_table(Qt.SortOrder.AscendingOrder))
        filter_sort_desc.clicked.connect(lambda: self._sort_filter_table(Qt.SortOrder.DescendingOrder))
        tool_row.addWidget(filter_sort_asc)
        tool_row.addWidget(filter_sort_desc)
        layout.addLayout(tool_row)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("行追加")
        add_btn.clicked.connect(self._filter_add_row)
        del_btn = QPushButton("選択行削除")
        del_btn.clicked.connect(self._filter_del_row)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(del_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self.filter_table = QTableWidget(0, 4)
        self.filter_table.setHorizontalHeaderLabels(["有効", "パターン", "種別", "登録順"])
        filter_header = self.filter_table.horizontalHeader()
        filter_header.setStretchLastSection(False)
        filter_header.setSectionResizeMode(0, filter_header.ResizeMode.Interactive)
        filter_header.setSectionResizeMode(1, filter_header.ResizeMode.Interactive)
        filter_header.setSectionResizeMode(2, filter_header.ResizeMode.Interactive)
        filter_header.setSectionResizeMode(3, filter_header.ResizeMode.Fixed)
        self.filter_table.setColumnWidth(0, 62)
        self.filter_table.setColumnHidden(3, True)
        self.filter_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.filter_table.setSortingEnabled(True)
        self._filter_table_delegate = _ConversionTableDelegate(self.filter_table, last_editable_col=1)
        self._filter_table_delegate.request_new_row.connect(lambda: self._filter_add_row())
        self.filter_table.setItemDelegate(self._filter_table_delegate)
        self._filter_type_delegate = _FilterTypeDelegate(self.filter_table)
        self.filter_table.setItemDelegateForColumn(2, self._filter_type_delegate)
        self.filter_table.cellClicked.connect(self._on_filter_table_cell_clicked)
        self.filter_table.installEventFilter(self)
        layout.addWidget(self.filter_table, 1)

        defaults = [
            ("ありがとうございました", "partial"),
            ("ご視聴ありがとうございました", "partial"),
            ("チャンネル登録よろしくお願いします", "partial"),
            ("高評価よろしくお願いします", "partial"),
            ("字幕は自動生成されています", "partial"),
            ("お疲れ様でした", "partial"),
            ("視聴ありがとうございました", "partial"),
            ("ご視聴ありがとう", "partial"),
            ("MBC", "partial"),
            ("NHK", "partial"),
        ]
        for pattern, ftype in defaults:
            self._filter_add_row(pattern, ftype, start_edit=False, notify=False)

    def _sort_filter_table(self, order: Qt.SortOrder) -> None:
        col = int(self.filter_sort_combo.currentData())
        self.filter_table.sortItems(col, order)
        self._apply_filter_table_search()

    def _on_filter_table_cell_clicked(self, row: int, col: int) -> None:
        # 種別列は1クリックで直接コンボを開く
        if col != 2:
            return
        item = self.filter_table.item(row, col)
        if item is None:
            return
        self.filter_table.setCurrentItem(item)
        self.filter_table.editItem(item)

    def _apply_filter_table_search(self) -> None:
        query = self.filter_search_edit.text()
        tokens = [t for t in query.lower().split() if t]
        table = self.filter_table
        for row in range(table.rowCount()):
            type_item = table.item(row, 2)
            type_text = type_item.text() if type_item else ""
            parts = [
                "有効" if table.item(row, 0) and table.item(row, 0).checkState() == Qt.CheckState.Checked else "無効",
                (table.item(row, 1).text() if table.item(row, 1) else ""),
                type_text,
            ]
            haystack = " ".join(parts).lower()
            visible = all(token in haystack for token in tokens)
            table.setRowHidden(row, not visible)

    def _filter_add_row(
        self,
        pattern: str = "",
        ftype: str = "partial",
        enabled: bool = True,
        order_index: Optional[int] = None,
        *,
        start_edit: bool = True,
        notify: bool = True,
    ) -> None:
        sorting_enabled = self.filter_table.isSortingEnabled()
        if sorting_enabled:
            self.filter_table.setSortingEnabled(False)
        self.filter_table.blockSignals(True)
        row = self.filter_table.rowCount()
        self.filter_table.insertRow(row)
        order_value = int(order_index) if order_index is not None else self._filter_order_seq
        self._filter_order_seq = max(self._filter_order_seq, order_value + 1)
        enabled_item = self._new_enabled_item(enabled)
        pattern_item = QTableWidgetItem(pattern)
        order_item = QTableWidgetItem(f"{order_value:08d}")
        order_item.setFlags(order_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.filter_table.setItem(row, 0, enabled_item)
        self.filter_table.setItem(row, 1, pattern_item)
        type_map = {"partial": "部分一致", "exact": "完全一致", "regex": "正規表現"}
        type_value = ftype if ftype in type_map else "partial"
        type_item = QTableWidgetItem(type_map.get(type_value, "部分一致"))
        type_item.setData(Qt.ItemDataRole.UserRole, type_value)
        self.filter_table.setItem(row, 2, type_item)
        self.filter_table.setItem(row, 3, order_item)
        self.filter_table.blockSignals(False)
        if sorting_enabled:
            self.filter_table.setSortingEnabled(True)
        needs_search_clear = bool(start_edit and self.filter_search_edit.text().strip())
        if needs_search_clear:
            self.filter_search_edit.clear()
        else:
            self._apply_filter_table_search()
        self._focus_table_item_later(
            self.filter_table,
            focus_col=1,
            order_col=3,
            order_value=order_item.text(),
            edit=bool(start_edit and not pattern),
        )
        if notify:
            self._on_any_setting_changed()

    def _filter_del_row(self) -> None:
        rows = sorted({idx.row() for idx in self.filter_table.selectedIndexes()}, reverse=True)
        for row in rows:
            self.filter_table.removeRow(row)
        self._apply_filter_table_search()
        self._on_any_setting_changed()

    # ---- ヘルパー ----

    def _hrow(self, edit: QLineEdit, btn: QPushButton) -> QWidget:
        row = QHBoxLayout()
        row.addWidget(edit, 1)
        row.addWidget(btn)
        w = QWidget()
        w.setLayout(row)
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
        for table in (self.conversion_table, self.transcribe_conversion_table, self.filter_table):
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
        if table is self.filter_table:
            self._apply_filter_table_search()
            self._on_any_setting_changed()
        elif table is self.conversion_table:
            self._apply_conversion_table_search()
            self._on_any_setting_changed()
        elif table is self.transcribe_conversion_table:
            self._apply_transcribe_conversion_table_search()
            self._on_live_setting_changed()
        else:
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
                    cell_widget = table.cellWidget(row, col)
                    if isinstance(cell_widget, QComboBox):
                        idx = cell_widget.findText(raw)
                        if idx < 0:
                            idx = cell_widget.findData(raw)
                        if idx >= 0:
                            cell_widget.setCurrentIndex(idx)
                        continue
                    if item.flags() & Qt.ItemFlag.ItemIsEditable:
                        item.setText(raw)
        finally:
            table.blockSignals(False)
        if table is self.filter_table:
            self._apply_filter_table_search()
            self._on_any_setting_changed()
        elif table is self.conversion_table:
            self._apply_conversion_table_search()
            self._on_any_setting_changed()
        elif table is self.transcribe_conversion_table:
            self._apply_transcribe_conversion_table_search()
            self._on_live_setting_changed()
        else:
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
                last_col = 4
                if row >= 0 and col == last_col:
                    if row == conversion_table.rowCount() - 1:
                        self._conv_add_row()
                    else:
                        next_item = conversion_table.item(row + 1, 1)
                        if next_item is not None:
                            conversion_table.setCurrentCell(row + 1, 1)
                            conversion_table.editItem(next_item)
                    return True
        if transcribe_table is not None and obj is transcribe_table and event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Tab and not (
                event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier)
            ):
                row = transcribe_table.currentRow()
                col = transcribe_table.currentColumn()
                last_col = 4
                if row >= 0 and col == last_col:
                    if row == transcribe_table.rowCount() - 1:
                        self._transcribe_conv_add_row()
                    else:
                        next_item = transcribe_table.item(row + 1, 1)
                        if next_item is not None:
                            transcribe_table.setCurrentCell(row + 1, 1)
                            transcribe_table.editItem(next_item)
                    return True
        filter_table = getattr(self, "filter_table", None)
        if filter_table is not None and obj is filter_table and event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Tab and not (
                event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier)
            ):
                row = filter_table.currentRow()
                col = filter_table.currentColumn()
                if row >= 0 and col == 1:
                    if row == filter_table.rowCount() - 1:
                        self._filter_add_row()
                    else:
                        next_item = filter_table.item(row + 1, 1)
                        if next_item is not None:
                            filter_table.setCurrentCell(row + 1, 1)
                            filter_table.editItem(next_item)
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
        self.vr_ptt_enabled_chk.toggled.connect(self._on_any_setting_changed)
        self.vr_ptt_host_edit.textChanged.connect(self._on_any_setting_changed)
        self.vr_ptt_port_spin.valueChanged.connect(self._on_any_setting_changed)
        self.vr_ptt_timeout_spin.valueChanged.connect(self._on_any_setting_changed)
        self.vr_ptt_token_edit.textChanged.connect(self._on_any_setting_changed)

        # パイプライン設定
        self.kks_root_edit.textChanged.connect(self._on_any_setting_changed)
        self.kks_root_edit.editingFinished.connect(self._reload_face_preset_names)
        self.output_dir_edit.textChanged.connect(self._on_any_setting_changed)
        self.save_faster_text_chk.toggled.connect(self._on_any_setting_changed)
        self.save_source_wav_chk.toggled.connect(self._on_any_setting_changed)
        self.save_sbv2_text_chk.toggled.connect(self._on_any_setting_changed)
        self.save_sbv2_wav_chk.toggled.connect(self._on_any_setting_changed)
        self.max_response_chars_enabled_chk.toggled.connect(self._on_any_setting_changed)
        self.max_response_chars_spin.valueChanged.connect(self._on_any_setting_changed)
        self.diagnostic_log_enabled_chk.toggled.connect(self._on_any_setting_changed)
        self.faster_python_edit.textChanged.connect(self._on_any_setting_changed)
        self.faster_model_edit.currentTextChanged.connect(self._on_any_setting_changed)
        self.faster_device_combo.currentTextChanged.connect(self._on_any_setting_changed)
        self.faster_compute_combo.currentTextChanged.connect(self._on_any_setting_changed)
        self.faster_lang_edit.textChanged.connect(self._on_any_setting_changed)
        self.faster_beam_spin.valueChanged.connect(self._on_any_setting_changed)
        self.pipeline_python_edit.textChanged.connect(self._on_any_setting_changed)
        self.llm_backend_combo.currentTextChanged.connect(self._on_any_setting_changed)
        self.llm_base_url_edit.textChanged.connect(self._on_any_setting_changed)
        self.llm_model_edit.textChanged.connect(self._on_any_setting_changed)
        self.llm_api_key_edit.textChanged.connect(self._on_any_setting_changed)
        self.llm_system_prompt_edit.textChanged.connect(self._on_any_setting_changed)
        self.llm_temperature_spin.valueChanged.connect(self._on_any_setting_changed)
        self.llm_max_tokens_spin.valueChanged.connect(self._on_any_setting_changed)
        self.llm_timeout_spin.valueChanged.connect(self._on_any_setting_changed)
        self.sbv2_root_edit.textChanged.connect(self._on_any_setting_changed)
        self.video_metadata_edit.textChanged.connect(self._on_any_setting_changed)
        self.sbv2_mode_combo.currentTextChanged.connect(self._on_any_setting_changed)
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
        self.face_mode_combo.currentTextChanged.connect(self._on_any_setting_changed)
        self.face_preset_name_combo.currentIndexChanged.connect(self._on_any_setting_changed)
        self.face_preset_random_chk.toggled.connect(self._on_any_setting_changed)
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
        self.filter_table.itemChanged.connect(self._on_any_setting_changed)
        self.conversion_table.itemChanged.connect(self._on_any_setting_changed)
        self.chrome_profile_combo.currentIndexChanged.connect(self._on_any_setting_changed)
        self.chrome_port_spin.valueChanged.connect(self._on_any_setting_changed)
        self.chrome_headless_chk.toggled.connect(self._on_any_setting_changed)

    @staticmethod
    def _is_recorder_needed(cfg: AppConfig) -> bool:
        mode = (cfg.source_mode or DEFAULT_SOURCE_MODE).strip().lower()
        return mode in ("mic", "both")

    @staticmethod
    def _llm_requires_chrome(cfg: AppConfig) -> bool:
        return str(cfg.llm_backend or "grok_browser").strip().lower() == "grok_browser"

    # ---- 設定 ----

    def _build_config(self) -> AppConfig:
        return _build_config_impl(self, config_file=CONFIG_FILE, default_source_mode=DEFAULT_SOURCE_MODE)

    def _save_config(self, cfg: Optional[AppConfig] = None) -> Optional[AppConfig]:
        return _save_config_impl(
            self,
            config_file=CONFIG_FILE,
            default_source_mode=DEFAULT_SOURCE_MODE,
            cfg=cfg,
        )

    def _load_config(self) -> None:
        _load_config_impl(self, config_file=CONFIG_FILE, default_source_mode=DEFAULT_SOURCE_MODE)

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

    # ---- 文字変換プリセット ----

    @staticmethod
    def _table_order_index(item: Optional[QTableWidgetItem], fallback: int) -> int:
        if item is None:
            return max(0, int(fallback))
        try:
            return max(0, int((item.text() or "").strip()))
        except Exception:
            return max(0, int(fallback))

    def _load_conversion_presets(self) -> None:
        try:
            self._transcribe_conversion_presets = _load_presets_impl(
                TRANSCRIPTION_PRESETS_FILE,
                kind="transcription",
            )
            self._dictionary_presets = _load_presets_impl(
                DICTIONARY_PRESETS_FILE,
                kind="dictionary",
            )
            active = _load_active_presets_impl(ACTIVE_PRESETS_FILE)
            self._active_transcribe_preset_id = str(active.get("transcriptionPresetId", "")).strip()
            self._active_dictionary_preset_id = str(active.get("dictionaryPresetId", "")).strip()
        except Exception as exc:
            self._transcribe_conversion_presets = []
            self._dictionary_presets = []
            self._active_transcribe_preset_id = ""
            self._active_dictionary_preset_id = ""
            self._append_log(f"[preset] 読み込み失敗: {exc}")
        self._refresh_transcribe_conversion_preset_ui()
        self._refresh_dictionary_preset_ui()

    def _save_conversion_presets(self) -> None:
        try:
            _save_presets_impl(TRANSCRIPTION_PRESETS_FILE, self._transcribe_conversion_presets)
            _save_presets_impl(DICTIONARY_PRESETS_FILE, self._dictionary_presets)
            _save_active_presets_impl(
                ACTIVE_PRESETS_FILE,
                transcription_preset_id=self._active_transcribe_preset_id,
                dictionary_preset_id=self._active_dictionary_preset_id,
            )
        except Exception as exc:
            self._append_log(f"[preset] 保存失敗: {exc}")

    def _refresh_transcribe_conversion_preset_ui(self) -> None:
        combo = getattr(self, "transcribe_preset_combo", None)
        if combo is None:
            return
        combo.blockSignals(True)
        combo.clear()
        selected_index = -1
        for preset in self._transcribe_conversion_presets:
            name = str(preset.get("name", "")).strip()
            preset_id = str(preset.get("id", "")).strip()
            if not name or not preset_id:
                continue
            combo.addItem(name, preset_id)
            if preset_id == self._active_transcribe_preset_id:
                selected_index = combo.count() - 1
        if selected_index >= 0:
            combo.setCurrentIndex(selected_index)
        combo.blockSignals(False)

    def _refresh_dictionary_preset_ui(self) -> None:
        combo = getattr(self, "conversion_preset_combo", None)
        if combo is None:
            return
        combo.blockSignals(True)
        combo.clear()
        selected_index = -1
        for preset in self._dictionary_presets:
            name = str(preset.get("name", "")).strip()
            preset_id = str(preset.get("id", "")).strip()
            if not name or not preset_id:
                continue
            combo.addItem(name, preset_id)
            if preset_id == self._active_dictionary_preset_id:
                selected_index = combo.count() - 1
        if selected_index >= 0:
            combo.setCurrentIndex(selected_index)
        combo.blockSignals(False)

    def _collect_transcribe_conversion_entries(self) -> list[dict]:
        entries: list[dict] = []
        table = self.transcribe_conversion_table
        for row in range(table.rowCount()):
            enabled_item = table.item(row, 0)
            from_item = table.item(row, 1)
            grok_to_item = table.item(row, 2)
            display_to_item = table.item(row, 3)
            display_item = table.item(row, 4)
            order_item = table.item(row, 5)
            from_text = (from_item.text() if from_item else "").strip()
            if not from_text:
                continue
            entries.append(
                {
                    "enabled": bool(enabled_item and enabled_item.checkState() == Qt.CheckState.Checked),
                    "from": from_text,
                    "to_grok": (grok_to_item.text() if grok_to_item else "").strip(),
                    "to_display": (display_to_item.text() if display_to_item else "").strip(),
                    "display_apply": bool(display_item and display_item.checkState() == Qt.CheckState.Checked),
                    "order_index": self._table_order_index(order_item, row),
                }
            )
        return _normalize_transcription_entries(entries)

    def _collect_dictionary_entries(self) -> list[dict]:
        entries: list[dict] = []
        table = self.conversion_table
        for row in range(table.rowCount()):
            enabled_item = table.item(row, 0)
            from_item = table.item(row, 1)
            sbv2_to_item = table.item(row, 2)
            display_to_item = table.item(row, 3)
            display_item = table.item(row, 4)
            order_item = table.item(row, 5)
            from_text = (from_item.text() if from_item else "").strip()
            if not from_text:
                continue
            entries.append(
                {
                    "enabled": bool(enabled_item and enabled_item.checkState() == Qt.CheckState.Checked),
                    "from": from_text,
                    "to_sbv2": (sbv2_to_item.text() if sbv2_to_item else "").strip(),
                    "to_display": (display_to_item.text() if display_to_item else "").strip(),
                    "display_apply": bool(display_item and display_item.checkState() == Qt.CheckState.Checked),
                    "order_index": self._table_order_index(order_item, row),
                }
            )
        return _normalize_dictionary_entries(entries)

    def _replace_transcribe_conversion_entries(self, entries: list[dict], *, notify: bool) -> None:
        normalized_entries = _normalize_transcription_entries(entries)
        was_loading = self._loading_config
        self._loading_config = True
        try:
            self._transcribe_conversion_order_seq = 0
            self.transcribe_conversion_table.setRowCount(0)
            for entry in normalized_entries:
                self._transcribe_conv_add_row(
                    from_text=str(entry.get("from", "")),
                    to_grok=str(entry.get("to_grok", "")),
                    to_display=str(entry.get("to_display", "")),
                    display_apply=bool(entry.get("display_apply", True)),
                    enabled=bool(entry.get("enabled", True)),
                    order_index=int(entry.get("order_index", 0)),
                    start_edit=False,
                )
        finally:
            self._loading_config = was_loading
        self._apply_transcribe_conversion_table_search()
        if notify:
            self._on_any_setting_changed()

    def _replace_dictionary_entries(self, entries: list[dict], *, notify: bool) -> None:
        normalized_entries = _normalize_dictionary_entries(entries)
        was_loading = self._loading_config
        self._loading_config = True
        try:
            self._conversion_order_seq = 0
            self.conversion_table.setRowCount(0)
            for entry in normalized_entries:
                self._conv_add_row(
                    from_text=str(entry.get("from", "")),
                    to_sbv2=str(entry.get("to_sbv2", "")),
                    to_display=str(entry.get("to_display", "")),
                    display_apply=bool(entry.get("display_apply", False)),
                    enabled=bool(entry.get("enabled", True)),
                    order_index=int(entry.get("order_index", 0)),
                    start_edit=False,
                )
        finally:
            self._loading_config = was_loading
        self._apply_conversion_table_search()
        if notify:
            self._on_any_setting_changed()

    def _save_transcribe_conversion_preset(self) -> None:
        name = self.transcribe_preset_name_edit.text().strip()
        if not name:
            return
        entries = self._collect_transcribe_conversion_entries()
        now = _now_iso8601()

        matched_index = -1
        for index, preset in enumerate(self._transcribe_conversion_presets):
            if str(preset.get("name", "")).strip() == name:
                matched_index = index
                break

        if matched_index >= 0:
            preset = dict(self._transcribe_conversion_presets[matched_index])
            preset_id = str(preset.get("id", "")).strip() or _new_preset_id("tr")
            preset["id"] = preset_id
            preset["name"] = name
            preset["createdAt"] = str(preset.get("createdAt", "")).strip() or now
            preset["updatedAt"] = now
            preset["entries"] = entries
            self._transcribe_conversion_presets[matched_index] = preset
        else:
            preset_id = _new_preset_id("tr")
            self._transcribe_conversion_presets.append(
                {
                    "id": preset_id,
                    "name": name,
                    "createdAt": now,
                    "updatedAt": now,
                    "entries": entries,
                }
            )

        self._active_transcribe_preset_id = preset_id
        self._save_conversion_presets()
        self._refresh_transcribe_conversion_preset_ui()
        self._append_log(f"[preset] 文字起こし変換プリセット保存 → {name}")

    def _apply_transcribe_conversion_preset_from_combo(self) -> None:
        self._apply_transcribe_conversion_preset(self.transcribe_preset_combo.currentIndex())

    def _apply_transcribe_conversion_preset(self, idx: int) -> None:
        if idx < 0 or idx >= len(self._transcribe_conversion_presets):
            return
        preset = self._transcribe_conversion_presets[idx]
        preset_id = str(preset.get("id", "")).strip()
        name = str(preset.get("name", f"({idx + 1})"))
        self._replace_transcribe_conversion_entries(preset.get("entries", []), notify=True)
        self._active_transcribe_preset_id = preset_id
        self._save_conversion_presets()
        self._refresh_transcribe_conversion_preset_ui()
        self._append_log(f"[preset] 文字起こし変換適用 → {name}")

    def _delete_transcribe_conversion_preset(self) -> None:
        idx = self.transcribe_preset_combo.currentIndex()
        if idx < 0 or idx >= len(self._transcribe_conversion_presets):
            return
        removed = self._transcribe_conversion_presets.pop(idx)
        removed_id = str(removed.get("id", "")).strip()
        name = str(removed.get("name", f"({idx + 1})"))
        if removed_id and removed_id == self._active_transcribe_preset_id:
            self._active_transcribe_preset_id = ""
        self._save_conversion_presets()
        self._refresh_transcribe_conversion_preset_ui()
        self._append_log(f"[preset] 文字起こし変換プリセット削除 → {name}")

    def _save_dictionary_preset(self) -> None:
        name = self.conversion_preset_name_edit.text().strip()
        if not name:
            return
        entries = self._collect_dictionary_entries()
        now = _now_iso8601()

        matched_index = -1
        for index, preset in enumerate(self._dictionary_presets):
            if str(preset.get("name", "")).strip() == name:
                matched_index = index
                break

        if matched_index >= 0:
            preset = dict(self._dictionary_presets[matched_index])
            preset_id = str(preset.get("id", "")).strip() or _new_preset_id("dc")
            preset["id"] = preset_id
            preset["name"] = name
            preset["createdAt"] = str(preset.get("createdAt", "")).strip() or now
            preset["updatedAt"] = now
            preset["entries"] = entries
            self._dictionary_presets[matched_index] = preset
        else:
            preset_id = _new_preset_id("dc")
            self._dictionary_presets.append(
                {
                    "id": preset_id,
                    "name": name,
                    "createdAt": now,
                    "updatedAt": now,
                    "entries": entries,
                }
            )

        self._active_dictionary_preset_id = preset_id
        self._save_conversion_presets()
        self._refresh_dictionary_preset_ui()
        self._append_log(f"[preset] 変換辞書プリセット保存 → {name}")

    def _apply_dictionary_preset_from_combo(self) -> None:
        self._apply_dictionary_preset(self.conversion_preset_combo.currentIndex())

    def _apply_dictionary_preset(self, idx: int) -> None:
        if idx < 0 or idx >= len(self._dictionary_presets):
            return
        preset = self._dictionary_presets[idx]
        preset_id = str(preset.get("id", "")).strip()
        name = str(preset.get("name", f"({idx + 1})"))
        self._replace_dictionary_entries(preset.get("entries", []), notify=True)
        self._active_dictionary_preset_id = preset_id
        self._save_conversion_presets()
        self._refresh_dictionary_preset_ui()
        self._append_log(f"[preset] 変換辞書適用 → {name}")

    def _delete_dictionary_preset(self) -> None:
        idx = self.conversion_preset_combo.currentIndex()
        if idx < 0 or idx >= len(self._dictionary_presets):
            return
        removed = self._dictionary_presets.pop(idx)
        removed_id = str(removed.get("id", "")).strip()
        name = str(removed.get("name", f"({idx + 1})"))
        if removed_id and removed_id == self._active_dictionary_preset_id:
            self._active_dictionary_preset_id = ""
        self._save_conversion_presets()
        self._refresh_dictionary_preset_ui()
        self._append_log(f"[preset] 変換辞書プリセット削除 → {name}")

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
        self._run_session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._running = True
        self._paused = False
        self._active_runtime_cfg = cfg
        self._last_deferred_live_fields = tuple()
        self.start_btn.setText("■ 停止")
        self.pause_btn.setEnabled(True)
        self._append_log(f"[session] start session_{self._run_session_id}")

        # Grok選択時だけSeleniumを起動する。ローカルLLMではChrome不要。
        if self._llm_requires_chrome(cfg) and self._chrome_driver is None:
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
        # deleteLaterは使わない — _do_stop()でPython参照をNoneにして回収する
        # (deleteLaterだとC++が先に消えてpause()等でクラッシュする)
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
            diagnostic_log_enabled=cfg.diagnostic_log_enabled,
            diagnostic_log_interval_ms=cfg.diagnostic_log_interval_ms,
            tcp_host="", tcp_port=17890, tcp_token="", tcp_timeout_seconds=20.0,
            external_control_enabled=cfg.vr_ptt_enabled,
            external_control_host=cfg.vr_ptt_host,
            external_control_port=cfg.vr_ptt_port,
            external_control_token=cfg.vr_ptt_token,
            external_control_timeout_seconds=cfg.vr_ptt_timeout_seconds,
            external_control_strict_hold=cfg.vr_ptt_enabled,
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
        self._fw_test_guard_active = False
        self._fw_test_guard_prev_paused = False
        self._run_session_id = None
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
        if not self._send_text_to_pipeline(text, "手動"):
            return
        self._reset_manual_input()

    def _send_text_to_pipeline(self, text: str, source_label: str) -> bool:
        text = str(text or "").strip()
        if not text:
            return False
        if not self._pipeline_worker:
            self._append_log("[warn] パイプラインが起動していません")
            return False
        self._pipeline_worker.send_text(text)
        self._append_log(f"[{source_label}] {text}")
        self._push_manual_history(text)
        return True

    def _push_manual_history(self, text: str) -> None:
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
        self._reset_manual_input()

    def _reset_manual_input(self) -> None:
        combo = self.manual_combo
        combo.blockSignals(True)
        try:
            combo.setCurrentIndex(-1)
            line_edit = combo.lineEdit()
            if line_edit is not None:
                line_edit.clear()
        finally:
            combo.blockSignals(False)

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
        try:
            self._cleanup_fw_test_wav()
        except Exception:
            pass
        try:
            self._cleanup_sbv2_test_run_dir(self._sbv2_test_last_run_dir)
            self._sbv2_test_last_run_dir = None
        except Exception:
            pass
        self._stop_all()
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------

