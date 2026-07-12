"""
test_error_path_no_pii_leak.py — SAFETY BLOCKER (PR #26): the generic anonymise
error handler must NOT leak raw content into the model's context.

The MCP tools (bubble_shield_read / bubble_shield_mail_read /
bubble_shield_anonymize_text) route ALL exceptions through one generic
`except Exception` handler in _handle(). The bug: that handler interpolated
`str(e)` into the RETURNED tool text. A parser/lib error commonly quotes the raw
input it choked on — so if the anonymizer raises an exception carrying the mail
body / file text (sender, name, IBAN, phone, subject), that raw PII escapes into
the model's context through the supposedly fail-CLOSED error path.

These tests prove the invariant BY CONSTRUCTION: we force each anonymise function
to raise an exception whose message EMBEDS PII, drive the real _handle() tool
call, capture the returned tool text, and assert it contains NONE of the PII
tokens and IS the fixed French message.

All fixtures SYNTHETIC. mutation-check: revert the fix (put `{e}` back) → these
tests FAIL.
"""
import json
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "plugin" / "bubble-shield" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import bubble_shield_mcp as mcp  # noqa: E402

# ── synthetic PII that a parser/lib error might quote back verbatim ──────────
PII_IBAN = "FR7630006000011234567890189"
PII_NAME = "DUPONT Jean-Marc"
PII_EMAIL = "jean.marc.dupont@example-client.fr"
PII_PHONE = "+33612345678"
PII_SUBJECT = "Virement urgent dossier succession"
PII_TOKENS = [PII_IBAN, PII_NAME, PII_EMAIL, PII_PHONE, PII_SUBJECT]

# A raw block exactly as _anonymise_mail composes it (From / Subject / body),
# stuffed into the exception message — the worst-case leak.
RAW_LEAK = (
    f"From: {PII_NAME} <{PII_EMAIL}>\nSubject: {PII_SUBJECT}\n\n"
    f"Merci de virer les fonds sur {PII_IBAN}, joignable au {PII_PHONE}."
)


def _capture_handle(monkeypatch, name, arguments):
    """Drive the real tools/call handler and return the emitted tool-result text.

    _handle() writes its JSON-RPC response via mcp._send(...). We capture that
    payload instead of stdout so the assertion sees exactly what reaches the model.
    """
    captured = {}

    def _fake_send(obj):
        captured["obj"] = obj

    monkeypatch.setattr(mcp, "_send", _fake_send)

    req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    mcp._handle(req)

    obj = captured["obj"]
    result = obj.get("result", {})
    text = "".join(part.get("text", "") for part in result.get("content", []))
    return text, result


def _assert_no_pii(text, result):
    # 1) NONE of the PII tokens may appear in the returned tool text.
    for tok in PII_TOKENS:
        assert tok not in text, (
            f"PII LEAK: '{tok}' escaped into the returned tool text via the "
            f"error path. Returned text was: {text!r}"
        )
    # 2) It must be flagged as an error (fail-closed shape preserved).
    assert result.get("isError") is True, "error path must set isError:true"
    # 3) It must be the FIXED security message (no variable content).
    assert "Le contenu brut n'est PAS renvoyé" in text


def test_mail_read_error_does_not_leak_pii(monkeypatch):
    """bubble_shield_mail_read: anonymizer raises w/ raw mail in the message.

    Mail path is gated behind BUBBLE_SHIELD_ENABLE_MAIL (off by default in the
    shipped product — see test_mail_disabled.py); enable it here to exercise
    the generic exception-handler leak path this test targets.
    """
    monkeypatch.setenv("BUBBLE_SHIELD_ENABLE_MAIL", "1")

    def _boom(**kwargs):
        raise ValueError(f"boom parsing mail: {RAW_LEAK}")

    monkeypatch.setattr(mcp, "_anonymise_mail", _boom)
    text, result = _capture_handle(
        monkeypatch, "bubble_shield_mail_read", {"query": "ALL", "max": 5}
    )
    _assert_no_pii(text, result)
    assert "Échec de l'anonymisation" in text


def test_file_read_error_does_not_leak_pii(monkeypatch):
    """bubble_shield_read (file path): extractor raises w/ raw file text."""
    def _boom(path):
        raise ValueError(f"boom extracting {path}: {RAW_LEAK}")

    monkeypatch.setattr(mcp, "_anonymise_file", _boom)
    text, result = _capture_handle(
        monkeypatch, "bubble_shield_read", {"path": "/tmp/secret.pdf"}
    )
    _assert_no_pii(text, result)
    assert "Échec de l'anonymisation" in text


def test_anonymize_text_error_does_not_leak_pii(monkeypatch):
    """bubble_shield_anonymize_text: engine raises w/ raw input text."""
    def _boom(text, **kwargs):
        raise ValueError(f"boom anonymising: {RAW_LEAK}")

    monkeypatch.setattr(mcp, "_anonymise_text", _boom)
    text, result = _capture_handle(
        monkeypatch, "bubble_shield_anonymize_text", {"text": RAW_LEAK}
    )
    _assert_no_pii(text, result)
    assert "Échec de l'anonymisation" in text


def test_write_error_does_not_leak_pii(monkeypatch, tmp_path):
    """bubble_shield_write: de-anonymise/write raises w/ raw content in message.

    NB (Finding #40): the write now REFUSES an unguarded path BEFORE calling
    _deanonymise_to_file, so to reach the restore-error path we target a GUARDED
    location — a folder carrying a .bubble-shield.json marker, not allow-listed.
    """
    def _boom(path, content):
        raise ValueError(f"boom writing {path}: {RAW_LEAK}")

    # A guarded folder so the write proceeds to the (monkeypatched) restore.
    guarded = tmp_path / "Dossier"
    guarded.mkdir()
    (guarded / ".bubble-shield.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(mcp, "_deanonymise_to_file", _boom)
    text, result = _capture_handle(
        monkeypatch, "bubble_shield_write",
        {"path": str(guarded / "out.docx"), "content": "hello ⟦NOM_1⟧"},
    )
    for tok in PII_TOKENS:
        assert tok not in text, f"PII LEAK via write error path: {tok!r} in {text!r}"
    assert result.get("isError") is True
    assert "Aucun fichier n'a été produit" in text
    assert "Le contenu brut n'est PAS renvoyé" in text


def test_error_detail_is_logged_to_stderr(monkeypatch, capsys):
    """The exception detail is still available host-side — on STDERR only."""
    monkeypatch.setenv("BUBBLE_SHIELD_ENABLE_MAIL", "1")

    def _boom(**kwargs):
        raise ValueError(f"boom: {RAW_LEAK}")

    monkeypatch.setattr(mcp, "_anonymise_mail", _boom)
    text, _ = _capture_handle(
        monkeypatch, "bubble_shield_mail_read", {"query": "ALL"}
    )
    captured = capsys.readouterr()
    # Host-side operator keeps the detail for debugging…
    assert "bubble_shield" in captured.err
    # …but it is NEVER in the agent-facing return text.
    for tok in PII_TOKENS:
        assert tok not in text
