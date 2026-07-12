"""Unit tests for the PII masking util (positive + negative per pattern)."""
import pytest

from src.masking import mask_text


# --- positive cases: sensitive value must be removed --------------------------

@pytest.mark.parametrize("text, token", [
    # 주민등록번호
    ("내 번호는 901010-1234567 입니다", "[RRN]"),
    ("9010101234567 확인", "[RRN]"),
    ("foreigner 901010-5234567", "[RRN]"),
    # 휴대폰번호
    ("연락처 010-1234-5678", "[PHONE]"),
    ("call 01012345678 now", "[PHONE]"),
    ("011-123-4567 도 가능", "[PHONE]"),
    # 이메일
    ("메일 test.user+tag@example.co.kr 보냄", "[EMAIL]"),
    ("a@b.io", "[EMAIL]"),
    # 카드번호
    ("카드 1234-5678-9012-3456", "[CARD]"),
    ("1234567890123456", "[CARD]"),
])
def test_masks_sensitive_values(text, token):
    masked = mask_text(text)
    assert token in masked
    # No 7+ digit run should survive (the raw sensitive value is gone).
    import re
    assert re.search(r"\d{7,}", masked) is None


def test_masks_email_value_removed():
    assert "test@example.com" not in mask_text("write to test@example.com")


def test_masks_multiple_patterns_in_one_text():
    text = "이름 홍길동 / 010-1234-5678 / test@example.com / 901010-1234567"
    masked = mask_text(text)
    assert "[PHONE]" in masked
    assert "[EMAIL]" in masked
    assert "[RRN]" in masked
    # name (오탐 위험으로 비대상) stays untouched
    assert "홍길동" in masked


def test_masks_applied_to_translated_english_text():
    assert mask_text("My card is 1234-5678-9012-3456") == "My card is [CARD]"


# --- negative cases: ordinary text must stay intact ---------------------------

@pytest.mark.parametrize("text", [
    "회의는 2024-01-15 입니다",        # date, not RRN/card
    "수량은 12345 개",                 # short number
    "주문번호 1234-5678",              # 8 digits, not a card
    "건물 02-123-4567 (유선)",         # landline, not mobile
    "총 123456789012 원",             # 12 digits, no pattern
    "버전 1.2.3 릴리스",
    "그냥 평범한 한국어 문장입니다.",
    "@mention 은 이메일이 아님",        # no domain → not email
])
def test_leaves_non_sensitive_text_unchanged(text):
    assert mask_text(text) == text


def test_none_passthrough():
    assert mask_text(None) is None


def test_empty_string():
    assert mask_text("") == ""


def test_non_string_coerced():
    # int -> str, still no PII
    assert mask_text(42) == "42"
