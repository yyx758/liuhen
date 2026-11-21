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


def get_auth_config() -> dict:
    """登录 Cookie 与 CSRF 来源校验的配置；部署环境变量优先，本地回退到 .env。"""
    values = dotenv_values(ENV_FILE, interpolate=False)

    def read(name: str, default: str) -> str:
        return getenv(f"LIUHEN_{name.upper()}") or values.get(name) or default

    return {
        # 本地 http 开发可设为 false；线上经 HTTPS 访问必须为 true。
        "cookie_secure": read("cookie_secure", "true").lower() in {"1", "true", "yes"},
        # 允许发起写请求的页面来源（逗号分隔），用于校验 Origin 头。
        "allowed_origins": {
            origin.strip().rstrip("/")
            for origin in read("allowed_origins", "http://127.0.0.1:5173,http://localhost:5173").split(",")
            if origin.strip()
        },
    }
