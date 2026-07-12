"""
test_write_refuses_unguarded.py — Finding #40 (CRITICAL leak).

`bubble_shield_write` restores REAL client PII (tokens → clear, from the local
vault) and writes the finished file to disk. If that file lands in a location a
subsequent agent built-in `Read`/`cat` is NOT blocked from — i.e. OUTSIDE any
protected folder, OR inside one but on the marker's `allow_paths` allow-list /
`allow_extensions` exemption — then the agent can read the real names straight
back into the Cowork session in clear. The PostToolUse re-anonymise scrub does
NOT run on built-in Read in Cowork (issue #32105), so the PreToolUse BLOCK is the
ONLY protection, and an allow-listed path disables it.

The fix: `bubble_shield_write` REFUSES to write a restored real-PII document to
any path that would NOT be guarded against a later agent Read. The invariant it
enforces: "the real document always lands where a subsequent agent built-in Read
is BLOCKED." The human still opens it (Finder / the local viewer) — the guard
governs the agent, not Mac apps.

These tests prove the invariant BY CONSTRUCTION:
  1. write into an allow-listed `clean/` subfolder of a protected folder → REFUSED
     (this is the exact Finding #40 leak — pre-fix it WROTE the file).
  2. write OUTSIDE any protected folder → REFUSED.
  3. write to a GUARDED path (protected folder, not allow-listed) → SUCCEEDS,
     restores real values, returns count+path, does NOT echo clear content.
  4. sanity: after a successful guarded write, driving guard.py on that written
     path DENIES a built-in Read → the invariant holds end-to-end.

All fixtures SYNTHETIC (Marc DURAND); pii-guard blocks real names.
"""
import json
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "plugin" / "bubble-shield" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import bubble_shield_mcp as mcp  # noqa: E402
import guard as guardmod  # noqa: E402


# ── synthetic identity ───────────────────────────────────────────────────────
SYN_NAME = "Marc DURAND"
TOKEN = "⟦NOM_0001⟧"
CONTENT = f"Cher {TOKEN}, votre dossier est prêt."


# ── helpers ──────────────────────────────────────────────────────────────────
def _protected_folder(tmp_path: Path) -> Path:
    """A folder with a marker that allow-lists `clean/` (mirrors marker.example.json)."""
    folder = tmp_path / "Clients" / "Dossier-Demo"
    folder.mkdir(parents=True)
    (folder / ".bubble-shield.json").write_text(
        json.dumps({"allow_paths": ["clean"], "allow_extensions": [".anon.txt"]}),
        encoding="utf-8",
    )
    (folder / "clean").mkdir()
    return folder


def _stub_deanonymise(monkeypatch):
    """Replace the vault-backed engine restore with a deterministic stub so the
    test needs no NER daemon / real vault. It restores the synthetic name and
    ACTUALLY writes the file (so we can prove pre-fix WROTE, post-fix does not)."""
    def _fake_to_file(path, content):
        restored = content.replace(TOKEN, SYN_NAME)
        out = Path(path).expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(restored, encoding="utf-8")
        return {"path": str(out), "tokens_restored": 1, "tokens_unresolved": 0,
                "bytes_written": len(restored.encode("utf-8"))}
    monkeypatch.setattr(mcp, "_deanonymise_to_file", _fake_to_file)


def _capture_write(monkeypatch, path: Path):
    """Drive the real tools/call handler for bubble_shield_write; return (text, result)."""
    captured = {}
    monkeypatch.setattr(mcp, "_send", lambda obj: captured.__setitem__("obj", obj))
    req = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "bubble_shield_write",
                   "arguments": {"path": str(path), "content": CONTENT}},
    }
    mcp._handle(req)
    obj = captured["obj"]
    result = obj.get("result", {})
    text = "".join(part.get("text", "") for part in result.get("content", []))
    return text, result


# ── 1. allow-listed clean/ subfolder → REFUSED (the Finding #40 leak) ─────────
def test_write_into_allowlisted_clean_is_refused(tmp_path, monkeypatch):
    _stub_deanonymise(monkeypatch)
    folder = _protected_folder(tmp_path)
    target = folder / "clean" / "resultat-demo.txt"

    text, result = _capture_write(monkeypatch, target)

    # REFUSED: flagged as error, no file written, real name NOT echoed.
    assert result.get("isError") is True, "write to an allow-listed path must be refused"
    assert not target.exists(), (
        "LEAK (Finding #40): the restored real-PII file was written into an "
        "allow-listed clean/ subfolder — an agent built-in Read there is NOT "
        "blocked, so the real names re-enter the session."
    )
    assert SYN_NAME not in text, "the real name must never appear in the returned text"


# ── 2. outside any protected folder → REFUSED ────────────────────────────────
def test_write_outside_protected_folder_is_refused(tmp_path, monkeypatch):
    _stub_deanonymise(monkeypatch)
    # A plain folder with NO marker anywhere above it.
    plain = tmp_path / "scratch"
    plain.mkdir()
    target = plain / "resultat-demo.txt"

    text, result = _capture_write(monkeypatch, target)

    assert result.get("isError") is True, "write outside a protected folder must be refused"
    assert not target.exists(), (
        "restored real-PII must not be written where the agent can read it back"
    )
    assert SYN_NAME not in text


# ── 3. guarded path (protected, not allow-listed) → SUCCEEDS ─────────────────
def test_write_to_guarded_path_succeeds(tmp_path, monkeypatch):
    _stub_deanonymise(monkeypatch)
    folder = _protected_folder(tmp_path)
    # Folder ROOT — inside the marker, NOT under clean/ → guarded.
    target = folder / "resultat-demo.txt"

    text, result = _capture_write(monkeypatch, target)

    assert result.get("isError") is not True, f"guarded write must succeed, got: {text!r}"
    assert target.exists(), "the restored file must be written to the guarded path"
    assert target.read_text(encoding="utf-8") == f"Cher {SYN_NAME}, votre dossier est prêt."
    # existing behaviour preserved: count + path surfaced, clear content NOT echoed.
    assert "restaurée" in text or "restauré" in text
    assert SYN_NAME not in text, "the tool must never echo the restored clear content"
    assert str(target) in text


# ── 3b. guarded path in a NON-allow-listed subfolder → SUCCEEDS ──────────────
def test_write_to_guarded_subfolder_succeeds(tmp_path, monkeypatch):
    _stub_deanonymise(monkeypatch)
    folder = _protected_folder(tmp_path)
    # `sorties/` is NOT on the allow-list → guarded.
    target = folder / "sorties" / "resultat-demo.txt"

    text, result = _capture_write(monkeypatch, target)

    assert result.get("isError") is not True, f"guarded subfolder write must succeed, got: {text!r}"
    assert target.exists()
    assert SYN_NAME not in text


# ── 3c. allow_extensions exemption is also refused (mirror allow_paths) ───────
def test_write_to_allow_extension_is_refused(tmp_path, monkeypatch):
    _stub_deanonymise(monkeypatch)
    folder = _protected_folder(tmp_path)
    # `.anon.txt` is ext-exempt in the marker → NOT guarded → refuse.
    target = folder / "resultat-demo.anon.txt"

    text, result = _capture_write(monkeypatch, target)

    assert result.get("isError") is True, "write to an ext-exempt path must be refused"
    assert not target.exists()
    assert SYN_NAME not in text


# ── 4. sanity: a successful guarded write is DENIED to a later agent Read ─────
def _guard_decision_for_read(path: Path, cwd: str) -> str:
    """Drive guard.py's PreToolUse decision for a built-in Read of `path`."""
    captured = {}
    import builtins
    orig_print = builtins.print

    def _fake_print(*a, **k):
        # guard._decide prints exactly one JSON object to stdout.
        if a and isinstance(a[0], str):
            try:
                captured["obj"] = json.loads(a[0])
            except Exception:
                pass

    event = {"tool_name": "Read", "tool_input": {"file_path": str(path)}, "cwd": cwd}
    builtins.print = _fake_print
    try:
        guardmod._main(json.dumps(event))
    except SystemExit:
        pass
    finally:
        builtins.print = orig_print
    obj = captured.get("obj", {})
    return obj.get("hookSpecificOutput", {}).get("permissionDecision", "allow")


def test_guarded_write_target_would_deny_a_later_read(tmp_path, monkeypatch):
    _stub_deanonymise(monkeypatch)
    folder = _protected_folder(tmp_path)
    target = folder / "resultat-demo.txt"

    _, result = _capture_write(monkeypatch, target)
    assert result.get("isError") is not True and target.exists()

    # The written file sits under a marker, not allow-listed → guard DENIES a Read.
    decision = _guard_decision_for_read(target, cwd=str(tmp_path))
    assert decision == "deny", (
        "invariant broken: a built-in Read of the restored file must be BLOCKED "
        f"by the guard (got decision={decision!r})"
    )

    # And the allow-listed clean/ path (had we written there) WOULD be readable —
    # this is exactly why we refuse to write there.
    clean_path = folder / "clean" / "resultat-demo.txt"
    assert _guard_decision_for_read(clean_path, cwd=str(tmp_path)) != "deny"
