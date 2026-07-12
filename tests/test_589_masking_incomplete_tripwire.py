"""
test_589_masking_incomplete_tripwire.py — P0 SECURITY (#589): STRUCTURAL
TRIPWIRE. Fail closed whenever masking did NOT PROVABLY COMPLETE, regardless
of cause — not just the two already-known causes (daemon down / substantial
zero-detection) covered by test_589_zero_detection_failclosed.py.

ROOT CAUSE (live-confirmed, corrected diagnosis): a real client session showed
bubble_shield_read returning a RAW 43KB PDF plus a raw .docx, with ZERO masking tokens and isError=false — AND the
session's audit.jsonl had ZERO "anonymize" events for those reads. So
`_anonymise_text` did NOT complete a masking run, yet the RAW extracted text
was returned anyway. This violates the core guarantee (bubble_shield_mcp.py's
module docstring: "if anonymisation can't run, returns an ERROR, never raw").
Not reproducible from artifacts (transient session-time failure during a day
of daemon/arch turmoil) — but the CLASS of hole is structural: any path where
`res` ends up NOT being a genuinely-completed AnonymizationResult, and the
code still reaches `return res.anonymized + note`.

THE FIX: immediately after `res = engine.anonymize(text)` (before vault.save,
before ANY side effect that would trust `res`), _anonymise_text now requires
`res is not None and res.verdict_state in _VALID_VERDICT_STATES`. Any other
shape — None, a malformed/partial object, a mocked engine returning garbage —
raises MaskingIncompleteError, which the tools/call handler converts to
isError:true with a FIXED message and NO body (same fail-closed contract as
NERDownError / ZeroDetectionError).

KEY DISTINCTION (do not confuse with "zero PII found"): this tripwire does
NOT fire on a genuinely-completed 'nothing_to_do' or 'zero_detection' run —
those carry a VALID verdict_state (the engine DID run to completion, it just
found nothing / found nothing on a substantial doc) and are handled by their
own existing gates (zero_detection raises ZeroDetectionError explicitly;
nothing_to_do returns normally). This tripwire is strictly about "did the run
complete at all", not "what did it find".

Coverage (mirrors the brief's 4 required scenarios):
  1. engine.anonymize() returns a result with NO valid verdict_state (a
     malformed/partial object simulating a swallowed-exception path that still
     produced *something*) → FAILS CLOSED, raw text NOT in the raised message.
  2. engine.anonymize() returns None outright → FAILS CLOSED.
  3. A completed run with zero PII (nothing_to_do) → still RETURNS (not
     over-blocked) — regression guard, answers "what if no PII".
  4. A normal PII doc → still masked (regression, tokens present).
  5. Daemon-down → still raises NERDownError, unchanged.
  6. End-to-end tools/call handler → isError:true, no body, raw text absent.
  7. Both plugin + mcpb mirror copies behave identically.

Synthetic data only — no real client content anywhere in this file.
"""
from __future__ import annotations

import importlib.util as _ilu
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bubble_shield import policy as P  # noqa: E402
from bubble_shield.engine import AnonymizationEngine  # noqa: E402
from bubble_shield.vault import Vault  # noqa: E402


def _make_engine():
    """A real AnonymizationEngine, default (cloak-everything) policy."""
    return AnonymizationEngine(
        vault=Vault(mission="test-589-tripwire"),
        match_filter=P.make_match_filter(P.default_policy()),
    )


def _import_mcp(scripts_subpath=("plugin", "bubble-shield", "scripts")):
    """Import bubble_shield_mcp from either the plugin or the mcpb mirror copy,
    without running it as __main__, and without polluting sys.modules across
    the two variants. Mirrors test_589_zero_detection_failclosed.py's helper."""
    mcp_path = ROOT.joinpath(*scripts_subpath) / "bubble_shield_mcp.py"

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

    mod_name = "bubble_shield_mcp_589_tripwire_" + "_".join(scripts_subpath)
    spec = _ilu.spec_from_file_location(mod_name, str(mcp_path))
    mod = _ilu.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


# ── synthetic fixtures ───────────────────────────────────────────────────────

# The actual doc SHAPE from the live leak: substantial free-text prose with
# real-looking (synthetic) identifying data, so that IF masking had completed
# normally it would have produced tokens. The point of these tests is that it
# must NEVER reach the caller in the clear when the run did not complete.
RAW_LEAK_SHAPED_DOC = (
    "Liasse fiscale synthetique - SELARL Exemple Test. Le gerant M. Jean "
    "Fictif-Exemple, domicilie au 12 rue de la Republique Synthetique, "
    "joignable au 06 00 00 00 00, a signe le present document pour le compte "
    "de la societe. Reference client : DOSSIER-TEST-000000."
)

NORMAL_PII_DOC = (
    "Merci de contacter synthetic.contact@example-test.fr pour toute question. "
    "Le compte de reference est FR7630006000011234567890189 pour le virement."
)

NOTHING_TO_DO_INPUT = "ok"


class _SwallowedExceptionEngine:
    """Simulates the exact failure class behind the live leak: something
    downstream of the daemon call swallowed an exception and handed back an
    object that is NOT a real completed AnonymizationResult — no usable
    verdict_state — instead of raising. This is deliberately NOT a mock of
    `engine.anonymize` raising (that's already fail-closed via the generic
    `except Exception` in _handle) — it's the SILENT case: anonymize()
    returns normally, but with garbage, and old code trusted it anyway."""

    def __init__(self):
        self.vault = Vault(mission="test-589-tripwire-swallowed")

    def anonymize(self, text):
        # No verdict_state attribute at all — the malformed-object case.
        return SimpleNamespace(anonymized=text)


class _NoneReturningEngine:
    """Simulates engine.anonymize() returning None outright (e.g. a future
    refactor / a monkeypatch bug) — the other half of 'looked up fine but
    never finished'."""

    def __init__(self):
        self.vault = Vault(mission="test-589-tripwire-none")

    def anonymize(self, text):
        return None


class TestMaskingIncompleteFailsClosed:
    """THE regression that would have caught the live P0 leak session: when masking
    did not provably complete, the raw text must never come back."""

    def _run_with_engine(self, mcp, engine, text):
        vpath = Path("/tmp/test-589-tripwire-vault.json")
        with pytest.MonkeyPatch.context() as m:
            m.setattr(mcp, "_engine", lambda *a, **kw: (engine, vpath, True))
            return mcp._anonymise_text(text)

    def test_malformed_result_no_verdict_state_fails_closed(self):
        mcp = _import_mcp()
        engine = _SwallowedExceptionEngine()

        with pytest.raises(mcp.MaskingIncompleteError) as exc_info:
            self._run_with_engine(mcp, engine, RAW_LEAK_SHAPED_DOC)

        # The raw doc must not leak into the exception message either — that
        # message is exactly what the caller surfaces via isError:true.
        assert RAW_LEAK_SHAPED_DOC not in str(exc_info.value)
        assert "Jean Fictif-Exemple" not in str(exc_info.value)
        assert "06 00 00 00 00" not in str(exc_info.value)

    def test_none_result_fails_closed(self):
        mcp = _import_mcp()
        engine = _NoneReturningEngine()

        with pytest.raises(mcp.MaskingIncompleteError) as exc_info:
            self._run_with_engine(mcp, engine, RAW_LEAK_SHAPED_DOC)

        assert RAW_LEAK_SHAPED_DOC not in str(exc_info.value)

    def test_end_to_end_tools_call_iserror_no_raw_body(self, monkeypatch):
        """Drive the real JSON-RPC tools/call handler for bubble_shield_read
        (via bubble_shield_anonymize_text, same _anonymise_text code path) and
        confirm isError:true, a fixed message, and the raw doc text nowhere in
        the returned content — the exact shape of the live P0 leak session this
        closes (43KB PDF, zero tokens, isError=false → must now be
        isError=true, zero body)."""
        mcp = _import_mcp()
        engine = _SwallowedExceptionEngine()
        vpath = Path("/tmp/test-589-tripwire-vault-e2e.json")
        monkeypatch.setattr(mcp, "_engine", lambda *a, **kw: (engine, vpath, True))

        captured = {}
        monkeypatch.setattr(mcp, "_send", lambda obj: captured.update(obj=obj))

        req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "bubble_shield_anonymize_text",
                "arguments": {"text": RAW_LEAK_SHAPED_DOC},
            },
        }
        mcp._handle(req)

        result = captured["obj"].get("result", {})
        text = "".join(part.get("text", "") for part in result.get("content", []))

        assert result.get("isError") is True, (
            f"expected isError:true when masking did not complete, got: {result}")
        assert RAW_LEAK_SHAPED_DOC not in text
        assert "Jean Fictif-Exemple" not in text
        assert "06 00 00 00 00" not in text
        assert "DOSSIER-TEST-000000" not in text


class TestCompletedZeroPiiStillReturns:
    """Regression / 'what if genuinely no PII': a COMPLETED run with a valid
    verdict_state (nothing_to_do) must NOT be over-blocked by the tripwire —
    the tripwire is about completion, not about detection count."""

    def test_nothing_to_do_not_refused_by_tripwire(self):
        mcp = _import_mcp()
        engine = _make_engine()
        vpath = Path("/tmp/test-589-tripwire-vault-nothing.json")

        probe_res = _make_engine().anonymize(NOTHING_TO_DO_INPUT)
        assert probe_res.verdict_state == "nothing_to_do", (
            f"fixture {NOTHING_TO_DO_INPUT!r} is not nothing_to_do "
            f"(verdict_state={probe_res.verdict_state!r})"
        )

        with pytest.MonkeyPatch.context() as m:
            m.setattr(mcp, "_engine", lambda *a, **kw: (engine, vpath, True))
            out = mcp._anonymise_text(NOTHING_TO_DO_INPUT)  # must NOT raise
        assert isinstance(out, str)


class TestNormalPiiDocStillMaskedUnderTripwire:
    """Regression: the tripwire must not interfere with the normal masked-ok
    path — a real completed run with detectable PII is still masked."""

    def test_normal_doc_is_masked_not_refused(self):
        mcp = _import_mcp()
        engine = _make_engine()
        vpath = Path("/tmp/test-589-tripwire-vault-normal.json")
        with pytest.MonkeyPatch.context() as m:
            m.setattr(mcp, "_engine", lambda *a, **kw: (engine, vpath, True))
            out = mcp._anonymise_text(NORMAL_PII_DOC)

        assert "synthetic.contact@example-test.fr" not in out
        assert "FR7630006000011234567890189" not in out
        assert "⟦" in out, f"expected masking tokens in output:\n{out}"


class TestDaemonDownUnaffectedByTripwire:
    """Regression: the daemon-down fail-closed path (NERDownError) is
    unchanged — it still fires BEFORE engine.anonymize() is ever called, so
    the new tripwire never even gets a chance to run in that case."""

    def test_daemon_down_still_raises_nerdownerror(self):
        mcp = _import_mcp()
        engine = _make_engine()
        vpath = Path("/tmp/test-589-tripwire-vault-daemon-down.json")
        with pytest.MonkeyPatch.context() as m:
            m.setattr(mcp, "_engine", lambda *a, **kw: (engine, vpath, False))
            m.setattr(mcp, "_try_spawn_daemon_from_mcp", lambda: None)
            with pytest.raises(mcp.NERDownError):
                mcp._anonymise_text(NORMAL_PII_DOC)


class TestMcpbMirrorCopyBehavesIdentically:
    """The mcpb/server mirror copy must exhibit the exact same tripwire
    behaviour (on top of test_mirror_copies_identical.py's byte-identity
    check, this exercises the mcpb copy's code path directly)."""

    def test_mcpb_copy_also_fails_closed_on_incomplete_masking(self):
        mcp_mcpb = _import_mcp(
            scripts_subpath=("plugin", "bubble-shield", "mcpb", "server", "scripts")
        )
        engine = _SwallowedExceptionEngine()
        vpath = Path("/tmp/test-589-tripwire-vault-mcpb.json")
        with pytest.MonkeyPatch.context() as m:
            m.setattr(mcp_mcpb, "_engine", lambda *a, **kw: (engine, vpath, True))
            with pytest.raises(mcp_mcpb.MaskingIncompleteError) as exc_info:
                mcp_mcpb._anonymise_text(RAW_LEAK_SHAPED_DOC)
        assert RAW_LEAK_SHAPED_DOC not in str(exc_info.value)
