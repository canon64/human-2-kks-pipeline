from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class AppConfig:
    # Recorder
    wav_dir: Path
    threshold_dbfs: float
    silence_seconds: float
    min_duration_seconds: float
    pre_roll_seconds: float
    post_roll_seconds: float
    device: Optional[int]
    vr_ptt_enabled: bool
    vr_ptt_host: str
    vr_ptt_port: int
    vr_ptt_token: str
    vr_ptt_timeout_seconds: float
    # Pipeline
    kks_root: Path
    output_dir: Path
    save_fasterwhisper_text: bool
    save_source_wav: bool
    save_sbv2_input_text: bool
    save_sbv2_output_wav: bool
    faster_python: Path
    faster_model: str
    faster_device: str
    faster_compute: str
    faster_language: str
    faster_beam: int
    pipeline_python: Path
    sbv2_root: Path
    sbv2_model_name: str
    sbv2_model_file: str
    sbv2_speaker: str
    sbv2_style: str
    sbv2_length: float
    voice_volume: float
    voice_pitch: float
    pipe_name: str
    target_host: str
    target_port: int
    target_endpoint: str
    target_token: str
    remote_http: bool
    subtitle_send_enabled: bool
    subtitle_target_host: str
    subtitle_target_port: int
    subtitle_endpoint: str
    subtitle_token: str
    subtitle_timeout_sec: float
    main_index: int
    face: int
    keep_current_face: bool
    face_send_mode: str
    face_preset_id: str
    face_preset_name: str
    face_preset_random: bool
    source_mode: str
    external_text_enabled: bool
    external_text_host: str
    external_text_port: int
    external_text_endpoint: str
    external_text_token: str
    external_text_dedupe_max: int
    max_response_chars_enabled: bool = True
    max_response_chars: int = 3000
    transcribe_server_port: int = 18760
    video_metadata_path: Optional[Path] = None
    sbv2_mode: str = "auto"
    sbv2_server_url: str = "http://127.0.0.1:5000"
    sbv2_server_auto_start: bool = True
    diagnostic_log_enabled: bool = False
    diagnostic_log_interval_ms: int = 1000
    filter_phrases: list[dict] = field(default_factory=list)
    transcribe_conversion_dict: list[dict] = field(default_factory=list)
    conversion_dict: list[dict] = field(default_factory=list)


