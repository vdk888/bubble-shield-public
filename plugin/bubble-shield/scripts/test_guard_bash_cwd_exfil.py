#!/usr/bin/env python3
"""Regression matrix for the block_bash cwd-anchoring exfil vulnerability
(confirmed 2026-06-30 against a real client avis d'impôt).

THE BUG: the Bash command scan built its protected-path needles ONLY from
`_discover_marker_roots(cwd)`, which is cwd-anchored. When the bash tool's cwd was
an unrelated session/workspace root (the normal Cowork case), marker discovery
found nothing, the needle-set was empty, and a command containing the LITERAL
ABSOLUTE PATH to a marked file (`tesseract /a/b/Dossier/avis.jpg stdout`) sailed
through silently — `block_bash:true` became a no-op. The client re-extracted 1372
chars of real PII via `mcp__workspace__bash` + tesseract this way.

THE FIX: the Bash scan now extracts path-shaped tokens from the command string
itself and runs each through the SAME robust per-path marker walk-up the file-tool
path uses (`decide_block` → `_find_marker_root`), which is cwd-INDEPENDENT for
absolute paths. The legacy cwd needle scan is kept as defense-in-depth.

This file locks the fix in permanently. It is the SECOND time a bash-coverage gap
bit a real client — it must stay bulletproof. Run: python3 test_guard_bash_cwd_exfil.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
GUARD = HERE / "guard.py"

# An UNRELATED cwd, same shape as a Cowork session/workspace root that is NOT the
# connected protected client subfolder. This is the cwd that used to break the scan.
UNRELATED_CWD = "/Users"


def run(event: dict, home: str | None = None) -> str:
    """Run the guard with NO global config (marker-only, the Cowork deployment)."""
    env = dict(os.environ)
    td = tempfile.mkdtemp()
    env["BUBBLE_SHIELD_GUARD_CONFIG"] = str(Path(td) / "none.json")
    env["CLAUDE_PROJECT_DIR"] = td
    env["HOME"] = home or td
    p = subprocess.run([sys.executable, str(GUARD)],
                       input=json.dumps(event), capture_output=True, text=True, env=env)
    return p.stdout.strip()


def decision(out: str) -> str:
    if not out:
        return "allow-noop"
    try:
        return json.loads(out)["hookSpecificOutput"]["permissionDecision"]
    except Exception:
        return f"PARSE_ERROR:{out[:80]}"


def bash(cmd: str, cwd: str, home: str | None = None) -> str:
    return decision(run(
        {"tool_name": "mcp__workspace__bash", "tool_input": {"command": cmd}, "cwd": cwd},
        home))


def run_raw(raw: str, home: str | None = None) -> tuple[str, int]:
    """Run the guard with an ARBITRARY (possibly malformed) stdin payload.
    Returns (stdout, exit_code) so we can assert the fail-CLOSED contract:
    a crash must NOT exit 1 with empty output (that fails OPEN per hook
    semantics) — it must exit 0 with a deny JSON."""
    env = dict(os.environ)
    td = tempfile.mkdtemp()
    env["BUBBLE_SHIELD_GUARD_CONFIG"] = str(Path(td) / "none.json")
    env["CLAUDE_PROJECT_DIR"] = td
    env["HOME"] = home or td
    p = subprocess.run([sys.executable, str(GUARD)],
                       input=raw, capture_output=True, text=True, env=env)
    return p.stdout.strip(), p.returncode


PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}")


def main():
    print("Bubble Shield guard — block_bash cwd-exfil regression matrix")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # marker on the parent folder; the secret lives in a subfolder
        marked_root = root / "client-dossiers"
        protected = marked_root / "Dupont"
        protected.mkdir(parents=True)
        clean = protected / "clean"
        clean.mkdir()
        (marked_root / ".bubble-shield.json").write_text(json.dumps({
            "block_bash": True,
            "allow_paths": [str(clean)],
            "allow_extensions": [".anon.txt"],
        }), encoding="utf-8")
        secret = protected / "avis_impot.jpg"
        secret.write_text("FAKE PII", encoding="utf-8")
        clean_file = clean / "out.anon.txt"
        clean_file.write_text("safe", encoding="utf-8")
        anon_root = protected / "x.anon.txt"
        anon_root.write_text("safe", encoding="utf-8")
        unrelated = root / "photos"
        unrelated.mkdir()
        unrel_file = unrelated / "beach.jpg"
        unrel_file.write_text("x", encoding="utf-8")

        # --- DISPATCH STEP 4 MATRIX: absolute path to a marked file, varying cwd ---
        check("(a) cwd = the marked folder → deny",
              bash(f"tesseract {secret} stdout", str(protected)) == "deny")
        check("(b) cwd = a close ancestor → deny",
              bash(f"tesseract {secret} stdout", str(marked_root)) == "deny")
        check("(c) cwd = COMPLETELY UNRELATED [the exfil case] → deny",
              bash(f"tesseract {secret} stdout", UNRELATED_CWD) == "deny")

        # --- The client's EXACT three commands, cwd unrelated (the real attack) ---
        check("client: file <abs> → deny", bash(f"file {secret}", UNRELATED_CWD) == "deny")
        check("client: hex dump <abs> → deny", bash(f"xxd {secret} | head", UNRELATED_CWD) == "deny")
        check("client: tesseract <abs> stdout → deny",
              bash(f"tesseract {secret} stdout", UNRELATED_CWD) == "deny")

        # --- Quoting / escaping variants (paths with spaces), cwd unrelated ---
        spacey = protected / "avis impot 2024.jpg"
        spacey.write_text("PII", encoding="utf-8")
        check("double-quoted path w/ space → deny",
              bash(f'tesseract "{spacey}" stdout', UNRELATED_CWD) == "deny")
        check("single-quoted path w/ space → deny",
              bash(f"file '{spacey}'", UNRELATED_CWD) == "deny")
        check("backslash-escaped spaces → deny",
              bash("file " + str(spacey).replace(" ", "\\ "), UNRELATED_CWD) == "deny")

        # --- ~ expansion: HOME points at the marked parent → ~/Dupont/... is the file ---
        check("~/Dupont/avis (HOME=marked parent) → deny",
              bash("file ~/Dupont/avis_impot.jpg", UNRELATED_CWD, home=str(marked_root)) == "deny")

        # --- Relative paths (defense-in-depth via cwd resolution) ---
        check("slash-relative path into marked, cwd=ancestor → deny",
              bash("Dupont/avis_impot.jpg", str(marked_root)) == "deny")
        check("slash-relative path, cwd UNRELATED (resolves outside) → allow",
              bash("Dupont/avis_impot.jpg", UNRELATED_CWD) == "allow-noop")
        # Documented residual: a BARE filename (no slash) with cwd inside the marked
        # folder is ALLOWED (can't distinguish a bare filename from any other word
        # without a real shell lexer; read-tool path still covers the file). This is
        # the explicit, documented policy choice — see guard.py RESIDUAL-PATH POLICY.
        check("bare filename, cwd inside marked (documented residual ALLOW)",
              bash("cat avis_impot.jpg", str(protected)) == "allow-noop")

        # --- Allow cases MUST still pass (no over-blocking of routine shell use) ---
        check("ls (no path) → allow", bash("ls -la", UNRELATED_CWD) == "allow-noop")
        check("git status → allow", bash("git status", UNRELATED_CWD) == "allow-noop")
        check("abs path to UNRELATED file → allow",
              bash(f"cat {unrel_file}", UNRELATED_CWD) == "allow-noop")
        check("allow_paths clean/ file → allow",
              bash(f"cat {clean_file}", UNRELATED_CWD) == "allow-noop")
        check("ext-exempt .anon.txt in marked root → allow",
              bash(f"cat {anon_root}", UNRELATED_CWD) == "allow-noop")
        check("the marker file itself → allow",
              bash(f"cat {marked_root / '.bubble-shield.json'}", UNRELATED_CWD) == "allow-noop")

        # --- allow_paths / ext-exemption is NOT cwd-anchored either (dispatch step 5) ---
        # Same robust per-path resolution as the deny path: an allow_paths/ext-exempt
        # file is cleared regardless of cwd, an under-marker file is denied regardless.
        check("allow_paths resolves with UNRELATED cwd (not cwd-anchored)",
              bash(f"tesseract {clean_file} stdout", UNRELATED_CWD) == "allow-noop")

        # --- per-marker block_bash:false is honoured (marker.example.json documents it) ---
        nb = root / "nb"
        nb.mkdir()
        (nb / ".bubble-shield.json").write_text(json.dumps({"block_bash": False}), encoding="utf-8")
        nbsecret = nb / "s.jpg"
        nbsecret.write_text("PII", encoding="utf-8")
        check("per-marker block_bash:false → bash allowed",
              bash(f"file {nbsecret}", UNRELATED_CWD) == "allow-noop")
        check("...but the Read guard still denies that file (defense intact)",
              decision(run({"tool_name": "Read", "tool_input": {"file_path": str(nbsecret)},
                            "cwd": UNRELATED_CWD})) == "deny")

        # =====================================================================
        # FIX 2 (P0-SEC-2): glob metachars in a PARENT segment must not bypass.
        # A marked folder at .../clients/Dupont; a bash command that references
        # the file via a glob in a segment AT/ABOVE the marked folder used to
        # ALLOW (Path.resolve keeps the literal glob, marker walk-up misses it),
        # while the shell expands it at runtime and reads the file.
        # =====================================================================
        gt = root / "glob-test"
        gclients = gt / "clients"
        gdup = gclients / "Dupont"
        gdup.mkdir(parents=True)
        # marker on clients/ — the glob metachar sits AT/ABOVE this marked folder
        (gclients / ".bubble-shield.json").write_text('{"block_bash":true}', encoding="utf-8")
        gsecret = gdup / "avis.txt"
        gsecret.write_text("Jean Dupont FAKE PII avis", encoding="utf-8")
        gcwd = str(gt)
        litpath = str(gsecret)
        check("FIX2 literal path (control) → deny",
              bash(f"cat {litpath}", gcwd) == "deny")
        check("FIX2 star in parent  clients→cl*  → deny",
              bash(f"cat {gt}/cl*/Dupont/avis.txt", gcwd) == "deny")
        check("FIX2 bare */ in parent → deny",
              bash(f"cat {gt}/*/Dupont/avis.txt", gcwd) == "deny")
        check("FIX2 ? in parent  clients→client?  → deny",
              bash(f"cat {gt}/client?/Dupont/avis.txt", gcwd) == "deny")
        check("FIX2 charclass [c]lients → deny",
              bash(f"cat {gt}/[c]lients/Dupont/avis.txt", gcwd) == "deny")
        check("FIX2 brace {clients} → deny",
              bash(f"cat {gt}/{{clients}}/Dupont/avis.txt", gcwd) == "deny")
        check("FIX2 multi-segment glob clie*/Dup*/avis*.txt → deny",
              bash(f"cat {gt}/clie*/Dup*/avis*.txt", gcwd) == "deny")
        check("FIX2 recursive **/ → deny",
              bash(f"cat {gt}/**/avis.txt", gcwd) == "deny")
        # leaf-only glob where all parent segments are LITERAL must still deny
        # (regression guard — this already worked; must not break).
        check("FIX2 leaf-only glob, literal parents → deny (no regress)",
              bash(f"cat {gdup}/av*.txt", gcwd) == "deny")
        # un-expandable glob near a protected area fails CLOSED: a `?` that
        # matches nothing on disk but whose glob-free prefix sits above the marker
        # must still deny (marker-discovery under the prefix).
        check("FIX2 un-expandable glob near marker → deny (fail-closed)",
              bash(f"cat {gt}/cl?ents/Dupont/zzz_nonexistent_?.txt", gcwd) == "deny")
        # a glob over a TRULY unrelated, unmarked area must still ALLOW (no over-block).
        gphotos = gt / "photos"
        gphotos.mkdir()
        (gphotos / "a.jpg").write_text("x", encoding="utf-8")
        check("FIX2 glob over unmarked area → allow (no over-block)",
              bash(f"cat {gt}/pho*/a.jpg", gcwd) == "allow-noop")

        # =====================================================================
        # FIX 3 (P0-SEC-3): generic mcp__* file tools were matched by the hook
        # matcher but never inspected (only 6 native tools yielded candidates),
        # so mcp__filesystem__read_file(path=<marked>) silently ALLOWED.
        # =====================================================================
        def mcp(tool: str, ti: dict, cwd: str = UNRELATED_CWD) -> str:
            return decision(run({"tool_name": tool, "tool_input": ti, "cwd": cwd}))

        check("FIX3 mcp__filesystem__read_file path=marked → deny",
              mcp("mcp__filesystem__read_file", {"path": str(gsecret)}) == "deny")
        check("FIX3 mcp__fs__read uri=file://marked → deny",
              mcp("mcp__fs__read_text_file", {"uri": f"file://{gsecret}"}) == "deny")
        check("FIX3 mcp tool target= key → deny",
              mcp("mcp__editor__open", {"target": str(gsecret)}) == "deny")
        check("FIX3 mcp tool paths=[marked] (list) → deny",
              mcp("mcp__x__read_many", {"paths": [str(gsecret)]}) == "deny")
        check("FIX3 backstop: unenumerated key holding marked path → deny",
              mcp("mcp__weird__grab", {"nonstandard_key": str(gsecret)}) == "deny")
        # our OWN sanctioned read/write tools must NOT be blocked (safe path)
        check("FIX3 bubble_shield_read (own) → allow (not blocked)",
              mcp("mcp__bubble-shield__bubble_shield_read", {"path": str(gsecret)}) == "allow-noop")
        check("FIX3 bubble_shield_write (own) → allow (not blocked)",
              mcp("mcp__bubble-shield__bubble_shield_write", {"path": str(gsecret)}) == "allow-noop")
        # generic mcp tool on an UNRELATED path must still allow
        check("FIX3 mcp file tool on unmarked path → allow",
              mcp("mcp__filesystem__read_file", {"path": str(gphotos / 'a.jpg')}) == "allow-noop")

    # =====================================================================
    # FIX 1 (P0-SEC-1): the guard must FAIL CLOSED on any uncaught exception.
    # Pre-fix: a malformed event that crashed the decision path exited 1 with
    # NO deny JSON → per hook semantics (exit 1 = non-blocking) the tool RAN.
    # Post-fix: the blanket try/except in main() converts any crash into a
    # deny that exits 0. We assert BOTH the deny decision AND exit code 0.
    # =====================================================================
    def assert_failclosed(name: str, raw: str):
        out, code = run_raw(raw)
        # must NOT be the fail-OPEN shape (empty stdout + nonzero exit)
        ok = code == 0 and decision(out) == "deny"
        check(name + f" (exit={code})", ok)

    assert_failclosed("FIX1 tool_input is a list → deny (was AttributeError exit1)",
                      '{"tool_name":"Read","tool_input":[1,2,3],"cwd":"/tmp"}')
    assert_failclosed("FIX1 cwd is an int → deny (was TypeError exit1)",
                      '{"tool_name":"Bash","tool_input":{"command":"x"},"cwd":12345}')
    assert_failclosed("FIX1 tool_input is a string → deny",
                      '{"tool_name":"Read","tool_input":"boom","cwd":"/tmp"}')
    assert_failclosed("FIX1 event is a bare list → deny",
                      '[1,2,3]')
    # sanity: a WELL-FORMED event with no marked path still ALLOWS (the blanket
    # wrapper must not turn every call into a deny — only genuine failures).
    out, code = run_raw('{"tool_name":"Read","tool_input":{"file_path":"/tmp/unrelated_ok.txt"},"cwd":"/tmp"}')
    check("FIX1 well-formed unrelated read still allows (no over-deny)",
          code == 0 and decision(out) == "allow-noop")

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
