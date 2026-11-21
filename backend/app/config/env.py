"""优先从部署环境变量读取模型配置，本地回退到项目根目录 .env。"""

from pathlib import Path
from os import getenv

from dotenv import dotenv_values


ENV_FILE = Path(__file__).resolve().parents[3] / ".env"
MODEL_ENV_KEYS = ("base_url", "api_key", "model")


def get_model_config() -> dict[str, str | None]:
    values = dotenv_values(ENV_FILE, interpolate=False)
    return {name: getenv(f"LIUHEN_{name.upper()}") or values.get(name) for name in MODEL_ENV_KEYS}


def get_judge_config() -> dict[str, str | None]:
    values = dotenv_values(ENV_FILE, interpolate=False)
    return {
        name: values.get(f"judge_{name}") or values.get(name)
        for name in MODEL_ENV_KEYS
    }
