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

COVERAGE: rather than a hand-maintained file list (which itself drifts as new
files are added), this test DERIVES its coverage by globbing every ``*.py``
file actually shipped under ``mcpb/server/scripts/`` and
``mcpb/server/vendor/bubble_shield/`` and asserting each has a byte-identical
``plugin/bubble-shield/{scripts,vendor/bubble_shield}/<name>`` counterpart.
Any new file added to either mirrored tree is automatically covered with zero
maintenance here.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN = REPO / "plugin" / "bubble-shield"
MCPB_SERVER = PLUGIN / "mcpb" / "server"

# Mirrored directory pairs: (label, plugin-side dir, mcpb-bundle-side dir).
# Per RELEASING.md, the bundle is an rsync copy of these two source trees
# (excluding test files, __pycache__/.pyc, and deployment_allowlist.json —
# none of which live in the mcpb/server copy, so no exclusion list is needed
# here: we simply glob what the bundle actually ships and require a match).
MIRRORED_DIRS = [
    ("scripts", PLUGIN / "scripts", MCPB_SERVER / "scripts"),
    (
        "vendor/bubble_shield",
        PLUGIN / "vendor" / "bubble_shield",
        MCPB_SERVER / "vendor" / "bubble_shield",
    ),
]

# Files that intentionally exist ONLY in the mcpb/server copy (mcpb-only
# shims with no plugin-side counterpart) and must NOT be required to match
# anything. Empty today — document any future addition here with a reason.
MCPB_ONLY_EXCLUDES: set[str] = set()

# #576 — the INVERSE: files that legitimately exist ONLY on the PLUGIN side and are
# NOT expected in the bundle (so the inverse "missing-from-bundle" check must not flag
# them). test_*.py are excluded by the glob below (never bundled). Empty today: as of
# 2026-07-16 every plugin scripts/*.py + vendor/*.py IS mirrored to the bundle — so a
# NEW plugin file with no bundle copy is a real "never-mirrored" bug, exactly what
# #576 closes. Document any future plugin-only runtime file here with a reason.
PLUGIN_ONLY_EXCLUDES: set[str] = set()


def _discover_missing_from_bundle():
    """#576 — INVERSE of _discover_mirror_pairs: glob the PLUGIN side and find files
    that SHOULD be in the bundle but have NO bundle counterpart ("new-plugin-file-
    never-mirrored"). Excludes test_*.py (never bundled) + PLUGIN_ONLY_EXCLUDES.
    Returns the list of plugin-side labels missing from the bundle."""
    orphans = []
    for dir_label, plugin_dir, bundle_dir in MIRRORED_DIRS:
        if not plugin_dir.is_dir():
            continue
        for plugin_file in sorted(plugin_dir.glob("*.py")):
            name = plugin_file.name
            if name.startswith("test_") or name in PLUGIN_ONLY_EXCLUDES:
                continue
            if not (bundle_dir / name).is_file():
                orphans.append(f"{dir_label}/{name}")
    return orphans


def _discover_mirror_pairs():
    """Glob every *.py the bundle ships and pair it with its plugin source.

    Returns (pairs, missing) where:
      - pairs: list of (label, plugin_copy_path, bundle_copy_path) for files
        that exist on both sides and should be byte-identical.
      - missing: labels of bundle-side files with NO plugin-side counterpart
        (a real structural problem, not a drift — reported separately).
    """
    pairs = []
    missing = []
    for dir_label, plugin_dir, bundle_dir in MIRRORED_DIRS:
        assert bundle_dir.is_dir(), f"missing mcpb bundle dir: {bundle_dir}"
        for bundle_file in sorted(bundle_dir.glob("*.py")):
            name = bundle_file.name
            label = f"{dir_label}/{name}"
            if name in MCPB_ONLY_EXCLUDES:
                continue
            plugin_file = plugin_dir / name
            if not plugin_file.is_file():
                missing.append(label)
                continue
            pairs.append((label, plugin_file, bundle_file))
    return pairs, missing


def test_mirror_coverage_matches_bundle_contents():
    """Sanity check: every file the .mcpb ships maps to a plugin source file.

    This guards the discovery mechanism itself — if it ever finds zero pairs
    (e.g. a path renamed) or a bundle file with no plugin counterpart, that is
    a structural break the derived glob must surface, not silently ignore.
    """
    pairs, missing = _discover_mirror_pairs()
    assert not missing, (
        "mcpb/server ships file(s) with NO plugin/bubble-shield counterpart — "
        f"either add the missing plugin source or add to MCPB_ONLY_EXCLUDES "
        f"with a documented reason: {missing}"
    )
    assert len(pairs) >= 38, (
        f"expected to discover at least 38 mirrored files (12 scripts + 26 "
        f"vendor/bubble_shield), found {len(pairs)} — the glob may be broken"
    )


def test_plugin_and_mcpb_mirror_copies_are_byte_identical():
    """Each shipped file must be byte-for-byte identical to its bundle copy."""
    pairs, _missing = _discover_mirror_pairs()
    drifted = []
    for label, plugin_copy, bundle_copy in pairs:
        assert plugin_copy.is_file(), f"missing plugin copy: {plugin_copy}"
        assert bundle_copy.is_file(), f"missing bundle copy: {bundle_copy}"
        if plugin_copy.read_bytes() != bundle_copy.read_bytes():
            drifted.append(label)
    assert not drifted, (
        "plugin ↔ mcpb bundle DRIFT — the shipped .mcpb would run stale/divergent "
        f"code for: {drifted}. Re-sync per RELEASING.md and re-pack the .mcpb "
        "(rsync scripts/ + vendor/ into mcpb/server/, then mcpb pack)."
    )


def test_no_plugin_file_missing_from_bundle():
    """#576 — the INVERSE tripwire: a NEW plugin scripts/ or vendor/ file that should
    be mirrored to the .mcpb bundle but was NEVER copied ("new-plugin-file-never-
    mirrored"). #564 closed the 'mirrored-but-stale' class (byte-identity above); this
    closes the 'never-mirrored' class — a plugin file with no bundle counterpart means
    the shipped .mcpb is MISSING code a Cowork client's server needs.

    If a NEW plugin file is legitimately plugin-only (a hook/installer that never runs
    in the bundled server), add it to PLUGIN_ONLY_EXCLUDES with a documented reason —
    do NOT delete this assertion."""
    orphans = _discover_missing_from_bundle()
    assert not orphans, (
        "plugin file(s) NOT mirrored to the .mcpb bundle (never-copied) — the shipped "
        f".mcpb is missing code the bundled server needs: {orphans}. Re-sync per "
        "RELEASING.md (rsync scripts/ + vendor/ → mcpb/server/, re-pack), OR if the "
        "file is legitimately plugin-only, add it to PLUGIN_ONLY_EXCLUDES with a reason."
    )
