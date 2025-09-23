import os
from dotenv import load_dotenv, find_dotenv


def load_env_or_fail() -> None:
    env_path = find_dotenv()
    if not env_path:
        raise RuntimeError(f'no .env file found (cwd={os.getcwd()})')
    loaded = load_dotenv(env_path, override=False)
    if not loaded:
        raise RuntimeError(f'failed to load .env: {env_path}')
    print(f'env loaded: {env_path}')


def require_env(key: str, *, mask: bool = False) -> str:
    value = os.getenv(key)
    print(value)
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


def get_ws_url() -> dict[str, str]:
    print('ws_url', os.getenv('WS_URL'))
    return {
        "ws_url": require_env("WS_URL")
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
