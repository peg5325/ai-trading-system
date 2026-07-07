"""환경설정 로딩. .env 에서 Toss 자격증명과 DB 경로를 읽는다."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    toss_base: str
    toss_client_id: str
    toss_client_secret: str
    db_path: Path

    @property
    def has_credentials(self) -> bool:
        return bool(self.toss_client_id and self.toss_client_secret)


def load_settings() -> Settings:
    return Settings(
        toss_base=os.getenv("TOSS_API_BASE", "https://openapi.tossinvest.com").rstrip("/"),
        toss_client_id=os.getenv("TOSS_CLIENT_ID", "").strip(),
        toss_client_secret=os.getenv("TOSS_CLIENT_SECRET", "").strip(),
        db_path=PROJECT_ROOT / "data" / "market.sqlite",
    )
