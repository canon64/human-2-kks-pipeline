from __future__ import annotations

import threading

from PyQt6.QtCore import QObject, pyqtSignal

from services.rtfw_lan_service import probe_rtfw


class _ProbeSignals(QObject):
    finished = pyqtSignal(dict)


class RtfwLanController:
    def __init__(self, window, *, log) -> None:
        self.window = window
        self.log = log
        self._signals = _ProbeSignals()
        self._signals.finished.connect(self._probe_finished)
        self._probing = False
        window.rtfw_connect_btn.clicked.connect(self.probe)
        window.fw_backend_combo.currentIndexChanged.connect(self._backend_changed)
        self._backend_changed()

    def _backend_changed(self, *_args) -> None:
        remote = str(self.window.fw_backend_combo.currentData() or "local") == "rtfw_lan"
        self.window.rtfw_host_edit.setEnabled(remote)
        self.window.rtfw_port_spin.setEnabled(remote)
        self.window.rtfw_connect_btn.setEnabled(remote and not self._probing)
        if not remote:
            self.window.rtfw_status_label.setText("ローカルFW")
            self.window.rtfw_route_label.setText("既存WAV → ローカル転写")

    def probe(self) -> None:
        if self._probing:
            return
        self._probing = True
        self.window.rtfw_connect_btn.setEnabled(False)
        self.window.rtfw_status_label.setText("接続・認証確認中")
        host = self.window.rtfw_host_edit.text().strip()
        port = int(self.window.rtfw_port_spin.value())

        def work() -> None:
            self._signals.finished.emit(probe_rtfw(host=host, port=port))

        threading.Thread(target=work, name="RTFW.Probe", daemon=True).start()

    def _probe_finished(self, result: dict) -> None:
        self._probing = False
        self._backend_changed()
        if result.get("ok"):
            self.window.rtfw_status_label.setText("接続OK / 認証OK")
            self.window.rtfw_route_label.setText("本番は既存WAV保存後に送信")
            self.log("[rtfw] probe connected=1 authorized=1")
        else:
            error = str(result.get("error") or "connection failed")
            self.window.rtfw_status_label.setText("接続または認証に失敗")
            self.window.rtfw_route_label.setText(error[:160])
            self.log(f"[rtfw] probe failed: {error}")

    def update_runtime_status(self, status: dict) -> None:
        stage = str(status.get("stage") or "")
        labels = {
            "wav_ready": "既存録音WAVを取得",
            "connect": "サブPCへ接続中",
            "auth": "接続OK / 認証OK / 音声送信中",
            "session_begin": "区間確定済みクリップ / サブPC側VADを迂回",
            "send": "既存WAVのPCMを送信中",
            "ack": "音声ACK完了",
            "flush": "session.flush送信 / 確定待ち",
            "partial": "推論中",
            "final": "確定結果受信 / パイプライン投入",
            "error": "RTFW経路エラー",
        }
        self.window.rtfw_status_label.setText(labels.get(stage, stage or "待機"))
        if stage == "final":
            self.window.rtfw_route_label.setText(
                f"ACK {status.get('acked', 0)} / drop {status.get('dropped', 0)} / 確定 {status.get('textChars', 0)}文字"
            )
        elif stage == "error":
            self.window.rtfw_route_label.setText(str(status.get("error") or "")[:160])
        elif stage == "session_begin":
            self.window.rtfw_route_label.setText("Human側で区間確定済み → サブPC側VADなし → 全体を1回推論")

    def stop(self) -> None:
        return None
