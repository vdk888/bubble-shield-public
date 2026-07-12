"""
test_add_known_pii.py -- MCP tool `bubble_shield_add_known_pii`.

Closes the "client-flagged miss" loop: the detector missed a name (e.g. it
appeared in clear), the client says "you forgot X", the Cowork agent calls this
tool, and X is DETERMINISTICALLY masked in every subsequent document via the
known-PII gazetteer.

All values here are SYNTHETIC surnames invented for the test -- "Delmarre",
"Fauconnier", "Vasseraude". They are NOT real client names and deliberately are
NOT any of the real values the engine has seen.

TEST ISOLATION (safety-critical, #382 regression):
  Every test drives the REAL tools/call handler, which persists to
  $BUBBLE_SHIELD_HOME/gazetteer/known_pii.json. The autouse fixture below points
  BUBBLE_SHIELD_HOME at a per-test tmp dir, so the production ~/.bubble_shield
  gazetteer is NEVER written. `test_real_store_untouched` additionally proves the
  real store's bytes are unchanged across a real add.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "plugin" / "bubble-shield" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import bubble_shield_mcp as mcp  # noqa: E402
from bubble_shield.known_pii_store import is_known_pii, load_gazetteer  # noqa: E402


# -- isolation: every test writes to a tmp BUBBLE_SHIELD_HOME, never ~/.bubble_shield --

@pytest.fixture(autouse=True)
def _isolate_shield_home(tmp_path, monkeypatch):
    home = tmp_path / "shield_home"
    home.mkdir()
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(home))
    # The store resolves BUBBLE_SHIELD_HOME at CALL TIME (_shield_home()), so
    # setting the env is sufficient -- no path= plumbing needed.
    return home


def _gaz_path(home: Path) -> Path:
    return home / "gazetteer" / "known_pii.json"


def _call(monkeypatch, arguments: dict):
    """Drive the real tools/call handler for bubble_shield_add_known_pii."""
    captured = {}
    monkeypatch.setattr(mcp, "_send", lambda obj: captured.__setitem__("obj", obj))
    req = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "bubble_shield_add_known_pii", "arguments": arguments},
    }
    mcp._handle(req)
    obj = captured["obj"]
    result = obj.get("result", {})
    text = "".join(part.get("text", "") for part in result.get("content", []))
    return text, result


# -- 1. happy path -- client-flagged miss is added and now known --

def test_add_known_pii_masks_thereafter(_isolate_shield_home, monkeypatch):
    home = _isolate_shield_home
    text, result = _call(monkeypatch, {"value": "Delmarre", "confirm": True})

    assert result.get("isError") is not True, f"add should succeed, got: {text}"
    assert "Delmarre" in text  # client gave this value; echoing it back is fine
    assert is_known_pii("Delmarre", path=_gaz_path(home)), (
        "the flagged value must now be a known PII entry"
    )

    from bubble_shield.known_pii_recognizer import make_known_pii_recognizer
    from bubble_shield.engine import AnonymizationEngine
    from bubble_shield.vault import Vault

    rec = make_known_pii_recognizer(path=_gaz_path(home))
    assert rec is not None
    engine = AnonymizationEngine(vault=Vault(mission="t-add-known"), extra_recognizers=[rec])
    out = engine.anonymize("Dossier Delmarre - a valider.")
    assert "Delmarre" not in out.anonymized, "flagged miss must be masked in the next doc"


# -- 2. confirm not true -> REFUSED, nothing added --

def test_missing_confirm_is_refused(_isolate_shield_home, monkeypatch):
    home = _isolate_shield_home
    text, result = _call(monkeypatch, {"value": "Fauconnier"})  # confirm omitted

    assert result.get("isError") is True, "must refuse without confirm=true"
    assert not is_known_pii("Fauconnier", path=_gaz_path(home)), "nothing may be added"
    assert not _gaz_path(home).exists(), "no gazetteer file should be created on refusal"


def test_confirm_false_is_refused(_isolate_shield_home, monkeypatch):
    home = _isolate_shield_home
    text, result = _call(monkeypatch, {"value": "Fauconnier", "confirm": False})

    assert result.get("isError") is True
    assert not is_known_pii("Fauconnier", path=_gaz_path(home))


# -- 3. idempotent -- second add reports "already present", one entry --

def test_idempotent_second_add(_isolate_shield_home, monkeypatch):
    home = _isolate_shield_home
    text1, r1 = _call(monkeypatch, {"value": "Vasseraude", "confirm": True})
    text2, r2 = _call(monkeypatch, {"value": "Vasseraude", "confirm": True})

    assert r1.get("isError") is not True and r2.get("isError") is not True
    import unicodedata
    def _fold(s: str) -> str:
        return "".join(c for c in unicodedata.normalize("NFD", s.lower())
                       if unicodedata.category(c) != "Mn")
    assert "deja" in _fold(text2) or "already" in text2.lower(), (
        f"second add should report already-present, got: {text2}"
    )
    g = load_gazetteer(path=_gaz_path(home))
    assert len([e for e in g.entries if e.value.lower() == "vasseraude"]) == 1


# -- 4. pattern-looking value -> rejected (steer to add_field) --

@pytest.mark.parametrize("bad", [r"\d{5}", "[A-Z]+", "FR{2}\\d", "a{3,5}"])
def test_pattern_value_rejected(_isolate_shield_home, monkeypatch, bad):
    home = _isolate_shield_home
    text, result = _call(monkeypatch, {"value": bad, "confirm": True})

    assert result.get("isError") is True, f"pattern-looking value {bad!r} must be rejected"
    assert "add_field" in text, "the steer must point at add_field for patterns"
    assert not _gaz_path(home).exists() or load_gazetteer(path=_gaz_path(home)).is_empty


def test_empty_value_rejected(_isolate_shield_home, monkeypatch):
    home = _isolate_shield_home
    text, result = _call(monkeypatch, {"value": "   ", "confirm": True})
    assert result.get("isError") is True
    assert not _gaz_path(home).exists() or load_gazetteer(path=_gaz_path(home)).is_empty


# -- 5. default entity_type is NOM; explicit type honoured --

def test_default_entity_type_is_nom(_isolate_shield_home, monkeypatch):
    home = _isolate_shield_home
    _call(monkeypatch, {"value": "Delmarre", "confirm": True})
    g = load_gazetteer(path=_gaz_path(home))
    entry = next(e for e in g.entries if e.value == "Delmarre")
    assert entry.entity_type == "NOM"


def test_explicit_entity_type_honoured(_isolate_shield_home, monkeypatch):
    home = _isolate_shield_home
    _call(monkeypatch, {"value": "12 rue Vasseraude", "confirm": True, "entity_type": "ADRESSE"})
    g = load_gazetteer(path=_gaz_path(home))
    entry = next(e for e in g.entries if e.value == "12 rue Vasseraude")
    assert entry.entity_type == "ADRESSE"


# -- 6. TEST ISOLATION PROOF -- the real ~/.bubble_shield is byte-untouched --

def test_real_store_untouched(_isolate_shield_home, monkeypatch):
    """A real add through the handler must land in the tmp home, and the real
    production gazetteer must be byte-for-byte unchanged (or still absent)."""
    real = Path.home() / ".bubble_shield" / "gazetteer" / "known_pii.json"
    before = real.read_bytes() if real.exists() else None
    before_exists = real.exists()

    text, result = _call(monkeypatch, {"value": "Delmarre", "confirm": True})
    assert result.get("isError") is not True

    assert is_known_pii("Delmarre", path=_gaz_path(_isolate_shield_home))

    after = real.read_bytes() if real.exists() else None
    assert real.exists() == before_exists, "handler must not create the real gazetteer"
    assert after == before, "the real production gazetteer must be byte-unchanged"
    if real.exists():
        assert "Delmarre" not in real.read_text(encoding="utf-8")
