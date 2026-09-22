"""DEEPL_CONTEXT_SENTENCES: DeepL context window size, 0 turns it off.

An env knob rather than a constant so prod can switch the feature off (or A/B
it service by service) with an .env.prod edit and a restart, no revert.
"""
import pytest

from src.config import get_deepl_context_sentences


def test_unset_defaults_to_eight(monkeypatch):
    monkeypatch.delenv('DEEPL_CONTEXT_SENTENCES', raising=False)
    assert get_deepl_context_sentences() == 8


def test_empty_defaults_to_eight(monkeypatch):
    monkeypatch.setenv('DEEPL_CONTEXT_SENTENCES', '')
    assert get_deepl_context_sentences() == 8


def test_zero_is_accepted_as_off(monkeypatch):
    monkeypatch.setenv('DEEPL_CONTEXT_SENTENCES', '0')
    assert get_deepl_context_sentences() == 0


@pytest.mark.parametrize('raw', ['eight', '-1', '2.5'])
def test_invalid_value_fails_startup(monkeypatch, raw):
    monkeypatch.setenv('DEEPL_CONTEXT_SENTENCES', raw)
    with pytest.raises(RuntimeError):
        get_deepl_context_sentences()
