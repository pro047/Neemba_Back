import asyncpg

from src.config import get_postgres_config


class Db:
    """asyncpg connection pool wrapper.

    Owns a single asyncpg pool built from ``get_postgres_config()``.
    The pool is created/closed by the FastAPI lifespan and shared via
    ``app.state`` (see ``main.py``). ``get_postgres_config()`` uses
    ``require_env`` so missing ``POSTGRES_*`` env vars fail fast at
    startup — same policy as the NATS/DeepL configs.
    """

    def __init__(self) -> None:
        self._pool: asyncpg.Pool | None = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("db pool is not initialized; call create_pool() first")
        return self._pool

    async def create_pool(self, *, min_size: int = 1, max_size: int = 10) -> asyncpg.Pool:
        if self._pool is not None:
            return self._pool

        config = get_postgres_config()
        self._pool = await asyncpg.create_pool(
            host=config["postgres_host"],
            port=int(config["postgres_port"]),
            user=config["postgres_user"],
            password=config["postgres_password"],
            database=config["postgres_database"],
            min_size=min_size,
            max_size=max_size,
        )
        return self._pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
