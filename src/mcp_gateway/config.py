"""Runtime configuration, read from environment variables (and a local .env)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    docs_dir: Path
    log_dir: Path
    clients_file: Path
    routing_file: Path
    discovery_mode: str  # "flat" | "meta"
    agent_model: str
    anthropic_api_key: str | None
    max_agent_steps: int
    sql_row_limit: int


def load_settings(discovery_mode: str | None = None) -> Settings:
    data_dir = Path(os.getenv("GATEWAY_DATA_DIR", ROOT / "data"))
    mode = (discovery_mode or os.getenv("GATEWAY_DISCOVERY", "flat")).lower()
    if mode not in {"flat", "meta"}:
        raise ValueError(f"GATEWAY_DISCOVERY must be 'flat' or 'meta', got {mode!r}")
    return Settings(
        data_dir=data_dir,
        docs_dir=Path(os.getenv("GATEWAY_DOCS_DIR", data_dir / "docs")),
        log_dir=Path(os.getenv("GATEWAY_LOG_DIR", ROOT / "logs")),
        clients_file=Path(os.getenv("GATEWAY_CLIENTS", ROOT / "clients.json")),
        routing_file=Path(os.getenv("GATEWAY_ROUTING_FILE", ROOT / "data" / "routing" / "global.md")),
        discovery_mode=mode,
        agent_model=os.getenv("GATEWAY_AGENT_MODEL", "claude-sonnet-5-5"),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
        max_agent_steps=int(os.getenv("GATEWAY_MAX_AGENT_STEPS", "6")),
        sql_row_limit=int(os.getenv("GATEWAY_SQL_ROW_LIMIT", "50")),
    )
