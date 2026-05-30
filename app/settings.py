# -*- coding: utf-8 -*-
import os
import sys
import asyncio
from pathlib import Path
from urllib.parse import urlparse

# === 引入所需库 ===
from hcaptcha_challenger.agent import AgentConfig
from pydantic import Field, SecretStr
from pydantic_settings import SettingsConfigDict
from loguru import logger

# --- 核心路径定义 ---
PROJECT_ROOT = Path(__file__).parent
VOLUMES_DIR = PROJECT_ROOT.joinpath("volumes")
LOG_DIR = VOLUMES_DIR.joinpath("logs")
USER_DATA_DIR = VOLUMES_DIR.joinpath("user_data")
RUNTIME_DIR = VOLUMES_DIR.joinpath("runtime")
SCREENSHOTS_DIR = VOLUMES_DIR.joinpath("screenshots")
RECORD_DIR = VOLUMES_DIR.joinpath("record")
HCAPTCHA_DIR = VOLUMES_DIR.joinpath("hcaptcha")

# === 配置类定义 ===
class EpicSettings(AgentConfig):
    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")

    GEMINI_BASE_URL: str = Field(
        default=os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com"),
        description="Gemini API Base URL",
    )

    GEMINI_API_KEY: SecretStr | None = Field(
        default_factory=lambda: os.getenv("GEMINI_API_KEY"),
        description="Gemini API Key",
    )
    
    GEMINI_MODEL: str = Field(
        default=os.getenv("GEMINI_MODEL", "gemini-3.5-flash"),
        description="模型名称",
    )

    CHALLENGE_CLASSIFIER_MODEL: str = Field(
        default_factory=lambda: os.getenv(
            "CHALLENGE_CLASSIFIER_MODEL", os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
        ),
        description="挑战分类模型名称",
    )

    IMAGE_CLASSIFIER_MODEL: str = Field(
        default_factory=lambda: os.getenv(
            "IMAGE_CLASSIFIER_MODEL", os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
        ),
        description="图像分类模型名称",
    )

    SPATIAL_POINT_REASONER_MODEL: str = Field(
        default_factory=lambda: os.getenv(
            "SPATIAL_POINT_REASONER_MODEL", os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
        ),
        description="空间点推理模型名称",
    )

    SPATIAL_PATH_REASONER_MODEL: str = Field(
        default_factory=lambda: os.getenv(
            "SPATIAL_PATH_REASONER_MODEL", os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
        ),
        description="空间路径推理模型名称",
    )

    EPIC_EMAIL: str = Field(default_factory=lambda: os.getenv("EPIC_EMAIL"))
    EPIC_PASSWORD: SecretStr = Field(default_factory=lambda: os.getenv("EPIC_PASSWORD"))
    DISABLE_BEZIER_TRAJECTORY: bool = Field(default=True)

    cache_dir: Path = HCAPTCHA_DIR.joinpath(".cache")
    challenge_dir: Path = HCAPTCHA_DIR.joinpath(".challenge")
    captcha_response_dir: Path = HCAPTCHA_DIR.joinpath(".captcha")

    ENABLE_APSCHEDULER: bool = Field(default=True)
    TASK_TIMEOUT_SECONDS: int = Field(default=900)
    REDIS_URL: str = Field(default="redis://redis:6379/0")
    CELERY_WORKER_CONCURRENCY: int = Field(default=1)
    CELERY_TASK_TIME_LIMIT: int = Field(default=1200)
    CELERY_TASK_SOFT_TIME_LIMIT: int = Field(default=900)

    @property
    def user_data_dir(self) -> Path:
        target_ = USER_DATA_DIR.joinpath(self.EPIC_EMAIL)
        target_.mkdir(parents=True, exist_ok=True)
        return target_

settings = EpicSettings()
settings.ignore_request_questions = ["Please drag the crossing to complete the lines"]
