"""운영 로그 설정 (rotation 적용).

logs/ 아래 runtime.log / error.log / trade.log / review.log 를 RotatingFileHandler 로 생성.
log_dir 별로 고유 logger 이름을 써서 중복 핸들러를 방지한다.
"""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler


def setup_loggers(log_dir: str, max_bytes: int = 1_000_000, backup_count: int = 3) -> dict:
    os.makedirs(log_dir, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    def mk(name: str, filename: str) -> logging.Logger:
        lg = logging.getLogger(f"paper.{name}.{os.path.abspath(log_dir)}")
        lg.setLevel(logging.INFO)
        for h in list(lg.handlers):
            lg.removeHandler(h)
        handler = RotatingFileHandler(
            os.path.join(log_dir, filename), maxBytes=max_bytes,
            backupCount=backup_count, encoding="utf-8")
        handler.setFormatter(fmt)
        lg.addHandler(handler)
        lg.propagate = False
        return lg

    return {
        "runtime": mk("runtime", "runtime.log"),
        "error": mk("error", "error.log"),
        "trade": mk("trade", "trade.log"),
        "review": mk("review", "review.log"),
    }
