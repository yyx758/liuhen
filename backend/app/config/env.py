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


def get_usage_config() -> dict:
    """每日对话上限的默认值（D9），用户未单独设置时使用。"""
    values = dotenv_values(ENV_FILE, interpolate=False)
    raw = getenv("LIUHEN_DEFAULT_DAILY_CHAT_LIMIT") or values.get("default_daily_chat_limit") or "50"
    limit = int(raw)
    if not 1 <= limit <= 1000:
        raise RuntimeError("default_daily_chat_limit 须在 1–1000 之间")
    return {"default_daily_chat_limit": limit}


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


def get_smtp_config() -> dict:
    """发信用的 SMTP 配置；部署环境变量优先，本地回退到 .env。每次调用都重新读取，未配置时各项为 None。"""
    values = dotenv_values(ENV_FILE, interpolate=False)

    def read(name: str) -> str | None:
        return getenv(f"LIUHEN_{name.upper()}") or values.get(name) or None

    return {
        "host": read("smtp_host"),
        "port": int(read("smtp_port") or 465),
        # 完整邮箱地址，同时作为发件地址。
        "username": read("smtp_username"),
        # QQ 邮箱填 SMTP 授权码，不是 QQ 密码。
        "password": read("smtp_password"),
    }
