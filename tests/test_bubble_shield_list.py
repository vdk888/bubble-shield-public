"""test_bubble_shield_list.py — folder-listing discovery capability.

Two changes are under test:

CHANGE 1 (guard.py) — split Glob (names-only) from Grep (content):
  - Glob on a protected folder → ALLOWED (a listing, no file CONTENT).
  - Grep on the same folder     → still DENIED (returns matching LINES = content).
  - Read / `cat` (Bash)         → still DENIED (content).
  The guard is driven over its real stdin/stdout hook contract (subprocess),
  exactly like tests/../scripts/test_guard.py.

CHANGE 2 (bubble_shield_mcp.py) — bubble_shield_list(folder):
  - entry NAMES come back IN CLEAR (unmasked) — a folder/file name is a
    navigation label the user already owns and sees on their own machine, so
    they must be able to navigate/reference folders and files BY NAME. Lists
    ext + size for files, and returns NO file CONTENT;
  - NON-recursive (subfolder contents are NOT enumerated);
  - names are unmasked REGARDLESS of NER daemon state (no masking happens here
    at all, so nothing depends on the daemon) — content masking (bubble_shield_read)
    keeps its own separate fail-closed contract, untouched by this change.

ALL fixtures SYNTHETIC. No real client name is used anywhere (pii-guard blocks
real names on commit). "Marc DURAND" below is an invented placeholder.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _ROOT / "plugin" / "bubble-shield" / "scripts"
GUARD = _SCRIPTS / "guard.py"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import bubble_shield_mcp as mcp  # noqa: E402

# Make the vendored engine (bubble_shield.recognizers.Match, posttool_anonymize)
# importable for the daemon-up stub below.
_VENDOR = mcp._vendor()
if str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))
import posttool_anonymize  # noqa: E402,F401  (imported so monkeypatch targets it)


# ---------------------------------------------------------------------------
# CHANGE 1 — guard.py: Glob allowed, Grep/Read/cat still denied.
# ---------------------------------------------------------------------------

def _run_guard(event: dict, protected: str) -> str:
    """Drive guard.py via its stdin/stdout hook contract; return the decision
    ("deny" | "allow" | "allow-noop")."""
    with tempfile.TemporaryDirectory() as td:
        cfgp = Path(td) / "bubble-shield.json"
        cfgp.write_text(json.dumps({"protected_folders": [protected], "block_bash": True}))
        env = dict(os.environ,
                   BUBBLE_SHIELD_GUARD_CONFIG=str(cfgp),
                   CLAUDE_PROJECT_DIR=td, HOME=td)
        p = subprocess.run([sys.executable, str(GUARD)],
                           input=json.dumps(event), capture_output=True, text=True, env=env)
    out = p.stdout.strip()
    if not out:
        return "allow-noop"
    try:
        return json.loads(out)["hookSpecificOutput"]["permissionDecision"]
    except Exception:
        return f"PARSE_ERROR:{out}"


PROT = "/tmp/bubble-shield-list-test-clients"


def test_glob_on_protected_folder_is_allowed():
    """Glob returns NAMES only → sanctioned discovery path → ALLOWED."""
    ev = {"tool_name": "Glob", "tool_input": {"path": PROT}, "cwd": "/tmp"}
    assert _run_guard(ev, PROT) == "allow-noop"


def test_glob_with_pattern_into_protected_folder_is_allowed():
    ev = {"tool_name": "Glob",
          "tool_input": {"path": f"{PROT}/dossier-x", "pattern": "*.pdf"}, "cwd": "/tmp"}
    assert _run_guard(ev, PROT) == "allow-noop"


def test_grep_on_protected_folder_still_denied():
    """Grep returns matching LINES (file CONTENT) → must stay DENIED."""
    ev = {"tool_name": "Grep", "tool_input": {"path": f"{PROT}/dossier-x"}, "cwd": "/tmp"}
    assert _run_guard(ev, PROT) == "deny"


def test_read_on_protected_folder_still_denied():
    ev = {"tool_name": "Read", "tool_input": {"file_path": f"{PROT}/der.pdf"}, "cwd": "/tmp"}
    assert _run_guard(ev, PROT) == "deny"


def test_bash_cat_on_protected_folder_still_denied():
    """The Bash branch is untouched — `cat` into a protected folder still denies."""
    ev = {"tool_name": "Bash", "tool_input": {"command": f"cat {PROT}/der.pdf"}, "cwd": "/tmp"}
    assert _run_guard(ev, PROT) == "deny"


# ---------------------------------------------------------------------------
# CHANGE 2 — bubble_shield_list: masking, non-recursion, no content, fail-closed.
# ---------------------------------------------------------------------------

# A SYNTHETIC PII-bearing filename. "Marc DURAND" is invented, not a real client.
PII_FILENAME = "DCC - Marc DURAND - 2026.pdf"
# A SYNTHETIC subfolder name a user would navigate by. "DUPONT" is invented.
SUBSCRIPTION_SUBFOLDER = "Souscription DUPONT"


def _make_protected_folder(tmp_path: Path) -> Path:
    """Create a folder marked protected (in-folder .bubble-shield.json marker),
    with a PII-named PDF, a benign file, and a subfolder holding a secret file
    (to prove non-recursion)."""
    folder = tmp_path / "clients"
    folder.mkdir()
    (folder / ".bubble-shield.json").write_text("{}", encoding="utf-8")
    (folder / PII_FILENAME).write_text("body of the pdf", encoding="utf-8")
    (folder / "notes.txt").write_text("hello", encoding="utf-8")
    sub = folder / "sub"
    sub.mkdir()
    (sub / "SECRET-Sophie-MARTIN-2025.pdf").write_text("nested body", encoding="utf-8")
    return folder


def _daemon_up(monkeypatch):
    """Force the NER daemon UP with a stub GLiNER that flags an all-caps surname
    as PERSON, so a masked name yields a ⟦…⟧ token deterministically (no network)."""
    import re
    from bubble_shield.recognizers import Match  # vendored Match type

    def _fake_detector(text):
        def _detect(t):
            out = []
            for m in re.finditer(r"\bDURAND\b", t):
                out.append(Match(start=m.start(), end=m.end(),
                                 entity_type="PERSON", value=m.group(),
                                 score=0.95, priority=5))
            return out
        return _detect

    import posttool_anonymize as pt
    monkeypatch.setattr(pt, "_daemon_detector", _fake_detector)


def _daemon_down(monkeypatch):
    """Force the NER daemon DOWN (detector returns None → regex-only → fail-closed
    for _anonymise_text). Also stub the re-arm spawn so nothing is launched."""
    import posttool_anonymize as pt
    monkeypatch.setattr(pt, "_daemon_detector", lambda text: None)
    monkeypatch.setattr(mcp, "_try_spawn_daemon_from_mcp", lambda: None)


def test_list_returns_pii_filename_in_clear_and_reports_modality(monkeypatch, tmp_path):
    """A PII-named file → NAME returned IN CLEAR (navigation label the user
    already owns), ext + modality + size present, and NO file CONTENT
    ('body of the pdf') anywhere in the listing."""
    _daemon_up(monkeypatch)
    folder = _make_protected_folder(tmp_path)

    out = mcp._list_folder(str(folder))
    data = json.loads(out)

    assert data["protected"] is True
    assert data["recursive"] is False

    # locate the PDF entry (the only .pdf at this level)
    pdfs = [e for e in data["entries"] if e.get("ext") == ".pdf"]
    assert len(pdfs) == 1, f"expected one pdf entry, got {pdfs}"
    pdf = pdfs[0]

    # NAME in clear: exact match, no masking token, nothing tokenised.
    assert pdf["name"] == PII_FILENAME, f"name should be unmasked/clear: {pdf['name']!r}"
    assert "⟦" not in pdf["name"], f"listing must not mask names: {pdf['name']!r}"

    # modality + size present; no content.
    assert pdf["modality"] == "pdf"
    assert isinstance(pdf["size"], int) and pdf["size"] > 0
    assert "body of the pdf" not in out, "file CONTENT must never be returned"


def test_list_subscription_subfolder_name_in_clear(monkeypatch, tmp_path):
    """A protected folder containing a subfolder named 'Souscription DUPONT'
    (synthetic) lists it with the name IN CLEAR, type dir — the user must be
    able to navigate to it by name."""
    _daemon_up(monkeypatch)
    folder = _make_protected_folder(tmp_path)
    (folder / SUBSCRIPTION_SUBFOLDER).mkdir()

    out = mcp._list_folder(str(folder))
    data = json.loads(out)

    matches = [e for e in data["entries"] if e["name"] == SUBSCRIPTION_SUBFOLDER]
    assert len(matches) == 1, f"expected subfolder listed in clear, got entries: {data['entries']}"
    assert matches[0]["type"] == "dir"
    assert "⟦" not in matches[0]["name"]


def test_list_is_non_recursive(monkeypatch, tmp_path):
    """The subfolder is listed as a dir entry, but its CONTENTS are NOT enumerated."""
    _daemon_up(monkeypatch)
    folder = _make_protected_folder(tmp_path)

    out = mcp._list_folder(str(folder))
    data = json.loads(out)

    dirs = [e for e in data["entries"] if e["type"] == "dir"]
    assert any(d for d in dirs), "the subfolder should appear as a dir entry"
    # the nested file's basename must NOT appear — non-recursive.
    assert "SECRET" not in out and "nested body" not in out


def test_list_names_in_clear_even_when_ner_down(monkeypatch, tmp_path):
    """Listing NAMES no longer depend on the NER daemon at all — this tool never
    masks names, so it works identically whether the daemon is up or down. Only
    file CONTENT (bubble_shield_read) has a fail-closed NER dependency, and this
    change does not touch that path."""
    _daemon_down(monkeypatch)
    folder = _make_protected_folder(tmp_path)

    # 1) the core returns the listing normally — no NERDownError raised.
    out = mcp._list_folder(str(folder))
    data = json.loads(out)
    pdfs = [e for e in data["entries"] if e.get("ext") == ".pdf"]
    assert len(pdfs) == 1
    assert pdfs[0]["name"] == PII_FILENAME
    assert "body of the pdf" not in out, "file CONTENT must never be returned"

    # 2) driven through the real handler → ok (not isError), clear names present.
    captured = {}
    monkeypatch.setattr(mcp, "_send", lambda obj: captured.__setitem__("obj", obj))
    mcp._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": {"name": "bubble_shield_list", "arguments": {"folder": str(folder)}}})
    result = captured["obj"]["result"]
    text = "".join(p.get("text", "") for p in result.get("content", []))
    assert not result.get("isError"), f"listing should not fail closed on NER-down: {text!r}"
    assert PII_FILENAME in text
    assert "body of the pdf" not in text


def test_list_non_protected_folder_returns_plain_listing(monkeypatch, tmp_path):
    """Pointed at a NON-protected folder → never crashes; returns a plain
    (unmasked) listing with protected=false. Even with NER down it must work
    (no masking needed, so no daemon dependency)."""
    _daemon_down(monkeypatch)
    plain = tmp_path / "public"
    plain.mkdir()
    (plain / "readme.txt").write_text("hi", encoding="utf-8")

    out = mcp._list_folder(str(plain))
    data = json.loads(out)
    assert data["protected"] is False
    assert data["count"] == 1
    assert data["entries"][0]["name"] == "readme.txt"


def test_list_missing_folder_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        mcp._list_folder(str(tmp_path / "does-not-exist"))
