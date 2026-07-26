"""Shared fixtures: a real Postgres test DB (WU2 결정: fake-pool 대신 테스트 DB).

``pg_pool`` 을 요청하는 테스트만 컨테이너를 띄운다 — 나머지 테스트는 docker 가
없어도 그대로 돈다. 세션 시작 시 일회용 ``postgres:16-alpine`` 컨테이너를
띄우고(고정 포트 54329) alembic ``upgrade head`` 로 실제 마이그레이션을 적용,
테스트마다 TRUNCATE 로 독립성을 보장한다. docker 가 없으면 해당 테스트는
사유와 함께 skip 된다.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

SERVICE_ROOT = Path(__file__).resolve().parents[1]

_PG_CONTAINER = "neemba-pytest-pg"
_PG_IMAGE = "postgres:16-alpine"
_PG_HOST = "127.0.0.1"
_PG_PORT = "54329"
_PG_USER = "test"
_PG_PASSWORD = "test"
_PG_DATABASE = "neemba_test"

_PG_ENV = {
    "POSTGRES_HOST": _PG_HOST,
    "POSTGRES_PORT": _PG_PORT,
    "POSTGRES_USER": _PG_USER,
    "POSTGRES_PASSWORD": _PG_PASSWORD,
    "POSTGRES_DATABASE": _PG_DATABASE,
}


async def _wait_for_postgres(timeout: float = 30.0) -> None:
    """TCP 로 접속될 때까지 재시도. postgres 이미지의 초기화용 임시 서버는
    unix socket 만 열므로, TCP 접속 성공 = 최종 서버 준비 완료."""
    import asyncpg

    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            conn = await asyncpg.connect(
                host=_PG_HOST,
                port=int(_PG_PORT),
                user=_PG_USER,
                password=_PG_PASSWORD,
                database=_PG_DATABASE,
            )
        except Exception as e:  # noqa: PERF203 — retry loop, not hot path
            last_err = e
            await asyncio.sleep(0.3)
        else:
            await conn.close()
            return
    msg = f"test postgres not ready in {timeout}s: {last_err!r}"
    raise TimeoutError(msg)


@pytest.fixture(scope="session")
def _pg_server():
    """일회용 테스트 Postgres 컨테이너 (세션 스코프, 요청 시에만 기동)."""
    if shutil.which("docker") is None:
        pytest.skip("docker unavailable — test-DB tests skipped")

    # 이전 실행이 남긴 동명 컨테이너 제거 (실패 무시).
    subprocess.run(
        ["docker", "rm", "-f", _PG_CONTAINER],
        capture_output=True, check=False,
    )
    run = subprocess.run(
        [
            "docker", "run", "-d", "--rm",
            "--name", _PG_CONTAINER,
            "-e", f"POSTGRES_USER={_PG_USER}",
            "-e", f"POSTGRES_PASSWORD={_PG_PASSWORD}",
            "-e", f"POSTGRES_DB={_PG_DATABASE}",
            "-p", f"{_PG_HOST}:{_PG_PORT}:5432",
            _PG_IMAGE,
        ],
        capture_output=True, text=True, check=False,
    )
    if run.returncode != 0:
        pytest.skip(f"cannot start test postgres: {run.stderr.strip()}")

    try:
        asyncio.run(_wait_for_postgres())
        # 실제 마이그레이션으로 스키마 적용 (스키마 정의 중복 없음 + 마이그레이션 검증).
        upgrade = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=SERVICE_ROOT,
            env={**os.environ, **_PG_ENV},
            capture_output=True, text=True, check=False,
        )
        if upgrade.returncode != 0:
            msg = f"alembic upgrade failed:\n{upgrade.stderr}"
            raise RuntimeError(msg)
        yield _PG_ENV
    finally:
        subprocess.run(
            ["docker", "rm", "-f", _PG_CONTAINER],
            capture_output=True, check=False,
        )


@pytest.fixture
async def pg_pool(_pg_server):
    """테스트당 새 asyncpg pool + 테이블 초기화 (테스트 간 의존성 차단)."""
    import asyncpg

    pool = await asyncpg.create_pool(
        host=_PG_HOST,
        port=int(_PG_PORT),
        user=_PG_USER,
        password=_PG_PASSWORD,
        database=_PG_DATABASE,
        min_size=1,
        max_size=5,
    )
    async with pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE app.translations, app.sessions, app.ws_blips "
            "RESTART IDENTITY"
        )
    yield pool
    await pool.close()
