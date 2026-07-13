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
    fw_backend: str
    rtfw_host: str
    rtfw_port: int
    pipeline_python: Path
    llm_backend: str
    llm_base_url: str
    llm_model: str
    llm_api_key: str
    llm_system_prompt: str
    llm_temperature: float
    llm_max_tokens: int
    llm_timeout_seconds: float
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
    sd_prompt_begin_tag: str = "[SD_PROMPT_BEGIN]"
    sd_prompt_end_tag: str = "[SD_PROMPT_END]"
    sd_prompt_send_enabled: bool = False
    sd_prompt_target_host: str = "192.168.11.10"
    sd_prompt_target_port: int = 7860
    sd_prompt_endpoint: str = "/sdapi/v1/txt2img"
    sd_prompt_token: str = ""
    sd_prompt_timeout_sec: float = 40.0
    sd_prompt_generate_forever: bool = False
    sd_preview_auto_show: bool = False
    sd_control_port: int = 18768
    sd_slideshow_interval_sec: int = 20
    sd_blankmap_sync_enabled: bool = True
    sd_blankmap_status_host: str = "127.0.0.1"
    sd_blankmap_status_port: int = 55782
    sd_blankmap_status_endpoint: str = "/slideshow/status"
    sd_blankmap_status_timeout_sec: float = 1.0
    sd_prompt_model_checkpoint: str = ""
    sd_prompt_vae: str = ""
    sd_prompt_clip_skip: int = 0
    sd_prompt_append_prompt: str = ""
    sd_prompt_negative_prompt: str = ""
    sd_prompt_steps: int = 20
    sd_prompt_width: int = 512
    sd_prompt_height: int = 768
    sd_prompt_cfg_scale: float = 7.0
    sd_prompt_sampler_name: str = ""
    sd_prompt_scheduler: str = ""
    sd_prompt_seed: int = -1
    sd_prompt_subseed: int = -1
    sd_prompt_subseed_strength: float = 0.0
    sd_prompt_batch_size: int = 1
    sd_prompt_n_iter: int = 1
    sd_prompt_restore_faces: bool = False
    sd_prompt_tiling: bool = False
    sd_prompt_save_images: bool = True
    sd_prompt_send_images: bool = False
    sd_prompt_enable_hr: bool = False
    sd_prompt_hr_scale: float = 2.0
    sd_prompt_hr_upscaler: str = "Latent"
    sd_prompt_hr_second_pass_steps: int = 0
    sd_prompt_denoising_strength: float = 0.45
    sd_prompt_hr_resize_x: int = 0
    sd_prompt_hr_resize_y: int = 0
    sd_prompt_hr_sampler_name: str = ""
    sd_prompt_hr_scheduler: str = ""
    sd_prompt_hr_checkpoint_name: str = ""
    sd_prompt_hr_prompt: str = ""
    sd_prompt_hr_negative_prompt: str = ""
    sd_prompt_extra_payload_json: str = ""
    max_response_chars_enabled: bool = True
    max_response_chars: int = 3000
    transcribe_server_port: int = 18760
    video_metadata_path: Optional[Path] = None
    sbv2_mode: str = "auto"
    sbv2_server_url: str = "http://127.0.0.1:5000"
    sbv2_server_auto_start: bool = True
    diagnostic_log_enabled: bool = False
    diagnostic_log_interval_ms: int = 1000
    translate_enabled: bool = False
    translate_source: str = "auto"
    translate_target: str = "ja"
    translate_input_subtitle_original: bool = True
    translate_response_enabled: bool = False
    translate_response_target: str = "en"
    translate_voice_enabled: bool = False
    translate_voice_target: str = "ja"
    filter_phrases: list[dict] = field(default_factory=list)
    transcribe_conversion_dict: list[dict] = field(default_factory=list)
    conversion_dict: list[dict] = field(default_factory=list)
    sd_prompt_rewrite_rules: list[dict] = field(default_factory=list)

