"""
test_589_zero_detection_failclosed.py — P0 SECURITY FIX (#589): fail-CLOSED
on zero-detection for a SUBSTANTIAL document.

ROOT CAUSE (live-confirmed): `_anonymise_text` in bubble_shield_mcp.py only
failed closed when the NER daemon was DOWN (`NERDownError`). But when the
engine ran successfully and found ZERO detections on a substantial document
(`res.verdict_state == "zero_detection"`), it still returned `res.anonymized`
— which, on a zero-detection result, IS THE RAW INPUT TEXT — plus a soft
"please review" note appended. The note is not containment: the raw PII is
already in the model's context by the time the note is read. This leaked a
real client's raw PDF (43KB, 4 raw phone numbers, zero tokens) in a live
session while the NER daemon was UP and healthy.

THE FIX: after `res = engine.anonymize(text)`, if `res.verdict_state ==
"zero_detection"` (which, per engine.py's `substantial_text` property, ONLY
fires for a document with >=8 words AND >=40 chars of prose — the "nothing to
worry about" trivial/empty case is the DISTINCT `nothing_to_do` state and is
never gated here), _anonymise_text raises `ZeroDetectionError` instead of
returning `res.anonymized`. The tools/call handler in _handle() converts this
to isError:true with a FIXED French message and NO body — the raw text never
reaches the returned tool content.

Coverage (mirrors the brief's 4 required scenarios + a mirror-copy check):
  1. Substantial doc, zero detections → FAILS CLOSED, raw text NOT returned.
  2. Normal doc with detectable PII → still masked (regression, tokens present).
  3. Tiny/empty input (nothing_to_do) → NOT refused (regression, no over-block).
  4. Daemon-down → still raises NERDownError, unchanged.
  5. Both plugin + mcpb mirror copies raise identically (byte-identical files
     already covered by test_mirror_copies_identical.py; this just exercises
     the mcpb copy directly too, belt-and-suspenders).

Synthetic data only — no real client content anywhere in this file.
"""
from __future__ import annotations

import importlib.util as _ilu
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bubble_shield import policy as P  # noqa: E402
from bubble_shield.engine import AnonymizationEngine  # noqa: E402
from bubble_shield.vault import Vault  # noqa: E402


def _make_engine():
    """A real AnonymizationEngine, default (cloak-everything) policy."""
    return AnonymizationEngine(
        vault=Vault(mission="test-589"),
        match_filter=P.make_match_filter(P.default_policy()),
    )


def _import_mcp(scripts_subpath=("plugin", "bubble-shield", "scripts")):
    """Import bubble_shield_mcp from either the plugin or the mcpb mirror copy,
    without running it as __main__, and without polluting sys.modules across
    the two variants (each call gets an independent module object so the
    plugin-copy test and the mcpb-copy test don't silently share state)."""
    mcp_path = ROOT.joinpath(*scripts_subpath) / "bubble_shield_mcp.py"

    # Stub posttool_anonymize so the module import never depends on a live
    # daemon process being reachable in CI.
    if "posttool_anonymize" not in sys.modules:
        _scripts = str(ROOT.joinpath(*scripts_subpath))
        try:
            if _scripts not in sys.path:
                sys.path.insert(0, _scripts)
            import posttool_anonymize  # noqa: F401
        except Exception:
            fake_pt = types.ModuleType("posttool_anonymize")
            fake_pt._daemon_detector = lambda *a, **kw: None
            fake_pt._try_spawn_daemon = lambda: None
            fake_pt.NERD_URL = "http://127.0.0.1:0"
            fake_pt._daemon_up = lambda: False
            sys.modules["posttool_anonymize"] = fake_pt

    mod_name = "bubble_shield_mcp_589_" + "_".join(scripts_subpath)
    spec = _ilu.spec_from_file_location(mod_name, str(mcp_path))
    mod = _ilu.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


# ── synthetic fixtures ───────────────────────────────────────────────────────

# UPDATE (P0 #589 quality-gate refinement, see test_589_quality_gate.py):
# this file's ORIGINAL fixture here was clean, substantial prose — which
# tripped zero_detection and was, at the time this file was written, ALWAYS
# hard-refused. The refined behaviour (Joris, live-validated 2026-07-07) is a
# clean/garbled SPLIT: clean prose with zero detections is the honest "no PII
# here" case and must RETURN (see test_589_quality_gate.py's
# TestCleanZeroDetectionReturns for that coverage in detail); only GARBLED/
# low-quality zero-detection text still hard fail-closes. So this file's
# ZERO_DETECTION_DOC fixture — which exists specifically to exercise the
# fail-closed path below — is now a GARBLED fixture (still trips
# zero_detection, but also fails the text-quality gate), so the "still fails
# closed" assertions in this file continue to test a true fail-closed case
# under the refined logic. The clean-prose case moved to
# test_589_quality_gate.py where it belongs (its own dedicated coverage).
ZERO_DETECTION_DOC = (
    "l3 d0 m3n xz p0r t5 io qw3 rt ui0 as gh zx bn qw rt "
    "1z 3x y5 q p0 u7 t6 e5 q1 z2 c3 b4 m5 l k j h g f d s a "
    "0987 !@#$ %^&* () qa ed tg uj ol p0 m, n. b/ v; c: x! z? "
    "9a1 8s2 7d3 6f4 5g5 4h6 3j7 2k8 1l9 q0w e9r t8y u7i o6p"
)

# A normal doc carrying clearly-detectable PII (email + IBAN-shaped string) —
# must still be masked normally; this is the non-regression case.
NORMAL_PII_DOC = (
    "Merci de contacter synthetic.contact@example-test.fr pour toute question. "
    "Le compte de reference est FR7630006000011234567890189 pour le virement."
)

# Tiny/empty inputs → nothing_to_do, must NOT be refused.
TINY_INPUTS = ["", "   ", "ok", "31/12/2024"]


class TestZeroDetectionFailsClosed:
    """Core P0 fix: a substantial zero-detection doc must never come back
    with its raw text in the tool result."""

    def _run(self, mcp, text):
        engine = _make_engine()
        vpath = Path("/tmp/test-589-vault.json")
        with pytest.MonkeyPatch.context() as m:
            m.setattr(mcp, "_engine", lambda *a, **kw: (engine, vpath, True))
            return mcp._anonymise_text(text)

    def test_substantial_zero_detection_raises_and_hides_raw_text(self):
        mcp = _import_mcp()
        # Sanity: confirm our fixture really is zero_detection before asserting
        # the fail-closed behaviour (so this test can't pass for the wrong
        # reason if the fixture drifts under a recognizer change).
        probe_engine = _make_engine()
        probe_res = probe_engine.anonymize(ZERO_DETECTION_DOC)
        assert probe_res.verdict_state == "zero_detection", (
            f"fixture ZERO_DETECTION_DOC no longer trips zero_detection "
            f"(verdict_state={probe_res.verdict_state!r}) — recognizers changed, "
            f"pick a new synthetic doc that yields zero matches"
        )

        with pytest.raises(Exception) as exc_info:
            self._run(mcp, ZERO_DETECTION_DOC)

        # The raw text must not be embedded in the exception message either —
        # that message is what the caller surfaces via isError:true.
        assert ZERO_DETECTION_DOC not in str(exc_info.value)
        assert "l3 d0 m3n" not in str(exc_info.value)

    def test_raised_error_is_not_a_silent_return(self):
        """Belt-and-suspenders: calling _anonymise_text on the zero-detection
        fixture must RAISE, not return a string containing the raw doc."""
        mcp = _import_mcp()
        try:
            out = self._run(mcp, ZERO_DETECTION_DOC)
        except Exception:
            return  # expected — fail-closed via exception
        # If it didn't raise, it must be because the fixture stopped tripping
        # zero_detection (already covered above) — either way, the raw text
        # must never appear in a returned string.
        assert ZERO_DETECTION_DOC not in out
        assert "l3 d0 m3n" not in out

    def test_end_to_end_tools_call_iserror_no_body(self, monkeypatch):
        """Drive the real JSON-RPC tools/call handler for bubble_shield_read
        and bubble_shield_anonymize_text and confirm isError:true with a fixed
        message, and the raw doc text nowhere in the returned content."""
        mcp = _import_mcp()

        engine = _make_engine()
        vpath = Path("/tmp/test-589-vault-e2e.json")
        monkeypatch.setattr(mcp, "_engine", lambda *a, **kw: (engine, vpath, True))

        captured = {}

        def _fake_send(obj):
            captured["obj"] = obj

        monkeypatch.setattr(mcp, "_send", _fake_send)

        req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "bubble_shield_anonymize_text",
                "arguments": {"text": ZERO_DETECTION_DOC},
            },
        }
        mcp._handle(req)

        result = captured["obj"].get("result", {})
        text = "".join(part.get("text", "") for part in result.get("content", []))

        assert result.get("isError") is True, (
            f"expected isError:true on a zero-detection substantial doc, got: {result}")
        assert ZERO_DETECTION_DOC not in text
        assert "l3 d0 m3n" not in text
        # A clear, human-facing message: no certification, human review required,
        # content not returned.
        assert "PAS" in text or "pas" in text, f"expected a clear refusal message: {text!r}"


class TestNormalPiiDocStillMasked:
    """Regression: a document with real detectable PII must still be masked
    (not accidentally swept into the new fail-closed branch)."""

    def test_normal_doc_is_masked_not_refused(self):
        mcp = _import_mcp()
        engine = _make_engine()
        vpath = Path("/tmp/test-589-vault-normal.json")
        with pytest.MonkeyPatch.context() as m:
            m.setattr(mcp, "_engine", lambda *a, **kw: (engine, vpath, True))
            out = mcp._anonymise_text(NORMAL_PII_DOC)

        assert "synthetic.contact@example-test.fr" not in out, \
            f"email leaked in clear:\n{out}"
        assert "FR7630006000011234567890189" not in out, \
            f"IBAN leaked in clear:\n{out}"
        assert "⟦" in out, f"expected masking tokens in output:\n{out}"


class TestTinyEmptyInputNotOverBlocked:
    """Regression: nothing_to_do (tiny/empty input) must NOT be refused —
    only the substantial zero_detection case fails closed."""

    @pytest.mark.parametrize("text", TINY_INPUTS)
    def test_tiny_input_not_refused(self, text):
        mcp = _import_mcp()
        engine = _make_engine()
        vpath = Path("/tmp/test-589-vault-tiny.json")

        # Confirm the fixture really is nothing_to_do, not zero_detection.
        probe_res = _make_engine().anonymize(text)
        assert probe_res.verdict_state == "nothing_to_do", (
            f"fixture {text!r} is not nothing_to_do "
            f"(verdict_state={probe_res.verdict_state!r}) — pick a smaller fixture"
        )

        with pytest.MonkeyPatch.context() as m:
            m.setattr(mcp, "_engine", lambda *a, **kw: (engine, vpath, True))
            # Must NOT raise.
            out = mcp._anonymise_text(text)
        assert isinstance(out, str)


class TestDaemonDownUnchanged:
    """Regression: the pre-existing daemon-down fail-closed path (NERDownError)
    must be completely unaffected by this fix."""

    def test_daemon_down_still_raises_nerdownerror(self):
        mcp = _import_mcp()
        engine = _make_engine()
        vpath = Path("/tmp/test-589-vault-daemon-down.json")
        with pytest.MonkeyPatch.context() as m:
            m.setattr(mcp, "_engine", lambda *a, **kw: (engine, vpath, False))
            m.setattr(mcp, "_try_spawn_daemon_from_mcp", lambda: None)
            with pytest.raises(mcp.NERDownError):
                mcp._anonymise_text(NORMAL_PII_DOC)


class TestMcpbMirrorCopyBehavesIdentically:
    """The mcpb/server mirror copy must exhibit the exact same fail-closed
    behaviour (on top of test_mirror_copies_identical.py's byte-identity
    check, this exercises the mcpb copy's code path directly)."""

    def test_mcpb_copy_also_fails_closed_on_zero_detection(self):
        mcp_mcpb = _import_mcp(
            scripts_subpath=("plugin", "bubble-shield", "mcpb", "server", "scripts")
        )
        engine = _make_engine()
        vpath = Path("/tmp/test-589-vault-mcpb.json")
        with pytest.MonkeyPatch.context() as m:
            m.setattr(mcp_mcpb, "_engine", lambda *a, **kw: (engine, vpath, True))
            with pytest.raises(Exception) as exc_info:
                mcp_mcpb._anonymise_text(ZERO_DETECTION_DOC)
        assert ZERO_DETECTION_DOC not in str(exc_info.value)
