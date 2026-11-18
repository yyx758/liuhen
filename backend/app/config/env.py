"""直接从项目根目录的 .env 读取模型配置。"""

from pathlib import Path

from dotenv import dotenv_values


ENV_FILE = Path(__file__).resolve().parents[3] / ".env"
MODEL_ENV_KEYS = ("base_url", "api_key", "model")


def get_model_config() -> dict[str, str | None]:
    values = dotenv_values(ENV_FILE, interpolate=False)
    return {name: values.get(name) for name in MODEL_ENV_KEYS}


def get_judge_config() -> dict[str, str | None]:
    values = dotenv_values(ENV_FILE, interpolate=False)
    return {
        name: values.get(f"judge_{name}") or values.get(name)
        for name in MODEL_ENV_KEYS
    }
