from __future__ import annotations

from config.models import AppConfig


def deferred_live_fields(cfg_prev: AppConfig, cfg_now: AppConfig) -> list[str]:
    fields: list[str] = []
    if cfg_prev.wav_dir != cfg_now.wav_dir:
        fields.append("wav_dir")
    if cfg_prev.faster_python != cfg_now.faster_python:
        fields.append("faster_python")
    if cfg_prev.faster_model != cfg_now.faster_model:
        fields.append("faster_model")
    if cfg_prev.faster_device != cfg_now.faster_device:
        fields.append("faster_device")
    if cfg_prev.faster_compute != cfg_now.faster_compute:
        fields.append("faster_compute")
    if cfg_prev.faster_language != cfg_now.faster_language:
        fields.append("faster_language")
    if cfg_prev.faster_beam != cfg_now.faster_beam:
        fields.append("faster_beam")
    if cfg_prev.transcribe_server_port != cfg_now.transcribe_server_port:
        fields.append("transcribe_server_port")
    if cfg_prev.sbv2_root != cfg_now.sbv2_root:
        fields.append("sbv2_root")
    if cfg_prev.sbv2_server_url != cfg_now.sbv2_server_url:
        fields.append("sbv2_server_url")
    if cfg_prev.sbv2_server_auto_start != cfg_now.sbv2_server_auto_start:
        fields.append("sbv2_server_auto_start")
    if cfg_prev.external_text_enabled != cfg_now.external_text_enabled:
        fields.append("external_text_enabled")
    if cfg_prev.external_text_host != cfg_now.external_text_host:
        fields.append("external_text_host")
    if cfg_prev.external_text_port != cfg_now.external_text_port:
        fields.append("external_text_port")
    if cfg_prev.external_text_endpoint != cfg_now.external_text_endpoint:
        fields.append("external_text_endpoint")
    if cfg_prev.external_text_token != cfg_now.external_text_token:
        fields.append("external_text_token")
    if cfg_prev.external_text_dedupe_max != cfg_now.external_text_dedupe_max:
        fields.append("external_text_dedupe_max")
    if cfg_prev.device != cfg_now.device:
        fields.append("device")
    return fields


def apply_live_settings(window, cfg: AppConfig) -> None:
    prev = window._active_runtime_cfg
    window._active_runtime_cfg = cfg
    if not window._running:
        window._last_deferred_live_fields = tuple()
        return

    if window._pipeline_worker is None:
        window._pending_cfg = cfg
        return

    window._pipeline_worker.update_runtime_config(cfg)

    if window._recorder_worker is not None:
        window._recorder_worker.update_live_config(
            output_dir=cfg.wav_dir,
            threshold_dbfs=cfg.threshold_dbfs,
            silence_seconds=cfg.silence_seconds,
            min_duration_seconds=cfg.min_duration_seconds,
            pre_roll_seconds=cfg.pre_roll_seconds,
            post_roll_seconds=cfg.post_roll_seconds,
            device=cfg.device,
        )

    need_rec = window._is_recorder_needed(cfg)
    has_rec = window._recorder_worker is not None
    if need_rec and (not has_rec) and (not window._paused):
        window._start_recorder(cfg)
        window._append_log("[live] 録音ワーカーを開始")
    elif (not need_rec) and has_rec:
        window._stop_recorder()
        window._append_log("[live] source_mode=external: 録音ワーカー停止")

    if prev is None:
        window._last_deferred_live_fields = tuple()
        return

    deferred = tuple(deferred_live_fields(prev, cfg))
    if deferred and deferred != window._last_deferred_live_fields:
        window._append_log("[live] 反映保留(再起動時): " + ", ".join(deferred))
    window._last_deferred_live_fields = deferred


def on_live_setting_changed(window, *_args) -> None:
    if window._loading_config:
        return
    cfg = window._save_config()
    if cfg is None:
        return
    apply_live_settings(window, cfg)


def on_any_setting_changed(window, *_args) -> None:
    on_live_setting_changed(window, *_args)

