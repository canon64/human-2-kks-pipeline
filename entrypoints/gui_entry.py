from __future__ import annotations

import sys
from typing import Callable, Optional

from PyQt6.QtWidgets import QApplication, QMainWindow

from config.constants import MUTEX_NAME
from core.single_instance import acquire_single_instance


def run_gui(
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


def run_gui_default(argv: Optional[list[str]] = None) -> int:
    # Delay import to avoid circular dependency when human_2_KKS_pipeline imports this module.
    from human_2_KKS_pipeline import MainWindow

    return run_gui(MainWindow, argv=argv, mutex_name=MUTEX_NAME)

