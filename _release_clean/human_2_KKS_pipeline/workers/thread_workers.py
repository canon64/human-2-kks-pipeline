from __future__ import annotations

from PyQt6.QtCore import QThread, pyqtSignal


class SeleniumWorker(QThread):
    result_ready = pyqtSignal(str, object)  # (status, driver)
    error_occurred = pyqtSignal(str)

    def __init__(self, func):
        super().__init__()
        self._func = func

    def run(self):
        try:
            status, driver = self._func()
            self.result_ready.emit(status or "skipped", driver)
        except Exception as e:
            self.error_occurred.emit(str(e))


class TaskWorker(QThread):
    result_ready = pyqtSignal(object)
    error_occurred = pyqtSignal(str)

    def __init__(self, func):
        super().__init__()
        self._func = func

    def run(self):
        try:
            self.result_ready.emit(self._func())
        except Exception as e:
            self.error_occurred.emit(str(e))

