#!/usr/bin/env python3
"""Marker-based protection tests for the Bubble Shield guard (the Cowork-native path).

A `.bubble-shield.json` dropped INSIDE a folder protects that folder + everything
under it, discovered by walking up from each target file. No global config, no
~/.config — which is what lets Cowork (sandboxed, can't write dotfile dirs) arm
the guard by writing the marker into a folder the user connected.

Run: python3 test_guard_marker.py
"""
import json, os, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
GUARD = HERE / "guard.py"


def run(event: dict, env_extra: dict | None = None) -> str:
    """Run the guard with NO global config (so only markers are in play)."""
    env = dict(os.environ)
    # Neutralise every global-config location → force marker-only behaviour.
    td = tempfile.mkdtemp()
    env["BUBBLE_SHIELD_GUARD_CONFIG"] = str(Path(td) / "none.json")
    env["CLAUDE_PROJECT_DIR"] = td
    env["HOME"] = td
    if env_extra:
        env.update(env_extra)
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


PASS = FAIL = 0
def check(name, cond):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  ✅ {name}")
    else: FAIL += 1; print(f"  ❌ {name}")


def main():
    print("Bubble Shield guard — marker-based protection")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        client = root / "Souscription Test Client"
        (client / "sub").mkdir(parents=True)
        clean = client / "clean"; clean.mkdir()
        unrelated = root / "Holiday Photos"; unrelated.mkdir()

        # Drop a marker into the client folder (what Cowork does).
        (client / ".bubble-shield.json").write_text(json.dumps({
            "allow_paths": [str(clean)],
            "allow_extensions": [".anon.txt"],
        }), encoding="utf-8")

        der = client / "der.pdf"
        nested = client / "sub" / "kyc.pdf"

        # DENY: read a file inside the marked folder (and nested)
        check("read marked-folder file → deny",
              decision(run({"tool_name": "Read", "tool_input": {"file_path": str(der)}, "cwd": str(root)})) == "deny")
        check("read nested file under marker → deny",
              decision(run({"tool_name": "Edit", "tool_input": {"file_path": str(nested)}, "cwd": str(root)})) == "deny")
        check("write into marked folder → deny",
              decision(run({"tool_name": "Write", "tool_input": {"file_path": str(client / "x.txt")}, "cwd": str(root)})) == "deny")
        check("grep marked dir → deny",
              decision(run({"tool_name": "Grep", "tool_input": {"path": str(client / "sub")}, "cwd": str(root)})) == "deny")

        # ALLOW: the marker file itself is readable (skill needs it)
        check("read the marker file itself → allow",
              decision(run({"tool_name": "Read", "tool_input": {"file_path": str(client / ".bubble-shield.json")}, "cwd": str(root)})) == "allow-noop")
        # ALLOW: clean/ subfolder (per-marker allow_paths) + .anon.txt
        check("read clean/ output → allow",
              decision(run({"tool_name": "Read", "tool_input": {"file_path": str(clean / "der.anon.txt")}, "cwd": str(root)})) == "allow-noop")
        check("read .anon.txt anywhere in folder → allow",
              decision(run({"tool_name": "Read", "tool_input": {"file_path": str(client / "der.anon.txt")}, "cwd": str(root)})) == "allow-noop")
        # ALLOW: a folder WITHOUT a marker is not protected (opt-in / option B)
        check("read unmarked folder → allow",
              decision(run({"tool_name": "Read", "tool_input": {"file_path": str(unrelated / "beach.jpg")}, "cwd": str(root)})) == "allow-noop")

        # Bash: a command touching the marked folder is blocked (discovered via cwd descent)
        check("bash cat into marked folder → deny",
              decision(run({"tool_name": "Bash", "tool_input": {"command": f"cat '{der}'"}, "cwd": str(root)})) == "deny")
        check("bash unrelated → allow",
              decision(run({"tool_name": "Bash", "tool_input": {"command": "ls -la"}, "cwd": str(root)})) == "allow-noop")

        # Corrupt marker still protects (fail-closed)
        (unrelated / ".bubble-shield.json").write_text("{broken", encoding="utf-8")
        check("corrupt marker still protects its folder → deny",
              decision(run({"tool_name": "Read", "tool_input": {"file_path": str(unrelated / "x.pdf")}, "cwd": str(root)})) == "deny")

    # --- RELATIVE allow_paths must resolve against the MARKER folder, not CWD ---
    # (regression for the P1 where _norm resolved "clean" against os.getcwd() so
    # the documented anonymized-output escape-hatch silently never matched.)
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        client = root / "Souscription Rel Client"
        client.mkdir(parents=True)
        clean = client / "clean"; clean.mkdir()
        # allow_paths is RELATIVE ("clean") — the marker documents it as relative
        # to THIS marker's own folder.
        (client / ".bubble-shield.json").write_text(json.dumps({
            "allow_paths": ["clean"],
        }), encoding="utf-8")
        # Run the guard from an UNRELATED cwd (the real-world case: workspace root
        # ≠ connected client folder). Pre-fix, _norm("clean") became <cwd>/clean
        # and never matched <client>/clean → the hatch was dead.
        far_cwd = str(root.parent)

        check("relative allow_paths 'clean' → file in clean/ ALLOWED (cwd unrelated)",
              decision(run({"tool_name": "Read",
                            "tool_input": {"file_path": str(clean / "der.anon.txt")},
                            "cwd": far_cwd})) == "allow-noop")
        check("relative allow_paths → secret.pdf outside clean/ still DENIED",
              decision(run({"tool_name": "Read",
                            "tool_input": {"file_path": str(client / "secret.pdf")},
                            "cwd": far_cwd})) == "deny")
        # bash path routes through the same decide_block → relative hatch works there too
        check("bash into clean/ (relative allow_paths) → ALLOWED",
              decision(run({"tool_name": "Bash",
                            "tool_input": {"command": f"cat '{clean / 'der.anon.txt'}'"},
                            "cwd": far_cwd})) == "allow-noop")

        # SAFETY: a symlink inside clean/ pointing at a PROTECTED file must still
        # DENY — .resolve() follows it back out to the protected target. The hatch
        # must open the anonymized-output folder, not a hole into the raw data.
        secret = client / "secret.pdf"; secret.write_text("RAW PII", encoding="utf-8")
        evil = clean / "leak.pdf"
        try:
            evil.symlink_to(secret)
            check("symlink in clean/ → protected file still DENIED (resolve follows out)",
                  decision(run({"tool_name": "Read",
                                "tool_input": {"file_path": str(evil)},
                                "cwd": far_cwd})) == "deny")
        except OSError:
            check("symlink in clean/ (skipped — no symlink support)", True)

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
