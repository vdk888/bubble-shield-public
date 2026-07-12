"""
test_589_quality_gate.py — P0 #589: TEXT-QUALITY GATE on zero-detection.

ROOT CAUSE OF THE REFINEMENT (Joris, live-validated 2026-07-07): the P0 base
fix (test_589_zero_detection_failclosed.py) made EVERY substantial
zero_detection verdict a hard refusal. That over-blocks: a genuinely clean
document (real prose, GLiNER had a fair shot and confidently found nothing)
is the honest "no PII here" case and must NOT be refused. Candidate COUNT
alone can't tell the two apart — a garbage/OCR extraction can produce a stray
false candidate while clean prose produces none. What DOES separate them,
live-measured on real doc pairs:
    CLEAN prose:   real_word_ratio=0.84  avg_word_len=6.4  nonword_pct=0.7
    GARBAGE/OCR:   real_word_ratio=0.08  avg_word_len=2.4  nonword_pct=13.3

THE FIX: inside the zero_detection branch of _anonymise_text, call
_text_quality_gate(res.original) (real_word_ratio / avg_word_len /
nonword_pct against the module-top calibrated constants
_QUALITY_MIN_REAL_WORD_RATIO / _QUALITY_MIN_AVG_WORD_LEN /
_QUALITY_MAX_NONWORD_PCT):
    - CLEAN (gate passes)   → falls through, RETURNS with the honest
      _ZERO_DETECTION_CLEAN_NOTE appended (unchanged shape from before the P0
      base fix).
    - GARBLED (gate fails)  → raises ZeroDetectionError (fail-closed, no body
      returned) — this is the real-incident leak class this fix closes: a
      scanned-PDF financial document, OCR-degraded extraction.

Does NOT touch: MaskingIncompleteError tripwire, NERDownError (daemon-down),
nothing_to_do, masked_ok/leak/low_confidence — see
test_589_masking_incomplete_tripwire.py / test_589_zero_detection_failclosed.py
for those regressions; this file re-confirms the ones most likely to be
disturbed by this specific change (the zero_detection branch itself).

Coverage:
  1. Substantial CLEAN prose, zero detections → RETURNS (not refused), and
     the returned text carries the honest clean-verdict note.
  2. Substantial GARBLED text, zero detections → FAILS CLOSED, raw text NOT
     returned anywhere (exception message or otherwise).
  3. A table/number-heavy but real-word CGP-shaped doc, zero detections →
     NOT falsely refused (guards the brief's explicit over-blocking concern).
  4. Boundary doc engineered to sit just inside "clean" (documents which side
     the thresholds place it).
  5. Regressions: normal PII doc still masked; nothing_to_do still returns;
     daemon-down still raises NERDownError unchanged.
  6. Both plugin + mcpb mirror copies behave identically.

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
        vault=Vault(mission="test-589-quality"),
        match_filter=P.make_match_filter(P.default_policy()),
    )


def _import_mcp(scripts_subpath=("plugin", "bubble-shield", "scripts")):
    """Import bubble_shield_mcp from either the plugin or the mcpb mirror copy,
    without running it as __main__, and without polluting sys.modules across
    the two variants."""
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

    mod_name = "bubble_shield_mcp_589_quality_" + "_".join(scripts_subpath)
    spec = _ilu.spec_from_file_location(mod_name, str(mcp_path))
    mod = _ilu.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


# ── synthetic fixtures ───────────────────────────────────────────────────────

# Substantial CLEAN prose, no names/emails/IBANs/numbers-shaped-like-PII —
# trips zero_detection AND should read as high text-quality (real words,
# normal length, ~no symbol noise).
CLEAN_ZERO_DETECTION_DOC = (
    "Le present document decrit une procedure generale de verification interne "
    "sans mentionner aucune personne ni aucune coordonnee particuliere. Chaque "
    "etape doit etre validee avant de passer a la suivante, dans le respect "
    "des regles internes de controle qualite habituelles du service concerne. "
    "Ce rapport presente egalement les grandes lignes de la methodologie "
    "utilisee ainsi que les conclusions generales issues de cette analyse."
)

# Substantial GARBLED text — short glyph fragments, digit/letter mashups,
# heavy symbol noise — modeled on real OCR-failure output (isolated
# fragments from a scanned PDF with a broken text layer). Also trips
# zero_detection (no recognizable PII shapes) but should read as low
# text-quality on all three signals.
GARBLED_ZERO_DETECTION_DOC = (
    "l3 d0 m3n xz p0r t5 io qw3 rt ui0 as gh zx bn qw rt "
    "1z 3x y5 q p0 u7 t6 e5 q1 z2 c3 b4 m5 l k j h g f d s a "
    "0987 !@#$ %^&* () qa ed tg uj ol p0 m, n. b/ v; c: x! z? "
    "9a1 8s2 7d3 6f4 5g5 4h6 3j7 2k8 1l9 q0w e9r t8y u7i o6p"
)

# A table/number-heavy but real-word CGP-shaped doc: lots of figures and
# reference codes, but the surrounding words are real French words of normal
# length — must NOT be falsely refused (the brief's explicit over-blocking
# concern: a legit table-heavy doc with real words should pass quality).
TABLE_HEAVY_REAL_WORD_DOC = (
    "Bilan comptable exercice 2026 : Actif immobilise 125430 EUR, Actif "
    "circulant 84200 EUR, soit un Total actif de 209630 EUR. Cote passif, "
    "Capitaux propres 98000 EUR et Dettes financieres 111630 EUR. Chiffre "
    "affaires annuel 452100 EUR, Resultat net 31450 EUR, Marge brute 22 pct. "
    "Reference dossier CGP-2026-0417, identifiant client 88213, contrat "
    "numero 4471, valable jusqu au trente et un decembre deux mille vingt six."
)

# A normal doc carrying clearly-detectable PII (email + IBAN-shaped string) —
# must still be masked normally; this is the non-regression case (does not
# hit the zero_detection branch at all).
NORMAL_PII_DOC = (
    "Merci de contacter synthetic.contact@example-test.fr pour toute question. "
    "Le compte de reference est FR7630006000011234567890189 pour le virement."
)

# Tiny/empty inputs → nothing_to_do, must NOT be refused (unaffected by this
# change — the zero_detection branch never fires for these).
TINY_INPUTS = ["", "   ", "ok", "31/12/2024"]


class TestCleanZeroDetectionReturns:
    """The core refinement: clean prose with zero detections must RETURN, not
    be refused — the genuine "no PII in this document" case."""

    def _run(self, mcp, text):
        engine = _make_engine()
        vpath = Path("/tmp/test-589-quality-vault-clean.json")
        with pytest.MonkeyPatch.context() as m:
            m.setattr(mcp, "_engine", lambda *a, **kw: (engine, vpath, True))
            return mcp._anonymise_text(text)

    def test_fixture_really_is_zero_detection(self):
        """Sanity: confirm the clean fixture trips zero_detection before
        asserting quality-gate behaviour on top of it."""
        probe_engine = _make_engine()
        probe_res = probe_engine.anonymize(CLEAN_ZERO_DETECTION_DOC)
        assert probe_res.verdict_state == "zero_detection", (
            f"fixture CLEAN_ZERO_DETECTION_DOC no longer trips zero_detection "
            f"(verdict_state={probe_res.verdict_state!r}) — recognizers changed, "
            f"pick a new synthetic doc that yields zero matches"
        )

    def test_fixture_passes_the_quality_gate_directly(self):
        """Sanity: confirm the clean fixture reads as high quality per the
        gate function itself (independent of the zero_detection wiring)."""
        mcp = _import_mcp()
        assert mcp._text_quality_gate(CLEAN_ZERO_DETECTION_DOC) is True

    def test_clean_zero_detection_doc_returns_not_refused(self):
        mcp = _import_mcp()
        out = self._run(mcp, CLEAN_ZERO_DETECTION_DOC)
        assert isinstance(out, str)
        # The honest clean-verdict note must be present (unchanged shape from
        # before the P0 base fix over-blocked this case).
        assert "aucune donnée identifiante détectée" in out
        assert "relecture humaine est requise" in out
        # The original text is legitimately returned here (clean doc, no PII) —
        # confirm it round-trips rather than being silently dropped/refused.
        assert "Le present document decrit" in out


class TestGarbledZeroDetectionFailsClosed:
    """The other half of the split: garbled/low-quality text with zero
    detections must still hard fail-closed — the scanned-PDF/OCR leak class."""

    def _run(self, mcp, text):
        engine = _make_engine()
        vpath = Path("/tmp/test-589-quality-vault-garbled.json")
        with pytest.MonkeyPatch.context() as m:
            m.setattr(mcp, "_engine", lambda *a, **kw: (engine, vpath, True))
            return mcp._anonymise_text(text)

    def test_fixture_really_is_zero_detection(self):
        probe_engine = _make_engine()
        probe_res = probe_engine.anonymize(GARBLED_ZERO_DETECTION_DOC)
        assert probe_res.verdict_state == "zero_detection", (
            f"fixture GARBLED_ZERO_DETECTION_DOC no longer trips zero_detection "
            f"(verdict_state={probe_res.verdict_state!r}) — pick a new synthetic "
            f"garbled doc that still yields zero matches"
        )

    def test_fixture_fails_the_quality_gate_directly(self):
        mcp = _import_mcp()
        assert mcp._text_quality_gate(GARBLED_ZERO_DETECTION_DOC) is False

    def test_garbled_zero_detection_doc_fails_closed(self):
        mcp = _import_mcp()
        with pytest.raises(mcp.ZeroDetectionError) as exc_info:
            self._run(mcp, GARBLED_ZERO_DETECTION_DOC)
        # Raw garbled text must not leak into the exception message.
        assert GARBLED_ZERO_DETECTION_DOC not in str(exc_info.value)
        assert "l3 d0 m3n" not in str(exc_info.value)

    def test_end_to_end_tools_call_iserror_no_body(self, monkeypatch):
        mcp = _import_mcp()
        engine = _make_engine()
        vpath = Path("/tmp/test-589-quality-vault-garbled-e2e.json")
        monkeypatch.setattr(mcp, "_engine", lambda *a, **kw: (engine, vpath, True))

        captured = {}
        monkeypatch.setattr(mcp, "_send", lambda obj: captured.__setitem__("obj", obj))

        req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "bubble_shield_anonymize_text",
                "arguments": {"text": GARBLED_ZERO_DETECTION_DOC},
            },
        }
        mcp._handle(req)

        result = captured["obj"].get("result", {})
        text = "".join(part.get("text", "") for part in result.get("content", []))

        assert result.get("isError") is True, (
            f"expected isError:true on a garbled zero-detection doc, got: {result}")
        assert GARBLED_ZERO_DETECTION_DOC not in text
        assert "l3 d0 m3n" not in text
        assert "texte illisible" in text or "extraction dégradée" in text, (
            f"expected the degraded-extraction message: {text!r}")
        assert "contenu n'est PAS renvoyé" not in text or "PAS" in text


class TestTableHeavyRealWordDocNotOverBlocked:
    """Guard against over-blocking: a legit table/number-heavy CGP-shaped doc
    with real surrounding words must NOT be falsely refused."""

    def test_fixture_really_is_zero_detection(self):
        probe_engine = _make_engine()
        probe_res = probe_engine.anonymize(TABLE_HEAVY_REAL_WORD_DOC)
        assert probe_res.verdict_state == "zero_detection", (
            f"fixture TABLE_HEAVY_REAL_WORD_DOC no longer trips zero_detection "
            f"(verdict_state={probe_res.verdict_state!r}) — adjust fixture so it "
            f"still yields zero matches while remaining number/table-heavy"
        )

    def test_fixture_passes_the_quality_gate(self):
        mcp = _import_mcp()
        assert mcp._text_quality_gate(TABLE_HEAVY_REAL_WORD_DOC) is True, (
            "a legit table/number-heavy doc with real words must not trip the "
            "quality gate — real_word_ratio/avg_word_len should carry it through "
            "even with heavy digit/figure content"
        )

    def test_table_heavy_doc_returns_not_refused(self):
        mcp = _import_mcp()
        engine = _make_engine()
        vpath = Path("/tmp/test-589-quality-vault-table.json")
        with pytest.MonkeyPatch.context() as m:
            m.setattr(mcp, "_engine", lambda *a, **kw: (engine, vpath, True))
            out = mcp._anonymise_text(TABLE_HEAVY_REAL_WORD_DOC)
        assert isinstance(out, str)
        assert "Bilan comptable" in out


class TestQualityGateBoundary:
    """Document which side of the calibrated thresholds a borderline sample
    falls on, so drift is caught explicitly rather than silently."""

    def test_real_word_ratio_threshold_value(self):
        mcp = _import_mcp()
        assert mcp._QUALITY_MIN_REAL_WORD_RATIO == 0.40
        assert mcp._QUALITY_MIN_AVG_WORD_LEN == 3.5
        assert mcp._QUALITY_MAX_NONWORD_PCT == 8.0

    def test_doc_just_below_real_word_ratio_threshold_fails_gate(self):
        """Half real words / half single-char digit-letter noise tokens —
        engineered to sit BELOW 0.40 real_word_ratio. Documents that this
        specific doc lands on the GARBLED side of the split."""
        mcp = _import_mcp()
        # 6 real words, 10 single-token noise fragments -> ratio 6/16 = 0.375 < 0.40
        text = (
            "bonjour comment allez vous aujourd hui "
            "a1 b2 c3 d4 e5 f6 g7 h8 i9 j0"
        )
        assert mcp._text_quality_gate(text) is False

    def test_doc_comfortably_above_all_thresholds_passes_gate(self):
        mcp = _import_mcp()
        assert mcp._text_quality_gate(CLEAN_ZERO_DETECTION_DOC) is True


class TestNormalPiiDocStillMasked:
    """Regression: a document with real detectable PII must still be masked
    (never reaches the zero_detection branch at all)."""

    def test_normal_doc_is_masked_not_refused(self):
        mcp = _import_mcp()
        engine = _make_engine()
        vpath = Path("/tmp/test-589-quality-vault-normal.json")
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
    the quality gate only applies inside the zero_detection branch."""

    @pytest.mark.parametrize("text", TINY_INPUTS)
    def test_tiny_input_not_refused(self, text):
        mcp = _import_mcp()
        engine = _make_engine()
        vpath = Path("/tmp/test-589-quality-vault-tiny.json")

        probe_res = _make_engine().anonymize(text)
        assert probe_res.verdict_state == "nothing_to_do", (
            f"fixture {text!r} is not nothing_to_do "
            f"(verdict_state={probe_res.verdict_state!r}) — pick a smaller fixture"
        )

        with pytest.MonkeyPatch.context() as m:
            m.setattr(mcp, "_engine", lambda *a, **kw: (engine, vpath, True))
            out = mcp._anonymise_text(text)
        assert isinstance(out, str)


class TestDaemonDownUnchanged:
    """Regression: the pre-existing daemon-down fail-closed path (NERDownError)
    must be completely unaffected by this quality-gate refinement."""

    def test_daemon_down_still_raises_nerdownerror(self):
        mcp = _import_mcp()
        engine = _make_engine()
        vpath = Path("/tmp/test-589-quality-vault-daemon-down.json")
        with pytest.MonkeyPatch.context() as m:
            m.setattr(mcp, "_engine", lambda *a, **kw: (engine, vpath, False))
            m.setattr(mcp, "_try_spawn_daemon_from_mcp", lambda: None)
            with pytest.raises(mcp.NERDownError):
                mcp._anonymise_text(NORMAL_PII_DOC)


class TestMaskingIncompleteTripwireUnchanged:
    """Regression: the structural tripwire (masking-didn't-complete) must
    still fail closed, untouched by this quality-gate refinement."""

    def test_none_result_still_raises_masking_incomplete(self):
        mcp = _import_mcp()
        engine = _make_engine()
        vpath = Path("/tmp/test-589-quality-vault-tripwire.json")
        with pytest.MonkeyPatch.context() as m:
            m.setattr(mcp, "_engine", lambda *a, **kw: (engine, vpath, True))
            m.setattr(engine, "anonymize", lambda text: None)
            with pytest.raises(mcp.MaskingIncompleteError):
                mcp._anonymise_text(NORMAL_PII_DOC)


class TestMcpbMirrorCopyBehavesIdentically:
    """The mcpb/server mirror copy must exhibit the exact same clean/garbled
    split behaviour."""

    def test_mcpb_copy_returns_on_clean_zero_detection(self):
        mcp_mcpb = _import_mcp(
            scripts_subpath=("plugin", "bubble-shield", "mcpb", "server", "scripts")
        )
        engine = _make_engine()
        vpath = Path("/tmp/test-589-quality-vault-mcpb-clean.json")
        with pytest.MonkeyPatch.context() as m:
            m.setattr(mcp_mcpb, "_engine", lambda *a, **kw: (engine, vpath, True))
            out = mcp_mcpb._anonymise_text(CLEAN_ZERO_DETECTION_DOC)
        assert isinstance(out, str)
        assert "aucune donnée identifiante détectée" in out

    def test_mcpb_copy_fails_closed_on_garbled_zero_detection(self):
        mcp_mcpb = _import_mcp(
            scripts_subpath=("plugin", "bubble-shield", "mcpb", "server", "scripts")
        )
        engine = _make_engine()
        vpath = Path("/tmp/test-589-quality-vault-mcpb-garbled.json")
        with pytest.MonkeyPatch.context() as m:
            m.setattr(mcp_mcpb, "_engine", lambda *a, **kw: (engine, vpath, True))
            with pytest.raises(mcp_mcpb.ZeroDetectionError) as exc_info:
                mcp_mcpb._anonymise_text(GARBLED_ZERO_DETECTION_DOC)
        assert GARBLED_ZERO_DETECTION_DOC not in str(exc_info.value)
