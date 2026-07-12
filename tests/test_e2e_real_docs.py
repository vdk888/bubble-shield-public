"""Task 13b — ENLARGED end-to-end proof of the shadow-index redesign on REAL
approved client documents (Dropbox /Users/joris/Dropbox/clients), plus the
write/decode round-trip and Dropbox dataless-file resilience.

Joris explicitly approved the real client docs under ~/Dropbox/clients for this
test. HARD PII DISCIPLINE (this file is committed to git): NO real client name /
IBAN / value is ever written as a literal here. Every expectation is derived at
RUNTIME:
  - masking is asserted GENERICALLY (output contains ⟦…⟧ tokens, no raw value
    from the vault survives in clear);
  - the round-trip is proven by CAPTURE-AND-RESTORE (anonymize a real doc, keep
    its vault, restore, assert every captured value comes back) — the real
    values live only in local process memory, never in a committed assertion;
  - where an exact known-input assertion is needed, a SYNTHETIC doc created in
    the test is used, never a real client's data.

Environment reality (reported, not hidden):
  - The NER/Gemma daemon is often DOWN in CI/this env. The production
    _anonymise_file then FAILS CLOSED with NERDownError (by design — regex-only
    cannot certify context-free names). So the full-pipeline real sweep is
    SKIPPED-WITH-REASON when the daemon is down, and a real-but-model-free path
    (real extractor + real regex recognizers via the production engine) is run
    over the same real docs instead — real code over real docs, clearly labelled.
  - Dropbox files under clients/ are "online-only" placeholders: reading their
    bytes raises OSError (errno 11, "Resource deadlock avoided") until Dropbox
    hydrates them. In this env they do NOT hydrate. The e2e uses the subset that
    IS materialized for content assertions, and uses the WHOLE (mostly-dataless)
    folder to prove the sweep survives dataless files.
"""

import sys
from pathlib import Path

import pytest

from bubble_shield import shadow_index, shadow_store
from bubble_shield.engine import AnonymizationEngine
from bubble_shield.vault import Vault, TOKEN_RE

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "plugin/bubble-shield/scripts"))
sys.path.insert(0, str(HERE.parent / "plugin/bubble-shield/vendor"))
import bubble_shield_mcp as M  # noqa: E402
from bubble_shield_extract import extract_file, ExtractionError  # noqa: E402

CLIENTS = Path("/Users/joris/Dropbox/clients")

# The redesign's masking sentinel — every masked span is wrapped in ⟦…⟧.
_OPEN, _CLOSE = "⟦", "⟧"


def _readable_client_files():
    """Return the subset of clients/ files whose bytes are actually on disk
    (materialized). A dataless Dropbox placeholder raises OSError when its bytes
    are touched; we probe by reading 64 bytes. Skips .DS_Store noise."""
    if not CLIENTS.is_dir():
        return []
    out = []
    for p in sorted(CLIENTS.rglob("*")):
        if not p.is_file() or p.name == ".DS_Store":
            continue
        try:
            with open(p, "rb") as fh:
                fh.read(64)
            out.append(p)
        except OSError:
            continue  # dataless / online-only placeholder
    return out


def _extractable_real_doc():
    """A real, materialized client doc that yields real extractable text (some
    real PDFs are scanned images → ExtractionError; those are skipped here). The
    returned text is REAL PII and stays in local memory only — never asserted as
    a literal."""
    for p in _readable_client_files():
        try:
            text = extract_file(p)
        except (ExtractionError, Exception):
            continue
        if text and sum(c.isalpha() for c in text) > 200:
            return p, text
    return None, None


def _daemon_up() -> bool:
    """True only if the NER daemon is genuinely reachable. We use the real
    internal pt._daemon_up() health probe — NOT _daemon_detector, which spawns a
    re-arm and can return a detector against a half-started daemon that then
    refuses /detect (ConnectionRefused). Empirically in this env the daemon is
    down / half-up, so this returns False and the model-free path is taken."""
    try:
        import posttool_anonymize as pt
        return bool(pt._daemon_up())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 1. Sweep the REAL clients/ folder.
#    Full pipeline if the daemon is warm; otherwise skip-with-reason and run a
#    real-but-model-free sweep (real extractor + real regex engine) over the
#    materialized real docs so the sweep machinery is still exercised on real
#    data. Either way: real code, real docs.
# ---------------------------------------------------------------------------
def test_real_folder_sweep(tmp_path, monkeypatch):
    if not CLIENTS.is_dir():
        pytest.skip(f"real client folder not present: {CLIENTS}")
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    monkeypatch.setenv("BUBBLE_SHIELD_STORE_PASSPHRASE", "e2e-real-pass")

    readable = _readable_client_files()
    if not readable:
        pytest.skip("no materialized client files in this env (all dataless)")

    # Point the sweep at a temp root holding SYMLINKS to the real materialized
    # docs, so the walk sees real bytes without us copying real PII to a new
    # on-disk file. (Content-hash + extraction read through the symlink.)
    root = tmp_path / "real_subset"
    root.mkdir()
    for p in readable:
        (root / p.name).symlink_to(p)

    if _daemon_up():
        # FULL PIPELINE over real docs — the real production _anonymise_file.
        anonymize_fn = M._anonymise_file
        mode = "full-pipeline (daemon UP)"
    else:
        # Daemon down: production _anonymise_file fails closed (NERDownError) —
        # that is correct and is exercised in test_real_anonymise_fails_closed
        # below. Here we prove the SWEEP machinery on real docs with a real,
        # model-free anonymizer: real extractor + the production AnonymizationEngine
        # in its regex-only config (real IBAN/TEL/etc. recognizers). Not a fake
        # lambda — real production masking code, just without the ML detector.
        extract_failures = {"count": 0}

        def anonymize_fn(path):
            try:
                text = extract_file(Path(path))
            except ExtractionError:
                # Real finding: some client PDFs are scanned images with no text
                # layer → extraction fails closed (needs OCR). The redesign's
                # sweep should not die on them; store an explicit sentinel shadow
                # so the doc is accounted for. (In the full pipeline these route
                # to OCR; here, model-free, we just record they need OCR.)
                extract_failures["count"] += 1
                return "⟦OCR_REQUISE⟧"
            eng = AnonymizationEngine()
            eng.vault = Vault(mission="e2e-real")
            return eng.anonymize(text).anonymized
        mode = "model-free real-engine (daemon DOWN — full pipeline skipped)"

    result = shadow_index.run_sweep(str(root), anonymize_fn=anonymize_fn)

    # Empirical: the sweep RAN TO COMPLETION over the real docs and produced the
    # full result dict. A real doc that can't be certified (scanned image needing
    # OCR, structured CERFA whose Gemma second pass is unreachable, NER offline)
    # counts as `failed` (fail-closed, no shadow) — NOT a crash. Every readable
    # file lands in exactly one bucket.
    assert set(result) == {"indexed", "skipped", "deferred", "failed"}
    print(f"\n[real sweep] mode={mode} files={len(readable)} result={result}")
    accounted = result["indexed"] + result["failed"] + result["deferred"] + result["skipped"]
    assert accounted == len(readable), (
        f"sweep did not account for every readable file: {accounted} != {len(readable)}"
    )
    # The sweep produced a real outcome for real docs — at least one indexed OR
    # at least one legitimately fail-closed. Either is a valid, reported result
    # (both prove the sweep survived real-world doc conditions end to end).
    assert result["indexed"] >= 1 or result["failed"] >= 1, (
        f"sweep produced no outcome at all for real docs; result={result}"
    )


# ---------------------------------------------------------------------------
# 1b. Report the daemon-down fail-closed contract on a REAL doc, so it's an
#     explicit, visible outcome rather than a silent skip.
# ---------------------------------------------------------------------------
def test_real_anonymise_fails_closed_when_daemon_down(tmp_path, monkeypatch):
    if _daemon_up():
        pytest.skip("daemon is UP — fail-closed-on-down path not exercised here")
    p, text = _extractable_real_doc()
    if p is None:
        pytest.skip("no extractable materialized real doc in this env")
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    # Production contract: with the NER daemon down, anonymising a REAL doc must
    # FAIL CLOSED (no partial/regex-only body certified as safe), never return
    # raw PII. This is the redesign behaving correctly on real data.
    with pytest.raises(Exception) as ei:
        M._anonymise_file(str(p))
    assert "NER" in str(ei.value) or ei.type.__name__ == "NERDownError"


# ---------------------------------------------------------------------------
# 2. Read an indexed REAL doc back via _read_with_shadow — cached masked
#    version, MODEL-FREE (sabotage _anonymise_file), contains ⟦…⟧ tokens.
# ---------------------------------------------------------------------------
def test_read_serves_cached_masked_model_free(tmp_path, monkeypatch):
    p, text = _extractable_real_doc()
    if p is None:
        pytest.skip("no extractable materialized real doc in this env")
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    monkeypatch.setenv("BUBBLE_SHIELD_STORE_PASSPHRASE", "e2e-real-pass")

    # Build the masked shadow from the REAL doc using the real production engine
    # (regex recognizers). This yields genuine ⟦…⟧ tokens over real content and a
    # vault of real values (kept only in local memory).
    eng = AnonymizationEngine()
    eng.vault = Vault(mission="e2e-read")
    masked = eng.anonymize(text).anonymized
    real_values = list(eng.vault.to_value.values())

    # Index the real doc's real bytes under its real content hash, storing the
    # masked shadow. index_one hashes p's bytes (materialized), so the later read
    # hits the cache by hash.
    shadow_index.index_one(str(p), anonymize_fn=lambda _p: masked)

    # SABOTAGE the model path: any model call at read time crashes the test.
    monkeypatch.setattr(
        M, "_anonymise_file",
        lambda _p: (_ for _ in ()).throw(AssertionError("models on read path")))

    served = M._read_with_shadow(str(p))

    # It served the CACHED masked shadow (model-free): identical to what was
    # stored, carrying ⟦…⟧ tokens, and NOT the raw extracted text.
    assert served == masked, "read did not serve the cached shadow verbatim"
    assert _OPEN in served and _CLOSE in served, "served text carries no ⟦…⟧ tokens"
    assert TOKEN_RE.findall(served), "served shadow carries no ⟦…⟧ tokens"
    assert served != text, "read served RAW extracted text, not the masked shadow"

    # Structured-PII shapes (IBAN / long digit runs) that were tokenised must NOT
    # survive in clear. We check the SHAPE, not any specific real value, so a
    # regex NOM false-positive on a common dictionary word (which legitimately
    # recurs elsewhere in the doc) does not spuriously fail this leak check.
    import re as _re
    assert not _re.search(r"FR\d{2}\s?\d{4}", served), "an IBAN shape survived in clear"
    # Any real value that is a LONG token (>= 8 chars, structured — IBAN/phone/id,
    # not a short common word) must not appear in clear in the served shadow.
    structured_leaks = [
        v for v in real_values
        if v and len(v.replace(" ", "")) >= 8 and any(c.isdigit() for c in v)
        and v in served
    ]
    assert not structured_leaks, (
        f"{len(structured_leaks)} structured value(s) leaked in clear"
    )


# ---------------------------------------------------------------------------
# 3a. WRITE / DECODE ROUND-TRIP on a SYNTHETIC known doc — EXACT restoration.
#     Uses the real production restore path (engine.deanonymize → vault.restore,
#     the same restore bubble_shield's write/decode side uses). Known synthetic
#     input → we can assert exact equality with zero real PII in git.
# ---------------------------------------------------------------------------
def test_roundtrip_exact_on_synthetic():
    eng = AnonymizationEngine()
    eng.vault = Vault(mission="e2e-rt-synth")
    # Synthetic, obviously-fake values (a valid-shaped IBAN + a phone) so regex
    # recognizers fire deterministically without any ML.
    original = (
        "IBAN FR7630006000011234567890189 et telephone 06 12 34 56 78 "
        "sont a proteger dans ce document de test."
    )
    res = eng.anonymize(original)
    # Anonymize produced tokens and hid the raw values.
    assert _OPEN in res.anonymized
    assert "FR7630006000011234567890189" not in res.anonymized
    assert "06 12 34 56 78" not in res.anonymized
    # DECODE: tokens → real values, via the real restore path.
    restored = eng.deanonymize(res.anonymized)
    assert restored == original                      # exact round-trip
    assert not TOKEN_RE.findall(restored)            # no token survives restore


# ---------------------------------------------------------------------------
# 3b. WRITE / DECODE ROUND-TRIP on a REAL doc — capture-and-restore.
#     Anonymize a real doc, keep its vault, restore, assert EVERY captured real
#     value comes back and no token survives. Real values never leave memory.
# ---------------------------------------------------------------------------
def test_roundtrip_capture_restore_on_real_doc(tmp_path, monkeypatch):
    p, text = _extractable_real_doc()
    if p is None:
        pytest.skip("no extractable materialized real doc in this env")
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))

    eng = AnonymizationEngine()
    eng.vault = Vault(mission="e2e-rt-real")
    res = eng.anonymize(text)
    captured = list(eng.vault.to_value.values())
    if not captured:
        pytest.skip("real doc produced zero regex detections (no tokens to restore)")

    # Every minted token is present in the anonymized output.
    assert TOKEN_RE.findall(res.anonymized)
    # DECODE via the real restore path.
    restored = eng.deanonymize(res.anonymized)
    # Value-level round-trip: every captured real value reappears after restore,
    # and no ⟦…⟧ token is left dangling. (Exact byte-equality with `text` can
    # differ because engine.anonymize normalises PDF glued-token whitespace — a
    # documented display-only transform; the VALUES round-trip regardless.)
    missing = [v for v in captured if v not in restored]
    assert not missing, f"{len(missing)} captured value(s) did not restore"
    assert not TOKEN_RE.findall(restored), "tokens survived restore"


# ---------------------------------------------------------------------------
# 4. DATALESS RESILIENCE on the REAL folder: sweeping clients/ (which in this
#    env is mostly online-only placeholders) must NOT crash — dataless files are
#    DEFERRED, readable ones proceed.
# ---------------------------------------------------------------------------
def test_real_folder_sweep_survives_dataless(tmp_path, monkeypatch):
    if not CLIENTS.is_dir():
        pytest.skip(f"real client folder not present: {CLIENTS}")
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    monkeypatch.setenv("BUBBLE_SHIELD_STORE_PASSPHRASE", "e2e-real-pass")

    total = 0
    dataless = 0
    for f in CLIENTS.rglob("*"):
        if not f.is_file() or f.name == ".DS_Store":
            continue
        total += 1
        try:
            with open(f, "rb") as fh:
                fh.read(1)
        except OSError:
            dataless += 1
    if dataless == 0:
        pytest.skip("no dataless files present — resilience path not exercised "
                    "(all client files materialized in this env)")

    # A trivial model-free anonymizer: this test proves the WALK survives the
    # dataless files, not the masking quality (covered elsewhere). Files that
    # DO read get a shadow; dataless ones must be deferred, never fatal.
    result = shadow_index.run_sweep(
        str(CLIENTS), anonymize_fn=lambda _p: "clean-shadow")

    print(f"\n[dataless resilience] total={total} dataless={dataless} "
          f"result={result}")
    # The sweep RAN TO COMPLETION over a folder with real dataless files.
    assert set(result) == {"indexed", "skipped", "deferred", "failed"}
    # At least the dataless files were deferred (marked pending for a later
    # sweep), proving one unreadable file never aborted the walk.
    assert result["deferred"] >= 1
    # Every deferred file is queued for the next sweep to retry.
    assert len(shadow_store.pending_files()) >= 1
