"""配置管理模块：支持 .env 加载与运行时热更新 API Key（无需重启服务）。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
SAMPLE_DIR = ROOT_DIR / "sample_data"
DATA_DIR = ROOT_DIR / "data"
DB_PATH = DATA_DIR / "audit_pilot.db"
ENV_PATH = ROOT_DIR / ".env"

if ENV_PATH.exists():
    load_dotenv(ENV_PATH)


@dataclass
class RuntimeConfig:
    primary_base_url: str = os.getenv("PRIMARY_LLM_BASE_URL", "https://api.deepseek.com/v1")
    primary_api_key: str = os.getenv("PRIMARY_LLM_API_KEY", "")
    primary_model: str = os.getenv("PRIMARY_LLM_MODEL", "deepseek-chat")

    backup_base_url: str = os.getenv("BACKUP_LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    backup_api_key: str = os.getenv("BACKUP_LLM_API_KEY", "")
    backup_model: str = os.getenv("BACKUP_LLM_MODEL", "qwen-plus")

    llm_timeout_seconds: float = float(os.getenv("LLM_TIMEOUT_SECONDS", "20"))
    query_cache_ttl_seconds: int = int(os.getenv("QUERY_CACHE_TTL_SECONDS", "300"))
    simulate_primary_failure: bool = os.getenv("SIMULATE_PRIMARY_FAILURE", "false").lower() == "true"

    @property
    def has_primary_key(self) -> bool:
        return bool(self.primary_api_key.strip())

    @property
    def has_backup_key(self) -> bool:
        return bool(self.backup_api_key.strip())

    @property
    def mode(self) -> str:
        if self.has_primary_key or self.has_backup_key:
            return "live"
        return "demo"

    def to_public_dict(self) -> dict:
        def _mask_key(k: str) -> str:
            k = k.strip()
            if not k:
                return ""
            if len(k) <= 8:
                return "****"
            return f"{k[:4]}****{k[-4:]}"

        return {
            "mode": self.mode,
            "primary_base_url": self.primary_base_url,
            "primary_model": self.primary_model,
            "primary_key_configured": self.has_primary_key,
            "primary_key_preview": _mask_key(self.primary_api_key),
            "backup_base_url": self.backup_base_url,
            "backup_model": self.backup_model,
            "backup_key_configured": self.has_backup_key,
            "backup_key_preview": _mask_key(self.backup_api_key),
            "llm_timeout_seconds": self.llm_timeout_seconds,
            "query_cache_ttl_seconds": self.query_cache_ttl_seconds,
            "simulate_primary_failure": self.simulate_primary_failure,
        }


runtime_config = RuntimeConfig()
