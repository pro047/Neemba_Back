"""Pure helpers for the unauthenticated client-diagnostics upload.

The route in ``main.py`` only maps the exceptions below to status codes; every
decision (size caps, validation, sanitising, head parsing, ack counts) lives
here so it can be tested without a server or a database.
"""
from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError

# --- limits (contract values) ------------------------------------------------

MAX_BODY_BYTES: int = 2_097_152  # 2 MiB, raw request body (server hard cap)
MAX_ENTRIES: int = 10_000  # entries per request (server hard cap)
MAX_LINE_CHARS: int = 4_000  # per line, in str characters
MAX_APP_VERSION_CHARS: int = 64
MAX_EVENT_CHARS: int = 64  # longer 2nd token -> event NULL
SEQ_MAX: int = 2**63 - 1  # Postgres bigint

# Client guidance only; the server never enforces these.
RECOMMENDED_BATCH_ENTRIES: int = 5_000
RECOMMENDED_BATCH_BYTES: int = 1_048_576


class PayloadTooLarge(Exception):
    """Body or entry count over the hard cap (-> 413)."""


class InvalidUpload(Exception):
    """Malformed upload (-> 422). The app must not resend this batch."""


class ClientLogEntry(BaseModel):
    # StrictInt: JSON true / 1.5 / "7" must be rejected, not coerced.
    seq: StrictInt = Field(ge=0, le=SEQ_MAX)
    line: str = Field(min_length=1)  # length cap is applied by truncation, not rejection


class ClientLogUpload(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    install_id: uuid.UUID = Field(alias="installId")
    app_version: str | None = Field(
        default=None, alias="appVersion", max_length=MAX_APP_VERSION_CHARS
    )
    # Upper bound is a 413, not a 422, so it is checked in decode_upload.
    entries: list[ClientLogEntry] = Field(min_length=1)


class ClientLogAck(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    acked_max_seq: int = Field(alias="ackedMaxSeq")
    inserted: int
    duplicates: int
    truncated: int


@dataclass(frozen=True)
class ParsedEntry:
    seq: int
    line: str  # sanitized, possibly truncated to MAX_LINE_CHARS
    line_truncated: bool
    client_ts: datetime | None  # tz-aware UTC; None when the head doesn't parse
    event: str | None


async def read_body_capped(
    chunks: AsyncIterator[bytes],
    *,
    content_length: str | None,
    limit: int = MAX_BODY_BYTES,
) -> bytes:
    """Read a request body, refusing anything over ``limit`` bytes.

    A declared Content-Length over the limit is rejected before a single chunk
    is pulled. The declared value can lie (or be absent for chunked uploads),
    so the running total is checked too and reading stops at the first excess.
    """
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError:
            declared = None  # unparseable header: rely on the streaming check
        if declared is not None and declared > limit:
            raise PayloadTooLarge()

    buf = bytearray()
    async for chunk in chunks:
        buf += chunk
        if len(buf) > limit:
            raise PayloadTooLarge()
    return bytes(buf)


def sanitize_text(s: str) -> str:
    """Make ``s`` storable in a Postgres text column through asyncpg.

    Postgres text cannot hold U+0000, and asyncpg cannot encode a lone
    surrogate (JSON ``"\\ud800"`` decodes to one). Rejecting the line would make
    the app drop the whole batch (422 = never resend), so replace instead.
    """
    return s.replace("\x00", "�").encode("utf-8", "replace").decode("utf-8")


def _sanitize_tree(value):
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, list):
        return [_sanitize_tree(v) for v in value]
    if isinstance(value, dict):
        return {k: _sanitize_tree(v) for k, v in value.items()}
    return value


def decode_upload(raw: bytes) -> ClientLogUpload:
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError) as e:  # UnicodeDecodeError is a ValueError
        raise InvalidUpload() from e
    if not isinstance(data, dict):
        raise InvalidUpload()

    entries = data.get("entries")
    if isinstance(entries, list) and len(entries) > MAX_ENTRIES:
        raise PayloadTooLarge()

    # pydantic rejects a str holding a lone surrogate, which would turn one bad
    # character into a 422 for the whole batch. Sanitize before validating.
    try:
        data = _sanitize_tree(data)
        return ClientLogUpload.model_validate(data)
    except (ValidationError, RecursionError) as e:
        raise InvalidUpload() from e


def parse_line_head(line: str) -> tuple[datetime | None, str | None]:
    """Extract ``(client_ts, event)`` from ``"<iso-ts> <event> <rest>"``. Never raises."""
    parts = line.split(" ", 2)
    try:
        ts = datetime.fromisoformat(parts[0])
        if ts.tzinfo is None:
            # A naive time has no instant; guessing a zone would corrupt M3's ±5 min join.
            return None, None
        ts = ts.astimezone(UTC)
    except (ValueError, OverflowError):
        return None, None
    event = None
    if len(parts) > 1 and 1 <= len(parts[1]) <= MAX_EVENT_CHARS:
        event = parts[1]
    return ts, event


def normalize_entries(entries: list[ClientLogEntry]) -> list[ParsedEntry]:
    out: list[ParsedEntry] = []
    seen: set[int] = set()
    for e in entries:
        if e.seq in seen:  # first wins within a batch
            continue
        seen.add(e.seq)
        line = sanitize_text(e.line)
        # Parse before truncating so a long line keeps its ts/event.
        ts, event = parse_line_head(line)
        truncated = len(line) > MAX_LINE_CHARS
        if truncated:
            line = line[:MAX_LINE_CHARS]
        out.append(ParsedEntry(e.seq, line, truncated, ts, event))
    return out


def build_ack(
    upload: ClientLogUpload, parsed: list[ParsedEntry], inserted: int
) -> ClientLogAck:
    return ClientLogAck(
        acked_max_seq=max(e.seq for e in upload.entries),
        inserted=inserted,
        duplicates=len(upload.entries) - inserted,
        truncated=sum(1 for p in parsed if p.line_truncated),
    )
