"""Sensitive-data masking (mask-at-write).

Pure, dependency-free functions used to strip PII before it is persisted to
``app.translations`` *and* before it is fanned out on the monitor WebSocket
(see docs/monitoring-plan.md §6). The same util runs on both the source
(원문) and the translated (번역문) text.

Targeted Korean-service patterns (regex):
- 주민등록번호 (resident registration number)
- 휴대폰번호 (mobile phone number)
- 이메일 (email)
- 카드번호 (card number)

Names and bank-account numbers are intentionally *not* masked: their formats
are too ambiguous and produce too many false positives (see §6).

Ordering matters. We mask the longest / most-specific digit patterns first
(card → RRN → phone) so a 16-digit card is never partially eaten by the
phone pattern. Each numeric pattern is fenced with ``(?<!\\d)``/``(?!\\d)``
lookarounds so it only matches a whole run of digits, never a slice of a
longer number (e.g. a 13-digit id is left alone by the phone rule).
"""
from __future__ import annotations

import re

# email — masked first; it never overlaps the numeric patterns.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# 카드번호 — 16 digits as 4 groups of 4, optional - or space separators.
_CARD_RE = re.compile(r"(?<!\d)(?:\d{4}[-\s]?){3}\d{4}(?!\d)")

# 주민등록번호 — YYMMDD + separator + gender digit (1-8) + 6 digits (13 total).
_RRN_RE = re.compile(r"(?<!\d)\d{6}[-\s]?[1-8]\d{6}(?!\d)")

# 휴대폰번호 — 01[016789] + 3~4 digits + 4 digits (mobile only; landlines such
# as 02-xxx-xxxx are deliberately left unmasked to avoid false positives).
_PHONE_RE = re.compile(r"(?<!\d)01[016789][-\s]?\d{3,4}[-\s]?\d{4}(?!\d)")

# (pattern, replacement) applied in order.
_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (_EMAIL_RE, "[EMAIL]"),
    (_CARD_RE, "[CARD]"),
    (_RRN_RE, "[RRN]"),
    (_PHONE_RE, "[PHONE]"),
)


def mask_text(text: str | None) -> str | None:
    """Return ``text`` with detected PII replaced by ``[TYPE]`` placeholders.

    ``None`` is passed through unchanged so callers can mask optional fields
    without a guard. Non-string input is coerced via ``str`` for safety.
    """
    if text is None:
        return None
    if not isinstance(text, str):
        text = str(text)
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    return text
