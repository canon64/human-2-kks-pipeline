from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt

from config.models import AppConfig

DEFAULT_PIPE_NAME = "kks_voice_face_events"
LEGACY_DIAGNOSTIC_PIPE_NAMES = {"kks_voice_face_events_diag_0423"}


def _load_config_json(config_file: Path) -> dict:
    if not config_file.exists():
        return {}
    try:
        data = json.loads(config_file.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _read_transcribe_server_port(config_file: Path) -> int:
    data = _load_config_json(config_file)
    try:
        return int(data.get("transcribe_server_port", 18760))
    except Exception:
        return 18760


def _read_diagnostic_log_interval_ms(config_file: Path) -> int:
    data = _load_config_json(config_file)
    try:
        return max(100, int(data.get("diagnostic_log_interval_ms", 1000)))
    except Exception:
        return 1000


def _sync_grok_bridge_debug_port(config_file: Path, port: int) -> None:
    grok_config_file = config_file.parent / "grok_bridge_config.json"
    data = _load_config_json(grok_config_file)
    data["debug_port"] = int(port)
    grok_config_file.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _as_bool(value: object, default: bool) -> bool:
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


def _normalize_face_send_mode(value: object, default: str = "game_preset") -> str:
    token = str(value or "").strip().lower()
    if token == "preset_id":
        return "preset_name"
    if token in ("game_preset", "preset_name"):
        return token
    return default


def _normalize_sbv2_mode(value: object, default: str = "auto") -> str:
    token = str(value or "").strip().lower()
    if token in ("auto", "http", "local"):
        return token
    return default


def normalize_pipe_name(value: object, default: str = DEFAULT_PIPE_NAME) -> str:
    pipe_name = str(value or "").strip()
    prefix = "\\\\.\\pipe\\"
    if pipe_name.lower().startswith(prefix.lower()):
        pipe_name = pipe_name[len(prefix) :].strip()
    if not pipe_name or pipe_name.lower() in LEGACY_DIAGNOSTIC_PIPE_NAMES:
        return default
    return pipe_name


def build_config(window, *, config_file: Path, default_source_mode: str) -> AppConfig:
    diagnostic_log_interval_ms = _read_diagnostic_log_interval_ms(config_file)
    selected_face_preset_name = ""
    selected_face_preset_id = ""
    selected_face_preset_data = window.face_preset_name_combo.currentData()
    if isinstance(selected_face_preset_data, dict):
        selected_face_preset_name = str(selected_face_preset_data.get("name", "")).strip()
        selected_face_preset_id = str(selected_face_preset_data.get("id", "")).strip()
    if (not selected_face_preset_name) and (not isinstance(selected_face_preset_data, dict)):
        selected_face_preset_name = window.face_preset_name_combo.currentText().strip()
    filter_phrases = []
    for row in range(window.filter_table.rowCount()):
        enabled_item = window.filter_table.item(row, 0)
        pattern_item = window.filter_table.item(row, 1)
        type_item = window.filter_table.item(row, 2)
        enabled = bool(enabled_item and enabled_item.checkState() == Qt.CheckState.Checked)
        pattern = (pattern_item.text() if pattern_item else "").strip()
        ftype = "partial"
        if type_item is not None:
            raw_type = type_item.data(Qt.ItemDataRole.UserRole)
            if raw_type in ("partial", "exact", "regex"):
                ftype = str(raw_type)
            else:
                label = (type_item.text() or "").strip()
                label_to_type = {"部分一致": "partial", "完全一致": "exact", "正規表現": "regex"}
                ftype = label_to_type.get(label, "partial")
        if pattern:
            filter_phrases.append({"enabled": enabled, "pattern": pattern, "type": ftype})
    transcribe_conversion_dict = []
    for row in range(window.transcribe_conversion_table.rowCount()):
        enabled_item = window.transcribe_conversion_table.item(row, 0)
        from_item = window.transcribe_conversion_table.item(row, 1)
        grok_to_item = window.transcribe_conversion_table.item(row, 2)
        display_to_item = window.transcribe_conversion_table.item(row, 3)
        display_item = window.transcribe_conversion_table.item(row, 4)
        enabled = bool(enabled_item and enabled_item.checkState() == Qt.CheckState.Checked)
        from_str = (from_item.text() if from_item else "").strip()
        grok_to = (grok_to_item.text() if grok_to_item else "").strip()
        display_to = (display_to_item.text() if display_to_item else "").strip()
        if from_str:
            display_apply = bool(display_item and display_item.checkState() == Qt.CheckState.Checked)
            transcribe_conversion_dict.append(
                {
                    "enabled": enabled,
                    "from": from_str,
                    "to_grok": grok_to,
                    "to_display": display_to,
                    "display_apply": display_apply,
                }
            )
    conversion_dict = []
    for row in range(window.conversion_table.rowCount()):
        enabled_item = window.conversion_table.item(row, 0)
        from_item = window.conversion_table.item(row, 1)
        sbv2_to_item = window.conversion_table.item(row, 2)
        display_to_item = window.conversion_table.item(row, 3)
        display_item = window.conversion_table.item(row, 4)
        enabled = bool(enabled_item and enabled_item.checkState() == Qt.CheckState.Checked)
        from_str = (from_item.text() if from_item else "").strip()
        to_sbv2 = (sbv2_to_item.text() if sbv2_to_item else "").strip()
        to_display = (display_to_item.text() if display_to_item else "").strip()
        if from_str:
            display_apply = bool(display_item and display_item.checkState() == Qt.CheckState.Checked)
            conversion_dict.append(
                {
                    "enabled": enabled,
                    "from": from_str,
                    "to_sbv2": to_sbv2,
                    "to_display": to_display,
                    "display_apply": display_apply,
                }
            )

    return AppConfig(
        wav_dir=Path(window.wav_dir_edit.text().strip()).expanduser().resolve(),
        threshold_dbfs=float(window.threshold_spin.value()),
        silence_seconds=float(window.silence_spin.value()),
        min_duration_seconds=float(window.min_dur_spin.value()),
        pre_roll_seconds=float(window.pre_roll_spin.value()),
        post_roll_seconds=float(window.post_roll_spin.value()),
        device=window.device_combo.currentData(),
        vr_ptt_enabled=bool(window.vr_ptt_enabled_chk.isChecked()),
        vr_ptt_host=window.vr_ptt_host_edit.text().strip() or "127.0.0.1",
        vr_ptt_port=max(1, min(65535, int(window.vr_ptt_port_spin.value()))),
        vr_ptt_token=window.vr_ptt_token_edit.text().strip(),
        vr_ptt_timeout_seconds=max(0.2, float(window.vr_ptt_timeout_spin.value())),
        kks_root=Path(window.kks_root_edit.text().strip()).expanduser().resolve(),
        output_dir=Path(window.output_dir_edit.text().strip()).expanduser().resolve(),
        save_fasterwhisper_text=bool(window.save_faster_text_chk.isChecked()),
        save_source_wav=bool(window.save_source_wav_chk.isChecked()),
        save_sbv2_input_text=bool(window.save_sbv2_text_chk.isChecked()),
        save_sbv2_output_wav=bool(window.save_sbv2_wav_chk.isChecked()),
        faster_python=Path(window.faster_python_edit.text().strip()).expanduser().resolve(),
        faster_model=window.faster_model_edit.currentText().strip() or "large-v3",
        faster_device=window.faster_device_combo.currentText().strip(),
        faster_compute=window.faster_compute_combo.currentText().strip(),
        faster_language=window.faster_lang_edit.text().strip() or "ja",
        faster_beam=max(1, int(window.faster_beam_spin.value())),
        pipeline_python=Path(window.pipeline_python_edit.text().strip()).expanduser().resolve(),
        sbv2_root=Path(window.sbv2_root_edit.text().strip()).expanduser().resolve(),
        sbv2_model_name=window.model_name_combo.currentText().strip(),
        sbv2_model_file=window.model_file_edit.currentText().strip(),
        sbv2_speaker=window.speaker_edit.text().strip() or "0",
        sbv2_style=window.style_edit.text().strip() or "Neutral",
        sbv2_length=float(window.length_spin.value()),
        voice_volume=float(window.voice_volume_spin.value()),
        voice_pitch=float(window.voice_pitch_spin.value()),
        pipe_name=normalize_pipe_name(window.pipe_edit.text()),
        target_host=window.target_host_edit.text().strip(),
        target_port=int(window.target_port_spin.value()),
        target_endpoint=window.target_endpoint_edit.text().strip() or "/voice-face-event",
        target_token=window.target_token_edit.text().strip(),
        remote_http=bool(window.remote_http_chk.isChecked()),
        subtitle_send_enabled=bool(window.subtitle_send_chk.isChecked()),
        subtitle_target_host=window.subtitle_host_edit.text().strip() or "127.0.0.1",
        subtitle_target_port=int(window.subtitle_port_spin.value()),
        subtitle_endpoint=window.subtitle_endpoint_edit.text().strip() or "/subtitle-event",
        subtitle_token=window.subtitle_token_edit.text().strip(),
        subtitle_timeout_sec=float(window.subtitle_timeout_spin.value()),
        main_index=int(window.main_spin.value()),
        face=int(window.face_spin.value()),
        keep_current_face=bool(window.keep_face_chk.isChecked()),
        face_send_mode=_normalize_face_send_mode(
            window.face_mode_combo.currentData() if window.face_mode_combo.currentData() is not None else window.face_mode_combo.currentText(),
            "game_preset",
        ),
        face_preset_id=selected_face_preset_id,
        face_preset_name=selected_face_preset_name,
        face_preset_random=bool(window.face_preset_random_chk.isChecked()),
        source_mode=window.source_mode_combo.currentText().strip().lower() or default_source_mode,
        external_text_enabled=bool(window.external_text_chk.isChecked()),
        external_text_host=window.external_text_host_edit.text().strip() or "127.0.0.1",
        external_text_port=int(window.external_text_port_spin.value()),
        external_text_endpoint=window.external_text_endpoint_edit.text().strip() or "/manual-text",
        external_text_token=window.external_text_token_edit.text().strip(),
        external_text_dedupe_max=int(window.external_text_dedupe_spin.value()),
        max_response_chars_enabled=bool(window.max_response_chars_enabled_chk.isChecked()),
        max_response_chars=max(1, int(window.max_response_chars_spin.value())),
        transcribe_server_port=_read_transcribe_server_port(config_file),
        diagnostic_log_enabled=bool(window.diagnostic_log_enabled_chk.isChecked()),
        diagnostic_log_interval_ms=diagnostic_log_interval_ms,
        sbv2_mode=_normalize_sbv2_mode(
            window.sbv2_mode_combo.currentData()
            if window.sbv2_mode_combo.currentData() is not None
            else window.sbv2_mode_combo.currentText(),
            "auto",
        ),
        sbv2_server_url=window.sbv2_server_url_edit.text().strip(),
        sbv2_server_auto_start=window.sbv2_auto_start_chk.isChecked(),
        video_metadata_path=(
            Path(window.video_metadata_edit.text().strip()).expanduser().resolve()
            if window.video_metadata_edit.text().strip()
            else None
        ),
        filter_phrases=filter_phrases,
        transcribe_conversion_dict=transcribe_conversion_dict,
        conversion_dict=conversion_dict,
    )


def save_config(
    window,
    *,
    config_file: Path,
    default_source_mode: str,
    cfg: Optional[AppConfig] = None,
) -> Optional[AppConfig]:
    if cfg is None:
        try:
            cfg = build_config(window, config_file=config_file, default_source_mode=default_source_mode)
        except Exception:
            return None

    data = {
        "device_name": window.device_combo.currentText(),
        "wav_dir": str(cfg.wav_dir),
        "threshold_dbfs": cfg.threshold_dbfs,
        "silence_seconds": cfg.silence_seconds,
        "min_duration_seconds": cfg.min_duration_seconds,
        "pre_roll_seconds": cfg.pre_roll_seconds,
        "post_roll_seconds": cfg.post_roll_seconds,
        "vr_ptt_enabled": cfg.vr_ptt_enabled,
        "vr_ptt_host": cfg.vr_ptt_host,
        "vr_ptt_port": cfg.vr_ptt_port,
        "vr_ptt_token": cfg.vr_ptt_token,
        "vr_ptt_timeout_seconds": cfg.vr_ptt_timeout_seconds,
        "kks_root": str(cfg.kks_root),
        "output_dir": str(cfg.output_dir),
        "save_fasterwhisper_text": cfg.save_fasterwhisper_text,
        "save_source_wav": cfg.save_source_wav,
        "save_sbv2_input_text": cfg.save_sbv2_input_text,
        "save_sbv2_output_wav": cfg.save_sbv2_output_wav,
        "faster_python": str(cfg.faster_python),
        "faster_model": cfg.faster_model,
        "faster_device": cfg.faster_device,
        "faster_compute": cfg.faster_compute,
        "faster_language": cfg.faster_language,
        "faster_beam": cfg.faster_beam,
        "pipeline_python": str(cfg.pipeline_python),
        "sbv2_root": str(cfg.sbv2_root),
        "sbv2_model_name": cfg.sbv2_model_name,
        "sbv2_model_file": cfg.sbv2_model_file,
        "sbv2_speaker": cfg.sbv2_speaker,
        "sbv2_style": cfg.sbv2_style,
        "sbv2_length": cfg.sbv2_length,
        "voice_volume": cfg.voice_volume,
        "voice_pitch": cfg.voice_pitch,
        "pipe_name": cfg.pipe_name,
        "target_host": cfg.target_host,
        "target_port": cfg.target_port,
        "target_endpoint": cfg.target_endpoint,
        "target_token": cfg.target_token,
        "remote_http": cfg.remote_http,
        "subtitle_send_enabled": cfg.subtitle_send_enabled,
        "subtitle_target_host": cfg.subtitle_target_host,
        "subtitle_target_port": cfg.subtitle_target_port,
        "subtitle_endpoint": cfg.subtitle_endpoint,
        "subtitle_token": cfg.subtitle_token,
        "subtitle_timeout_sec": cfg.subtitle_timeout_sec,
        "main_index": cfg.main_index,
        "face": cfg.face,
        "keep_current_face": cfg.keep_current_face,
        "face_send_mode": cfg.face_send_mode,
        "face_preset_id": cfg.face_preset_id,
        "face_preset_name": cfg.face_preset_name,
        "face_preset_random": cfg.face_preset_random,
        "source_mode": cfg.source_mode,
        "external_text_enabled": cfg.external_text_enabled,
        "external_text_host": cfg.external_text_host,
        "external_text_port": cfg.external_text_port,
        "external_text_endpoint": cfg.external_text_endpoint,
        "external_text_token": cfg.external_text_token,
        "external_text_dedupe_max": cfg.external_text_dedupe_max,
        "max_response_chars_enabled": cfg.max_response_chars_enabled,
        "max_response_chars": cfg.max_response_chars,
        "transcribe_server_port": cfg.transcribe_server_port,
        "diagnostic_log_enabled": cfg.diagnostic_log_enabled,
        "diagnostic_log_interval_ms": cfg.diagnostic_log_interval_ms,
        "sbv2_mode": cfg.sbv2_mode,
        "sbv2_server_url": cfg.sbv2_server_url,
        "sbv2_server_auto_start": cfg.sbv2_server_auto_start,
        "video_metadata_path": str(cfg.video_metadata_path) if cfg.video_metadata_path else "",
        "filter_phrases": cfg.filter_phrases,
        "transcribe_conversion_dict": cfg.transcribe_conversion_dict,
        "conversion_dict": cfg.conversion_dict,
        "manual_history": window._manual_history[:50],
        "model_presets": window._model_presets,
        "chrome_debug_port": window.chrome_port_spin.value(),
        "chrome_headless": window.chrome_headless_chk.isChecked(),
        "chrome_profile": window.chrome_profile_combo.currentData() or "",
    }
    config_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    _sync_grok_bridge_debug_port(config_file, window.chrome_port_spin.value())
    return cfg


def load_config(window, *, config_file: Path, default_source_mode: str) -> None:
    if not config_file.exists():
        return
    try:
        data = json.loads(config_file.read_text(encoding="utf-8"))
    except Exception:
        return

    window._loading_config = True
    try:
        def s(key, widget_val):
            return str(data.get(key, widget_val))

        def f(key, widget_val):
            return float(data.get(key, widget_val))

        def i(key, widget_val):
            return int(data.get(key, widget_val))

        def b(key, widget_val):
            return bool(data.get(key, widget_val))

        device_name = data.get("device_name", "")
        if device_name and device_name != "System default":
            window._select_device_by_name(device_name)
        window.wav_dir_edit.setText(s("wav_dir", window.wav_dir_edit.text()))
        window.threshold_spin.setValue(f("threshold_dbfs", window.threshold_spin.value()))
        window.silence_spin.setValue(f("silence_seconds", window.silence_spin.value()))
        window.min_dur_spin.setValue(f("min_duration_seconds", window.min_dur_spin.value()))
        window.pre_roll_spin.setValue(f("pre_roll_seconds", window.pre_roll_spin.value()))
        window.post_roll_spin.setValue(f("post_roll_seconds", window.post_roll_spin.value()))
        window.vr_ptt_enabled_chk.setChecked(b("vr_ptt_enabled", False))
        window.vr_ptt_host_edit.setText(s("vr_ptt_host", window.vr_ptt_host_edit.text()))
        window.vr_ptt_port_spin.setValue(i("vr_ptt_port", window.vr_ptt_port_spin.value()))
        window.vr_ptt_timeout_spin.setValue(f("vr_ptt_timeout_seconds", window.vr_ptt_timeout_spin.value()))
        window.vr_ptt_token_edit.setText(s("vr_ptt_token", ""))
        window.kks_root_edit.setText(s("kks_root", window.kks_root_edit.text()))
        window.output_dir_edit.setText(s("output_dir", window.output_dir_edit.text()))
        window.save_faster_text_chk.setChecked(b("save_fasterwhisper_text", True))
        window.save_source_wav_chk.setChecked(b("save_source_wav", False))
        window.save_sbv2_text_chk.setChecked(b("save_sbv2_input_text", True))
        window.save_sbv2_wav_chk.setChecked(b("save_sbv2_output_wav", False))
        window.faster_python_edit.setText(s("faster_python", window.faster_python_edit.text()))
        window.faster_model_edit.setCurrentText(s("faster_model", window.faster_model_edit.currentText()))
        window.faster_device_combo.setCurrentText(s("faster_device", window.faster_device_combo.currentText()))
        window.faster_compute_combo.setCurrentText(s("faster_compute", window.faster_compute_combo.currentText()))
        window.faster_lang_edit.setText(s("faster_language", window.faster_lang_edit.text()))
        window.faster_beam_spin.setValue(i("faster_beam", window.faster_beam_spin.value()))
        window.pipeline_python_edit.setText(s("pipeline_python", window.pipeline_python_edit.text()))
        window.sbv2_root_edit.setText(s("sbv2_root", window.sbv2_root_edit.text()))
        window._reload_models()
        window.model_name_combo.setEditText(s("sbv2_model_name", ""))
        window.model_file_edit.setEditText(s("sbv2_model_file", ""))
        window.speaker_edit.setText(s("sbv2_speaker", window.speaker_edit.text()))
        window.style_edit.setText(s("sbv2_style", window.style_edit.text()))
        window.length_spin.setValue(f("sbv2_length", window.length_spin.value()))
        window.voice_volume_spin.setValue(f("voice_volume", window.voice_volume_spin.value()))
        window.voice_pitch_spin.setValue(f("voice_pitch", window.voice_pitch_spin.value()))
        window.pipe_edit.setText(normalize_pipe_name(s("pipe_name", window.pipe_edit.text())))
        window.target_host_edit.setText(s("target_host", window.target_host_edit.text()))
        window.target_port_spin.setValue(i("target_port", window.target_port_spin.value()))
        window.target_endpoint_edit.setText(s("target_endpoint", window.target_endpoint_edit.text()))
        window.target_token_edit.setText(s("target_token", ""))
        window.remote_http_chk.setChecked(b("remote_http", False))
        window.subtitle_send_chk.setChecked(b("subtitle_send_enabled", True))
        window.subtitle_host_edit.setText(s("subtitle_target_host", window.subtitle_host_edit.text()))
        window.subtitle_port_spin.setValue(i("subtitle_target_port", window.subtitle_port_spin.value()))
        window.subtitle_endpoint_edit.setText(s("subtitle_endpoint", window.subtitle_endpoint_edit.text()))
        window.subtitle_token_edit.setText(s("subtitle_token", ""))
        window.subtitle_timeout_spin.setValue(i("subtitle_timeout_sec", window.subtitle_timeout_spin.value()))
        window.main_spin.setValue(i("main_index", window.main_spin.value()))
        window.face_spin.setValue(i("face", window.face_spin.value()))
        window.keep_face_chk.setChecked(b("keep_current_face", True))
        if hasattr(window, "_reload_face_preset_names"):
            window._reload_face_preset_names(keep_selection=False)
        mode = _normalize_face_send_mode(data.get("face_send_mode", "game_preset"), "game_preset")
        mode_index = max(0, window.face_mode_combo.findData(mode))
        window.face_mode_combo.setCurrentIndex(mode_index)
        face_preset_name = str(data.get("face_preset_name", "")).strip()
        face_preset_id = str(data.get("face_preset_id", "")).strip()
        if hasattr(window, "_select_face_preset"):
            window._select_face_preset(face_preset_name, face_preset_id)
        window.face_preset_random_chk.setChecked(b("face_preset_random", False))
        window.source_mode_combo.setCurrentText(s("source_mode", default_source_mode))
        window.external_text_chk.setChecked(b("external_text_enabled", True))
        window.external_text_host_edit.setText(s("external_text_host", window.external_text_host_edit.text()))
        window.external_text_port_spin.setValue(i("external_text_port", window.external_text_port_spin.value()))
        window.external_text_endpoint_edit.setText(s("external_text_endpoint", window.external_text_endpoint_edit.text()))
        window.external_text_token_edit.setText(s("external_text_token", ""))
        window.external_text_dedupe_spin.setValue(i("external_text_dedupe_max", window.external_text_dedupe_spin.value()))
        window.max_response_chars_enabled_chk.setChecked(b("max_response_chars_enabled", True))
        window.max_response_chars_spin.setValue(i("max_response_chars", window.max_response_chars_spin.value()))
        window.max_response_chars_spin.setEnabled(window.max_response_chars_enabled_chk.isChecked())
        window.diagnostic_log_enabled_chk.setChecked(b("diagnostic_log_enabled", False))
        sbv2_mode = _normalize_sbv2_mode(data.get("sbv2_mode", "auto"), "auto")
        sbv2_mode_index = max(0, window.sbv2_mode_combo.findData(sbv2_mode))
        window.sbv2_mode_combo.setCurrentIndex(sbv2_mode_index)
        window.sbv2_server_url_edit.setText(s("sbv2_server_url", window.sbv2_server_url_edit.text()))
        window.sbv2_auto_start_chk.setChecked(b("sbv2_server_auto_start", True))
        window.video_metadata_edit.setText(s("video_metadata_path", window.video_metadata_edit.text()))
        window.chrome_port_spin.setValue(i("chrome_debug_port", 9222))
        window.chrome_headless_chk.setChecked(b("chrome_headless", False))
        saved_profile = s("chrome_profile", "")
        if saved_profile:
            for idx in range(window.chrome_profile_combo.count()):
                if window.chrome_profile_combo.itemData(idx) == saved_profile:
                    window.chrome_profile_combo.setCurrentIndex(idx)
                    break
        phrases = data.get("filter_phrases", [])
        if phrases:
            window._filter_order_seq = 0
            window.filter_table.setRowCount(0)
            for entry in phrases:
                if isinstance(entry, str):
                    entry = {"enabled": True, "pattern": entry, "type": "partial"}
                pattern = entry.get("pattern", "").strip()
                ftype = entry.get("type", "partial")
                enabled = _as_bool(entry.get("enabled", True), True)
                if pattern:
                    window._filter_add_row(pattern, ftype, enabled, start_edit=False, notify=False)
        stt_conv = data.get("transcribe_conversion_dict", [])
        window._transcribe_conversion_order_seq = 0
        window.transcribe_conversion_table.setRowCount(0)
        for entry in stt_conv:
            from_text = str(entry.get("from", "")).strip()
            if not from_text:
                continue
            to_grok = str(entry.get("to_grok", entry.get("to", "")))
            to_display = str(entry.get("to_display", entry.get("to", "")))
            display_apply = _as_bool(entry.get("display_apply", True), True)
            enabled = _as_bool(entry.get("enabled", True), True)
            window._transcribe_conv_add_row(
                from_text,
                to_grok,
                to_display,
                display_apply,
                enabled,
                start_edit=False,
            )
        conv = data.get("conversion_dict", [])
        window._conversion_order_seq = 0
        window.conversion_table.setRowCount(0)
        for entry in conv:
            from_text = str(entry.get("from", "")).strip()
            if not from_text:
                continue
            to_sbv2 = str(entry.get("to_sbv2", entry.get("to_grok", entry.get("to", ""))))
            to_display = str(entry.get("to_display", entry.get("to", "")))
            display_apply = _as_bool(entry.get("display_apply", False), False)
            enabled = _as_bool(entry.get("enabled", True), True)
            window._conv_add_row(
                from_text,
                to_sbv2,
                to_display,
                display_apply,
                enabled,
                start_edit=False,
            )
        window._model_presets = [p for p in data.get("model_presets", []) if isinstance(p, dict) and p.get("name")]
        window._refresh_preset_ui()
        history = data.get("manual_history", [])
        window._manual_history = list(history)[:50]
        window.manual_combo.clear()
        for h in window._manual_history:
            window.manual_combo.addItem(h)
        if hasattr(window, "_reset_manual_input"):
            window._reset_manual_input()
        else:
            window.manual_combo.setCurrentIndex(-1)
            if window.manual_combo.lineEdit() is not None:
                window.manual_combo.lineEdit().clear()
    finally:
        window._loading_config = False
