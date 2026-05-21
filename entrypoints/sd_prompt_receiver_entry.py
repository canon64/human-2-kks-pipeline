from __future__ import annotations

from typing import Optional

from sd_prompt_receiver.server import main as receiver_main


def run_sd_prompt_receiver(argv: Optional[list[str]] = None) -> int:
    return int(receiver_main(argv))
