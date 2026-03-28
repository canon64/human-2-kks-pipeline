from __future__ import annotations

from config.constants import MUTEX_NAME
from entrypoints.gui_entry import run_gui
from gui.main_window import MainWindow


def main() -> int:
    return run_gui(MainWindow, mutex_name=MUTEX_NAME)


if __name__ == "__main__":
    raise SystemExit(main())
