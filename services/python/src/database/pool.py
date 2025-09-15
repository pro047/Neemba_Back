import asyncpg
from services.python.src.config import postgres_host, postgres_port, postgres_user, postgres_password, postgres_database


class Db:
    def __init__(self) -> None:
        pass

    async def create_pool(self):
        pool = await asyncpg.create_pool(
            host=postgres_host,
            port=postgres_port,
            user=postgres_user,
            password=postgres_password,
            database=postgres_database
        )
        return pool
