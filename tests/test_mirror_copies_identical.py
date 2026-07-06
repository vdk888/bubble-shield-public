"""Byte-identity tripwire for the plugin ↔ MCPB-bundle mirror copies.

Bubble Shield ships a self-contained plugin AND an MCPB bundle that carries a
COPY of the same scripts/vendor under ``mcpb/server/``. The plugin copy and the
bundle copy MUST stay byte-identical — otherwise the code a Cowork client runs
(the .mcpb bundle) can silently diverge from the reviewed/tested plugin copy.

This is the exact failure mode the #40 single-source refactor exists to prevent
*within* a file (one decision function, no drift). But that guarantee only holds
per-copy: an editor who touches ``plugin/…/scripts/guard.py`` and forgets the
``mcpb/server`` mirror would ship a guard whose bundled build silently differs —
e.g. re-opening a closed leak in the .mcpb artifact ONLY, invisible to every
behavioural test that runs against the plugin copy.

This test fails loudly the instant any mirror pair drifts. If it fails, re-sync
per RELEASING.md (``rsync … scripts/ mcpb/server/scripts/`` etc.) and re-pack.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN = REPO / "plugin" / "bubble-shield"

# (label, plugin-copy, mcpb-bundle-copy) — every file duplicated into the bundle.
MIRROR_PAIRS = [
    (
        "guard.py",
        PLUGIN / "scripts" / "guard.py",
        PLUGIN / "mcpb" / "server" / "scripts" / "guard.py",
    ),
    (
        "bubble_shield_mcp.py",
        PLUGIN / "scripts" / "bubble_shield_mcp.py",
        PLUGIN / "mcpb" / "server" / "scripts" / "bubble_shield_mcp.py",
    ),
    (
        "bubble_shield_mail.py",
        PLUGIN / "scripts" / "bubble_shield_mail.py",
        PLUGIN / "mcpb" / "server" / "scripts" / "bubble_shield_mail.py",
    ),
    (
        "bubble_shield_setup_ml.py",
        PLUGIN / "scripts" / "bubble_shield_setup_ml.py",
        PLUGIN / "mcpb" / "server" / "scripts" / "bubble_shield_setup_ml.py",
    ),
    (
        "vendor/bubble_shield/known_pii_store.py",
        PLUGIN / "vendor" / "bubble_shield" / "known_pii_store.py",
        PLUGIN / "mcpb" / "server" / "vendor" / "bubble_shield" / "known_pii_store.py",
    ),
]


def test_plugin_and_mcpb_mirror_copies_are_byte_identical():
    """Each shipped file must be byte-for-byte identical to its bundle copy."""
    drifted = []
    for label, plugin_copy, bundle_copy in MIRROR_PAIRS:
        assert plugin_copy.is_file(), f"missing plugin copy: {plugin_copy}"
        assert bundle_copy.is_file(), f"missing bundle copy: {bundle_copy}"
        if plugin_copy.read_bytes() != bundle_copy.read_bytes():
            drifted.append(label)
    assert not drifted, (
        "plugin ↔ mcpb bundle DRIFT — the shipped .mcpb would run stale/divergent "
        f"code for: {drifted}. Re-sync per RELEASING.md and re-pack the .mcpb "
        "(rsync scripts/ + vendor/ into mcpb/server/, then mcpb pack)."
    )
