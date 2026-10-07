"""Local configuration. Secrets never become frontend configuration fields."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    host: str
    port: int
    model_name: str | None
    behavior_version: str
    fixed_now: datetime | None

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv(override=False)
        clock = os.getenv("CLINIC_AGENT_FIXED_NOW", "").strip()
        fixed_now = datetime.fromisoformat(clock.replace("Z", "+00:00")) if clock else None
        if fixed_now is not None and fixed_now.tzinfo is None:
            raise ValueError("CLINIC_AGENT_FIXED_NOW must include a timezone")
        return cls(
            data_dir=Path(os.getenv("CLINIC_AGENT_DATA_DIR", ".data")).resolve(),
            host=os.getenv("CLINIC_AGENT_HOST", "127.0.0.1"),
            port=int(os.getenv("CLINIC_AGENT_PORT", "8000")),
            model_name=os.getenv("AGENT_MODEL", "").strip() or None,
            behavior_version=os.getenv("CLINIC_AGENT_BEHAVIOR_VERSION", "v1"),
            fixed_now=fixed_now,
        )

    def ensure_storage(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
