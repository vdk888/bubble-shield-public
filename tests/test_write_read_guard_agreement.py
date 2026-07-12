#!/usr/bin/env python3
"""test_write_read_guard_agreement.py — the CI TRIPWIRE for Finding #40's root cause.

`bubble_shield_write` (#40 fix) refuses to write restored real PII to any path a
subsequent agent built-in Read would be ALLOWED on. It decides "is this path
guarded?" via `bubble_shield_mcp._path_is_guarded`. The guard hook decides "should
I block a Read/Bash of this path?" via `guard.decide_block_for_path`. These two
MUST agree on EVERY path, or the write gate and the read guard silently desync and
a leak opens (the exact Finding-#40 shape: a `.bubble-shield.json`-named target was
treated as guarded by the write side while the guard ALLOWED a Read of it).

This test builds a corpus of path CLASSES and asserts, for each:

    _path_is_guarded(p)  ==  (guard DENIES a built-in Read of p)

The "read decision" side is driven through the guard's REAL stdin/stdout PreToolUse
hook contract (a subprocess, exactly as Claude Code invokes it), so this is not a
tautology over the same in-process code — it exercises the actual hook path. If the
two implementations ever drift again (e.g. someone re-hand-copies the logic and
forgets the marker short-circuit), a corpus row flips and this test FAILS. A
built-in self-check (`test_agreement_is_a_real_tripwire`) proves that: it patches
`_path_is_guarded` to the OLD drifted logic and asserts the agreement assertion
would then FAIL on the marker-file row.

Semantics note on the marker file (`.bubble-shield.json`) itself: the guard
ALLOW-reads it (it's the guard's own metadata, no PII — `p.name == MARKER_NAME`
short-circuits to not-blocked). Therefore `_path_is_guarded` MUST return False for
it, so `bubble_shield_write` REFUSES to write restored PII to a marker-named path.
That is the correct/safe outcome: you never want the real document to masquerade as
the guard's own metadata file (which the guard deliberately leaves readable).

All identities SYNTHETIC (Marc DURAND). No real client name — pii-guard would block.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "plugin" / "bubble-shield" / "scripts"
GUARD = SCRIPTS / "guard.py"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import bubble_shield_mcp as mcp  # noqa: E402
import guard as guardmod  # noqa: E402


# ── the READ side: drive the guard's REAL PreToolUse hook (subprocess) ──────────
def _guard_denies_read(path: Path, cwd: str) -> bool:
    """True iff the guard DENIES a built-in Read of `path` — driven through the
    guard's real stdin/stdout hook contract as a subprocess (no in-process import),
    so this is an independent oracle for `_path_is_guarded`, not the same code."""
    env = dict(os.environ)
    env.pop("BUBBLE_SHIELD_GUARD_CONFIG", None)
    # No global config → only in-folder markers protect (the Cowork-native shape).
    env["CLAUDE_PROJECT_DIR"] = tempfile.gettempdir()
    event = {"tool_name": "Read", "tool_input": {"file_path": str(path)}, "cwd": cwd}
    proc = subprocess.run(
        [sys.executable, str(GUARD)],
        input=json.dumps(event), capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, f"guard exited {proc.returncode}: {proc.stderr}"
    out = proc.stdout.strip()
    if not out:
        return False  # no JSON → normal permission flow → not denied by the guard
    decision = json.loads(out)["hookSpecificOutput"]["permissionDecision"]
    return decision == "deny"


class WriteReadGuardAgreementTest(unittest.TestCase):
    """`_path_is_guarded(p)` must equal (guard DENIES a Read of p) for every path."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        base = Path(cls._tmp.name)
        cls.base = base

        # A protected client folder (in-folder marker, Cowork-native), allow-listing
        # `clean/` and ext-exempting `.anon.txt` (mirrors marker.example.json).
        cls.protected = base / "Clients" / "Dossier-Demo"
        cls.protected.mkdir(parents=True)
        (cls.protected / ".bubble-shield.json").write_text(
            json.dumps({"allow_paths": ["clean"], "allow_extensions": [".anon.txt"]}),
            encoding="utf-8",
        )
        (cls.protected / "clean").mkdir()

        # A SECOND protected folder whose marker allow-lists EVERYTHING (".") — the
        # "marker that allow-lists everything" corpus class.
        cls.wideopen = base / "Clients" / "Wide-Open"
        cls.wideopen.mkdir(parents=True)
        (cls.wideopen / ".bubble-shield.json").write_text(
            json.dumps({"allow_paths": ["."]}), encoding="utf-8",
        )

        # A real target OUTSIDE any protected folder, plus a symlink FROM outside
        # pointing INTO the protected folder (symlink-into-protected class).
        cls.outside = base / "scratch"
        cls.outside.mkdir()
        (cls.protected / "avis.txt").write_text("Marc DURAND, IBAN FR76 ...\n")
        cls.symlink_in = cls.outside / "link_into_protected"
        os.symlink(str(cls.protected / "avis.txt"), str(cls.symlink_in))

        # The corpus: (label, path). Each is checked for write/read AGREEMENT.
        cls.corpus = [
            # guarded-root file: under the marker, not exempt → GUARDED / DENY.
            ("guarded_root_file", cls.protected / "resultat-demo.txt"),
            # guarded non-exempt subdir → GUARDED / DENY.
            ("guarded_subdir_file", cls.protected / "sorties" / "out.txt"),
            # allow-listed clean/ subpath → NOT guarded / ALLOW.
            ("allowlisted_clean_subpath", cls.protected / "clean" / "resultat.txt"),
            # ext-exempt path (.anon.txt) → NOT guarded / ALLOW.
            ("ext_exempt_path", cls.protected / "resultat-demo.anon.txt"),
            # path outside any protected folder → NOT guarded / ALLOW.
            ("outside_any_protected", cls.outside / "note.txt"),
            # symlink-into-protected: realpath lands under the marker → GUARDED/DENY.
            ("symlink_into_protected", cls.symlink_in),
            # the marker FILE itself → guard ALLOW-reads it (short-circuit), so
            # NOT guarded — write must REFUSE writing restored PII to a marker path.
            ("marker_file_at_root", cls.protected / ".bubble-shield.json"),
            # a marker-named file in a DEEPER subdir (also short-circuited) → NOT
            # guarded / ALLOW. THIS is the exact Finding-#40 leak row.
            ("marker_file_in_subdir", cls.protected / "sub" / ".bubble-shield.json"),
            # marker that allow-lists everything → any file under it is exempt →
            # NOT guarded / ALLOW.
            ("wideopen_marker_allows_all", cls.wideopen / "anything.txt"),
        ]

    def test_write_and_read_guard_agree_on_every_path(self):
        cwd = str(self.base)
        mismatches = []
        for label, p in self.corpus:
            write_guarded = mcp._path_is_guarded(str(p))
            read_denied = _guard_denies_read(p, cwd)
            if write_guarded != read_denied:
                mismatches.append(
                    f"  {label}: _path_is_guarded={write_guarded} but "
                    f"guard-denies-Read={read_denied}  ({p})"
                )
        self.assertEqual(
            mismatches, [],
            "write gate and read guard DISAGREE (they have desynced — a leak or a "
            "spurious refusal):\n" + "\n".join(mismatches),
        )

    def test_marker_file_row_is_not_guarded(self):
        """Pin the load-bearing Finding-#40 semantics explicitly: a marker-named
        target is NOT guarded (guard allow-reads it), so write REFUSES it."""
        for label in ("marker_file_at_root", "marker_file_in_subdir"):
            p = dict(self.corpus)[label]
            self.assertFalse(
                mcp._path_is_guarded(str(p)),
                f"{label}: a `.bubble-shield.json`-named path must be NOT guarded "
                "(the guard allow-reads it) so write refuses restored PII there",
            )
            self.assertFalse(
                _guard_denies_read(p, str(self.base)),
                f"{label}: guard must ALLOW a Read of a marker-named path",
            )

    def test_agreement_is_a_real_tripwire(self):
        """Prove the agreement assertion is NOT a tautology: re-introduce the OLD
        drifted logic (a hand copy WITHOUT the `p.name == MARKER_NAME` short-circuit)
        and show it makes the marker-file row DISAGREE — i.e. the test would catch a
        future desync. Restores the real function afterwards."""
        p = dict(self.corpus)["marker_file_in_subdir"]

        def _drifted_is_guarded(path: str) -> bool:
            # A faithful copy of the PRE-FIX replica: everything EXCEPT the marker
            # short-circuit. On a `.bubble-shield.json` under a marker it returns
            # True (the bug), while the guard ALLOWS the Read → disagreement.
            g = guardmod
            pp = Path(os.path.expanduser(path)).resolve()
            hit = g._find_marker_root(pp)
            if hit is not None:
                root, mdata = hit
                m_allow_paths = [q for q in (g._norm(x, base=root)
                                             for x in mdata.get("allow_paths", [])) if q]
                if any(g._is_within(pp, ap) for ap in m_allow_paths):
                    return False
                return True   # ← BUG: marker file wrongly reported guarded
            return False

        # Real function agrees (both False); drifted one disagrees (True vs ALLOW).
        read_denied = _guard_denies_read(p, str(self.base))
        self.assertFalse(read_denied, "guard must ALLOW a Read of the marker file")
        self.assertEqual(
            mcp._path_is_guarded(str(p)), read_denied,
            "the REAL (single-source) implementation must AGREE with the guard",
        )
        self.assertNotEqual(
            _drifted_is_guarded(str(p)), read_denied,
            "the OLD drifted logic must DISAGREE — proving this test is a real "
            "tripwire that would fail if the two ever desync again",
        )


if __name__ == "__main__":
    unittest.main()
