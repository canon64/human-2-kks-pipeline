from __future__ import annotations

import logging
import os
from datetime import datetime

from .config import BridgeConfig


def setup_logger(config: BridgeConfig, base_dir: str) -> logging.Logger:
    logger = logging.getLogger("grok_bridge")
    if logger.handlers:
        return logger

    level_name = (config.log.level or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logger.setLevel(level)
    logger.propagate = False

    log_dir = os.path.join(base_dir, config.log.directory)
    os.makedirs(log_dir, exist_ok=True)
    log_name = f"{config.log.file_prefix}_{datetime.now():%Y%m%d}.log"
    log_path = os.path.join(log_dir, log_name)

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    logger.info("logger_ready path=%s", log_path)
    return logger
