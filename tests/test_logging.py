from __future__ import annotations

import logging

import pytest

from fieldnote.logging_setup import RedactingFilter, redact, run_id_var


def test_redact_env_secrets_and_key_shapes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
    text = (
        "calling https://api.telegram.org/bot123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi/sendMessage "
        "with key sk-ant-api03-abcdefghijklmnop and ?api_key=zzz999 and AIzaSyA1234567890abcdefghijklmnopqrstu"
    )
    out = redact(text)
    assert "ABCDEFGHIJKLMNOP" not in out
    assert "sk-ant-api03" not in out
    assert "zzz999" not in out
    assert "AIzaSyA123" not in out
    assert "<TELEGRAM_BOT_TOKEN:redacted>" in out


def test_log_records_are_redacted_and_carry_run_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret-value-123456")
    token = run_id_var.set("42")
    try:
        record = logging.LogRecord(
            "fieldnote.t", logging.INFO, __file__, 1, "key=%s", ("sk-ant-secret-value-123456",), None
        )
        assert RedactingFilter().filter(record)
        assert "secret-value" not in record.getMessage()
        assert record.run_id == "42"  # type: ignore[attr-defined]
    finally:
        run_id_var.reset(token)
