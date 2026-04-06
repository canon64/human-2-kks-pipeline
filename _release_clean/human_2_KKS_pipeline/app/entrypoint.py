from __future__ import annotations

import sys
from typing import Callable, Optional

from PyQt6.QtWidgets import QApplication, QMainWindow

from .constants import MUTEX_NAME
from .single_instance import acquire_single_instance


def run_qt_app(
    window_factory: Callable[[], QMainWindow],
    *,
    argv: Optional[list[str]] = None,
    mutex_name: str = MUTEX_NAME,
) -> int:
    if not acquire_single_instance(mutex_name):
        print("[info] Already running.")
        return 0

    app = QApplication(sys.argv if argv is None else argv)
    window = window_factory()
    window.show()
    return app.exec()

