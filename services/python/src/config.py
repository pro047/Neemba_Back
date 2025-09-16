import os
from typing import Final
from dotenv import load_dotenv


def load_env_or_fail() -> None:
    path = "/app"
    if os.path.exists(path):
        load_dotenv(dotenv_path=path, override=False)
        print(f'env loaded: {path}')
        return
    raise RuntimeError(
        f'no env file found (cwd={os.getcwd()} / path = {path}) / exists = {os.path.exists(path)}')


def require_env(key: str, *, mask: bool = True) -> str:
    value = os.getenv(key)
    if value is None or value == "":
        raise RuntimeError(f'env {key} is not configured')
    if mask:
        print(f'env {key} = **** ({len(value)} chars)')
    else:
        print(f'env {key} = {value}')
    return value


def require_env_int(key: str) -> int:
    raw = require_env(key)
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f'en {key} must be an integer, got: {raw!r}')


load_env_or_fail()


def get_nats_config() -> dict[str, str]:
    return {
        "nats_url": require_env("NATS_URL"),
        "nats_subject": require_env("NATS_SUBJECT"),
        "nats_stream_name": require_env("NATS_STREAM_NAME"),
        "nats_consumer_name": require_env("NATS_CONSUMER_NAME")
    }


def get_deepl_config() -> dict[str, str]:
    return {

        "deepl_api_key": require_env("DEEPL_API_KEY")
    }


def get_postgres_config() -> dict[str, str]:
    return {
        "postgres_host": require_env("POSTGRES_HOST"),
        "postgres_port": require_env("POSTGRES_PORT"),
        "postgres_user": require_env("POSTGRES_USER"),
        "postgres_password": require_env("POSTGRES_PASSWORD"),
        "postgres_database": require_env("POSTGRES_DATABASE"),
    }
