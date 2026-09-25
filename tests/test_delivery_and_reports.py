from __future__ import annotations

from pathlib import Path

import pytest

from fieldnote.config import get_settings
from fieldnote.delivery.dispatch import active_channels, send_digest
from fieldnote.delivery.email import EmailError, build_message, send_email
from fieldnote.delivery.telegram import TelegramClient, TelegramError
from fieldnote.reports.common import Anonymizer, limit_prose, split_telegram, tg_escape, tg_link
from fieldnote.textutil import word_count


def test_dry_run_is_default_without_credentials(cfg_ev, isolated_env: Path) -> None:  # type: ignore[no-untyped-def]
    cfg_ev.delivery.channels = ["telegram", "email"]
    live, reasons = active_channels(cfg_ev, get_settings(), dry_run=False)
    assert live == [] and any("credentials" in r for r in reasons)
    res = send_digest(cfg_ev, get_settings(), "Subject", "Body text", dry_run=False)
    assert res[0]["status"] == "dry_run"
    outbox = list((isolated_env / "out" / cfg_ev.workspace / "outbox").glob("*.txt"))
    assert outbox and "nothing was sent" in outbox[0].read_text(encoding="utf-8")


def test_channels_require_config_and_credentials(cfg_ev, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    cfg_ev.delivery.channels = []
    assert active_channels(cfg_ev, get_settings(), dry_run=False)[0] == []  # credentials alone are not enough
    cfg_ev.delivery.channels = ["telegram"]
    assert active_channels(cfg_ev, get_settings(), dry_run=False)[0] == ["telegram"]
    assert active_channels(cfg_ev, get_settings(), dry_run=True)[0] == []  # --dry-run always wins


def test_telegram_and_email_refuse_without_config() -> None:
    with pytest.raises(TelegramError):
        TelegramClient(get_settings())
    with pytest.raises(EmailError):
        send_email(get_settings(), "s", "t")


def test_email_message_with_attachment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMTP_FROM", "bot@example.com")
    monkeypatch.setenv("SMTP_TO", "a@example.com, b@example.com")
    pdf = tmp_path / "memo.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    msg = build_message(get_settings(), "Subject", "Body", "<p>Body</p>", [pdf])
    assert msg["To"] == "a@example.com, b@example.com"
    assert [p.get_filename() for p in msg.iter_attachments()] == ["memo.pdf"]


def test_telegram_escaping_and_splitting() -> None:
    assert tg_escape("a_b*c[1](x).!") == "a\\_b\\*c\\[1\\]\\(x\\)\\.\\!"
    assert tg_link("[F1]", "https://x.example/a)b") == "[\\[F1\\]](https://x.example/a\\)b)"
    lines = [tg_escape(f"Line {i}: " + "word " * 30) for i in range(40)]
    chunks = split_telegram(lines, 600)
    assert all(len(c) <= 600 for c in chunks)
    assert sum(c.count("Line") for c in chunks) == 40
    long_line = tg_escape("x." * 800)
    parts = split_telegram([long_line], 500)
    assert all(len(p) <= 500 and not p.endswith("\\") for p in parts[:-1])


def test_limit_prose_respects_budget_and_sentences() -> None:
    fields = {
        "what_happened": "First fact sentence here. Second fact sentence here. Third one.",
        "why_it_matters": "Because it matters a lot. And more.",
        "recommended_action": "Do the thing now.",
        "how_to_execute": "Step one by the lead. Step two by finance within 2 weeks. Step three later.",
    }
    out = limit_prose(fields, 22)
    assert sum(word_count(v) for v in out.values()) <= 30
    assert out["recommended_action"] == "Do the thing now."
    assert limit_prose(fields, 500) == fields


def test_anonymizer(cfg_ev) -> None:  # type: ignore[no-untyped-def]
    cfg_ev.output.anonymize_entities = True
    anon = Anonymizer(cfg_ev, {"Voltra": 34.0, "Zipp": 27.0})
    assert anon("Voltra Mobility cut prices; Zipp grew.") == "Market Leader cut prices; Challenger A grew."
    cfg_ev.output.anonymize_entities = False
    assert Anonymizer(cfg_ev)("Voltra") == "Voltra"
