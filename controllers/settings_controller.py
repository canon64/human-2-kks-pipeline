from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QTableWidgetItem

from config.models import AppConfig


def _read_transcribe_server_port(config_file: Path) -> int:
    if not config_file.exists():
        return 18760
    try:
        return int(json.loads(config_file.read_text(encoding="utf-8")).get("transcribe_server_port", 18760))
    except Exception:
        return 18760


def build_config(window, *, config_file: Path, default_source_mode: str) -> AppConfig:
    filter_phrases = [l for l in window.filter_edit.toPlainText().splitlines() if l.strip()]
    transcribe_conversion_dict = []
    for row in range(window.transcribe_conversion_table.rowCount()):
        from_item = window.transcribe_conversion_table.item(row, 0)
        grok_to_item = window.transcribe_conversion_table.item(row, 1)
        display_to_item = window.transcribe_conversion_table.item(row, 2)
        display_item = window.transcribe_conversion_table.item(row, 3)
        from_str = (from_item.text() if from_item else "").strip()
        grok_to = (grok_to_item.text() if grok_to_item else "").strip()
        display_to = (display_to_item.text() if display_to_item else "").strip()
        if from_str:
            display_apply = bool(display_item and display_item.checkState() == Qt.CheckState.Checked)
            transcribe_conversion_dict.append(
                {
                    "from": from_str,
                    "to_grok": grok_to,
                    "to_display": display_to,
                    "display_apply": display_apply,
                }
            )
    conversion_dict = []
    for row in range(window.conversion_table.rowCount()):
        from_item = window.conversion_table.item(row, 0)
        sbv2_to_item = window.conversion_table.item(row, 1)
        display_to_item = window.conversion_table.item(row, 2)
        display_item = window.conversion_table.item(row, 3)
        from_str = (from_item.text() if from_item else "").strip()
        to_sbv2 = (sbv2_to_item.text() if sbv2_to_item else "").strip()
        to_display = (display_to_item.text() if display_to_item else "").strip()
        if from_str:
            display_apply = bool(display_item and display_item.checkState() == Qt.CheckState.Checked)
            conversion_dict.append(
                {
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
        pipe_name=window.pipe_edit.text().strip() or "kks_voice_face_events",
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
        source_mode=window.source_mode_combo.currentText().strip().lower() or default_source_mode,
        external_text_enabled=bool(window.external_text_chk.isChecked()),
        external_text_host=window.external_text_host_edit.text().strip() or "127.0.0.1",
        external_text_port=int(window.external_text_port_spin.value()),
        external_text_endpoint=window.external_text_endpoint_edit.text().strip() or "/manual-text",
        external_text_token=window.external_text_token_edit.text().strip(),
        external_text_dedupe_max=int(window.external_text_dedupe_spin.value()),
        transcribe_server_port=_read_transcribe_server_port(config_file),
        sbv2_server_url=window.sbv2_server_url_edit.text().strip() or "http://127.0.0.1:5000",
        sbv2_server_auto_start=window.sbv2_auto_start_chk.isChecked(),
        video_metadata_path=(
            Path(window.video_metadata_edit.text().strip()).expanduser().resolve()
            if window.video_metadata_edit.text().strip()
            else None
        ),
        filter_phrases=filter_phrases,
        transcribe_conversion_dict=transcribe_conversion_dict,
        conversion_dict=conversion_dict,
        grok_detection_mode=window.grok_detection_combo.currentText().strip() or "stop_button",
        grok_text_stable_seconds=float(window.grok_text_stable_spin.value()),
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
        "manual_history": window._manual_history[:50],
        "model_presets": window._model_presets,
        "chrome_debug_port": window.chrome_port_spin.value(),
        "chrome_headless": window.chrome_headless_chk.isChecked(),
        "chrome_profile": window.chrome_profile_combo.currentData() or "",
        "grok_detection_mode": window.grok_detection_combo.currentText().strip() or "stop_button",
        "grok_text_stable_seconds": float(window.grok_text_stable_spin.value()),
    }
    config_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
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
        window.pipe_edit.setText(s("pipe_name", window.pipe_edit.text()))
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
        window.source_mode_combo.setCurrentText(s("source_mode", default_source_mode))
        window.external_text_chk.setChecked(b("external_text_enabled", True))
        window.external_text_host_edit.setText(s("external_text_host", window.external_text_host_edit.text()))
        window.external_text_port_spin.setValue(i("external_text_port", window.external_text_port_spin.value()))
        window.external_text_endpoint_edit.setText(s("external_text_endpoint", window.external_text_endpoint_edit.text()))
        window.external_text_token_edit.setText(s("external_text_token", ""))
        window.external_text_dedupe_spin.setValue(i("external_text_dedupe_max", window.external_text_dedupe_spin.value()))
        window.sbv2_server_url_edit.setText(s("sbv2_server_url", "http://127.0.0.1:5000"))
        window.sbv2_auto_start_chk.setChecked(b("sbv2_server_auto_start", True))
        window.video_metadata_edit.setText(s("video_metadata_path", window.video_metadata_edit.text()))
        window.chrome_port_spin.setValue(i("chrome_debug_port", 9222))
        window.chrome_headless_chk.setChecked(b("chrome_headless", False))
        window.grok_detection_combo.setCurrentText(s("grok_detection_mode", "stop_button"))
        window.grok_text_stable_spin.setValue(f("grok_text_stable_seconds", 1.5))
        saved_profile = s("chrome_profile", "")
        if saved_profile:
            for idx in range(window.chrome_profile_combo.count()):
                if window.chrome_profile_combo.itemData(idx) == saved_profile:
                    window.chrome_profile_combo.setCurrentIndex(idx)
                    break
        phrases = data.get("filter_phrases", [])
        if phrases:
            window.filter_edit.setPlainText("\n".join(phrases))
        stt_conv = data.get("transcribe_conversion_dict", [])
        window.transcribe_conversion_table.setRowCount(0)
        for entry in stt_conv:
            row = window.transcribe_conversion_table.rowCount()
            window.transcribe_conversion_table.insertRow(row)
            window.transcribe_conversion_table.setItem(row, 0, QTableWidgetItem(entry.get("from", "")))
            to_grok = str(entry.get("to_grok", entry.get("to", "")))
            to_display = str(entry.get("to_display", entry.get("to", "")))
            window.transcribe_conversion_table.setItem(row, 1, QTableWidgetItem(to_grok))
            window.transcribe_conversion_table.setItem(row, 2, QTableWidgetItem(to_display))
            window.transcribe_conversion_table.setItem(row, 3, window._new_display_apply_item(bool(entry.get("display_apply", True))))
        conv = data.get("conversion_dict", [])
        window.conversion_table.setRowCount(0)
        for entry in conv:
            row = window.conversion_table.rowCount()
            window.conversion_table.insertRow(row)
            window.conversion_table.setItem(row, 0, QTableWidgetItem(entry.get("from", "")))
            to_sbv2 = str(entry.get("to_sbv2", entry.get("to_grok", entry.get("to", ""))))
            to_display = str(entry.get("to_display", entry.get("to", "")))
            window.conversion_table.setItem(row, 1, QTableWidgetItem(to_sbv2))
            window.conversion_table.setItem(row, 2, QTableWidgetItem(to_display))
            window.conversion_table.setItem(row, 3, window._new_display_apply_item(bool(entry.get("display_apply", False))))
        window._model_presets = [p for p in data.get("model_presets", []) if isinstance(p, dict) and p.get("name")]
        window._refresh_preset_ui()
        history = data.get("manual_history", [])
        window._manual_history = list(history)[:50]
        window.manual_combo.clear()
        for h in window._manual_history:
            window.manual_combo.addItem(h)
    finally:
        window._loading_config = False
