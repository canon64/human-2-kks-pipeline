from __future__ import annotations

import traceback
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from voice_gate_recorder import RecorderConfig, VoiceGateRecorder


class RecorderWorker(QObject):
    finished = pyqtSignal()
    error = pyqtSignal(str)
    log = pyqtSignal(str)

    def __init__(self, config: RecorderConfig) -> None:
        super().__init__()
        self._config = config
        self._recorder: Optional[VoiceGateRecorder] = None

    @pyqtSlot()
    def run(self) -> None:
        try:
            self._recorder = VoiceGateRecorder(self._config, log_callback=self.log.emit)
            self._recorder.run()
        except Exception:
            self.error.emit(traceback.format_exc())
        finally:
            self.finished.emit()

    def stop(self) -> None:
        if self._recorder is not None:
            self._recorder.stop()

    def update_live_config(
        self,
        *,
        output_dir: Path,
        threshold_dbfs: float,
        silence_seconds: float,
        min_duration_seconds: float,
        pre_roll_seconds: float,
        post_roll_seconds: float,
        device: Optional[int],
        diagnostic_log_enabled: bool,
        diagnostic_log_interval_ms: int,
    ) -> None:
        self._config.output_dir = output_dir
        self._config.threshold_dbfs = threshold_dbfs
        self._config.silence_seconds = silence_seconds
        self._config.min_duration_seconds = min_duration_seconds
        self._config.pre_roll_seconds = pre_roll_seconds
        self._config.post_roll_seconds = post_roll_seconds
        self._config.device = device
        self._config.diagnostic_log_enabled = bool(diagnostic_log_enabled)
        self._config.diagnostic_log_interval_ms = max(100, int(diagnostic_log_interval_ms))

        if self._recorder is None:
            return

        rec = self._recorder
        rec.cfg.output_dir = output_dir
        rec.cfg.threshold_dbfs = threshold_dbfs
        rec.cfg.silence_seconds = silence_seconds
        rec.cfg.min_duration_seconds = min_duration_seconds
        rec.cfg.pre_roll_seconds = pre_roll_seconds
        rec.cfg.post_roll_seconds = post_roll_seconds
        rec.cfg.device = device
        rec.cfg.diagnostic_log_enabled = bool(diagnostic_log_enabled)
        rec.cfg.diagnostic_log_interval_ms = max(100, int(diagnostic_log_interval_ms))
        rec.silence_limit_samples = int(silence_seconds * rec.cfg.sample_rate)
        rec.pre_roll_limit_samples = int(pre_roll_seconds * rec.cfg.sample_rate)
        rec.post_roll_keep_samples = int(post_roll_seconds * rec.cfg.sample_rate)

