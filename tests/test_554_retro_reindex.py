"""
test_554_retro_reindex.py — #554 remainder: retro re-index of polluted shadows.

THE GAP (live-verified 2026-07-16): the self-correction loop (seed → de-pollution
→ sticky safe_words) cleans FUTURE docs, but a shadow written BEFORE a junk
word's judgment keeps that word masked forever — nothing re-indexes it.

THE FIX under test:
  1. put_shadow records value_hashes (sha256 of each masked value, canonical
     strip+lower) in a new shadow_values table.
  2. safe_words.add_safe(w) marks every shadow containing hash(w) STALE
     (fail-open hook).
  3. Reads keep serving stale shadows (over-masked = safe direction).
  4. list_indexed() excludes stale hashes → the next sweep re-indexes those
     files naturally; re-put clears the flag and refreshes value_hashes.

Safety property: a stale shadow is NEVER deleted — deleting would make the next
read a MISS, which serves RAW (B1). Over-masked-but-served beats raw.

Synthetic values only.
"""
import hashlib
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "plugin" / "bubble-shield" / "vendor"))

import pytest


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    monkeypatch.setenv("BUBBLE_SHIELD_SILENCE_PLAINTEXT_WARN", "1")
    return tmp_path


def _vh(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()


# ── store layer ───────────────────────────────────────────────────────────────

def test_put_shadow_records_value_hashes(home):
    from bubble_shield import shadow_store as ss
    ss.put_shadow("h1", "texte ⟦NOM_0001⟧", src_path="/tmp/a.txt",
                  value_hashes=[_vh("conseiller"), _vh("Sylvie FONTAINE")])
    conn = ss.connect()
    try:
        rows = {r[0] for r in conn.execute(
            "SELECT value_hash FROM shadow_values WHERE content_hash='h1'")}
    finally:
        conn.close()
    assert rows == {_vh("conseiller"), _vh("Sylvie FONTAINE")}


def test_value_hash_helper_is_canonical(home):
    from bubble_shield import shadow_store as ss
    assert ss.value_hash(" Conseiller ") == _vh("conseiller")


def test_mark_stale_flags_only_matching_shadows(home):
    from bubble_shield import shadow_store as ss
    ss.put_shadow("h1", "s1", src_path="/tmp/a.txt", value_hashes=[_vh("bonjour")])
    ss.put_shadow("h2", "s2", src_path="/tmp/b.txt", value_hashes=[_vh("fontaine")])
    n = ss.mark_stale_by_value_hash(_vh("bonjour"))
    assert n == 1
    assert ss.list_indexed() == {"h2"}          # stale excluded → sweep re-indexes
    assert ss.get_shadow("h1") == "s1"          # but reads STILL serve it (safe)


def test_reindex_clears_stale_and_refreshes_hashes(home):
    from bubble_shield import shadow_store as ss
    ss.put_shadow("h1", "polluted", src_path="/tmp/a.txt", value_hashes=[_vh("bonjour")])
    ss.mark_stale_by_value_hash(_vh("bonjour"))
    assert ss.list_indexed() == set()
    # re-index the same content: shadow replaced, stale cleared, hashes refreshed
    ss.put_shadow("h1", "clean", src_path="/tmp/a.txt", value_hashes=[_vh("fontaine")])
    assert ss.list_indexed() == {"h1"}
    conn = ss.connect()
    try:
        rows = {r[0] for r in conn.execute(
            "SELECT value_hash FROM shadow_values WHERE content_hash='h1'")}
    finally:
        conn.close()
    assert rows == {_vh("fontaine")}            # old junk hash gone


def test_legacy_db_upgrades_in_place(home):
    # A pre-#554 store (no stale column, no shadow_values) must upgrade additively.
    from bubble_shield import shadow_store as ss
    conn = ss.connect()
    conn.close()
    ss.put_shadow("h1", "s1", src_path="/tmp/a.txt")     # no value_hashes: fine
    assert ss.list_indexed() == {"h1"}
    assert ss.mark_stale_by_value_hash(_vh("x")) == 0    # no rows, no crash


# ── safe_words hook ───────────────────────────────────────────────────────────

def test_add_safe_marks_matching_shadows_stale(home):
    from bubble_shield import shadow_store as ss
    from bubble_shield import safe_words as sw
    ss.put_shadow("h1", "s1", src_path="/tmp/a.txt", value_hashes=[_vh("conseiller")])
    ss.put_shadow("h2", "s2", src_path="/tmp/b.txt", value_hashes=[_vh("fontaine")])
    sw.add_safe("Conseiller")                            # case-insensitive
    assert ss.list_indexed() == {"h2"}
    assert sw.is_safe("conseiller")


def test_add_safe_survives_broken_store(home, monkeypatch):
    # The hook is fail-open: a store error must never break add_safe.
    from bubble_shield import shadow_store as ss
    from bubble_shield import safe_words as sw
    monkeypatch.setattr(ss, "mark_stale_by_value_hash",
                        lambda vh: (_ for _ in ()).throw(RuntimeError("boom")))
    sw.add_safe("bonjour")                               # must not raise
    assert sw.is_safe("bonjour")


# ── index/sweep integration ──────────────────────────────────────────────────

def test_index_one_threads_value_hashes(home, tmp_path):
    from bubble_shield import shadow_store as ss
    from bubble_shield import shadow_index as si
    doc = tmp_path / "doc.txt"
    doc.write_text("le conseiller de Sylvie", encoding="utf-8")
    si.index_one(str(doc), anonymize_fn=lambda p: "le ⟦NOM_0001⟧ de ⟦NOM_0002⟧",
                 value_hashes_fn=lambda clean: [_vh("conseiller"), _vh("Sylvie")])
    h = ss.content_hash(doc)
    conn = ss.connect()
    try:
        rows = {r[0] for r in conn.execute(
            "SELECT value_hash FROM shadow_values WHERE content_hash=?", (h,))}
    finally:
        conn.close()
    assert rows == {_vh("conseiller"), _vh("Sylvie")}


def test_end_to_end_stale_shadow_is_reswept(home, tmp_path):
    """The full retro loop: index (junk masked) → add_safe → stale → re-sweep
    re-indexes the SAME unchanged file → clean shadow served."""
    from bubble_shield import shadow_store as ss
    from bubble_shield import safe_words as sw
    from bubble_shield import shadow_index as si
    root = tmp_path / "docs"; root.mkdir()
    doc = root / "d.txt"
    doc.write_text("le conseiller reste disponible", encoding="utf-8")

    state = {"junk_masked": True}
    def anon(p):
        return ("le ⟦NOM_0001⟧ reste disponible" if state["junk_masked"]
                else "le conseiller reste disponible")
    def vhfn(clean):
        return [_vh("conseiller")] if state["junk_masked"] else []

    r1 = si.run_sweep(str(root), anonymize_fn=anon, value_hashes_fn=vhfn)
    assert r1["indexed"] == 1
    h = ss.content_hash(doc)
    assert "⟦NOM_0001⟧" in ss.get_shadow(h)

    # de-pollution judges "conseiller" a word → sticky safe_words → stale
    sw.add_safe("conseiller")
    state["junk_masked"] = False                 # detection now drops it

    r2 = si.run_sweep(str(root), anonymize_fn=anon, value_hashes_fn=vhfn)
    assert r2["indexed"] == 1                    # re-indexed, not skipped
    assert ss.get_shadow(h) == "le conseiller reste disponible"

    # and a third sweep skips it again (no infinite re-index)
    r3 = si.run_sweep(str(root), anonymize_fn=anon, value_hashes_fn=vhfn)
    assert r3["skipped"] == 1 and r3["indexed"] == 0


# ── the sweep-side token→value resolver ──────────────────────────────────────

def test_vault_value_hashes_fn_resolves_tokens(home, tmp_path):
    from bubble_shield import shadow_index as si
    vault = tmp_path / "v.vault.json"
    vault.write_text(json.dumps({
        "to_value": {"⟦NOM_0001⟧": " Sylvie FONTAINE", "⟦NOM_0002⟧": "conseiller"},
    }), encoding="utf-8")
    fn = si.vault_value_hashes_fn(str(vault))
    got = set(fn("dossier de ⟦NOM_0001⟧, le ⟦NOM_0002⟧ et ⟦NOM_9999⟧"))
    # resolved values hashed canonically; unknown token ignored; no raw values returned
    assert got == {_vh("sylvie fontaine"), _vh("conseiller")}


def test_vault_value_hashes_fn_missing_vault_is_empty(home):
    from bubble_shield import shadow_index as si
    fn = si.vault_value_hashes_fn("/nonexistent/vault.json")
    assert fn("⟦NOM_0001⟧") == []
