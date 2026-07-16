#!/usr/bin/env python3
"""Black-box tests for the Bubble Shield tripwire (UserPromptSubmit hook): feed it an
event JSON on stdin, assert the soft-nudge / hard-block / no-op output.
Run: python3 test_tripwire.py"""
import json, os, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TRIPWIRE = HERE / "tripwire.py"

# A mod-97-valid French IBAN for the positive tests (do NOT use a real one).
VALID_IBAN = "FR1420041010050500013M02606"


def run(prompt: str, config: dict | None = None) -> dict:
    env = dict(os.environ)
    with tempfile.TemporaryDirectory() as td:
        if config is not None:
            cfgp = Path(td) / "bubble-shield.json"
            cfgp.write_text(json.dumps(config))
            env["BUBBLE_SHIELD_GUARD_CONFIG"] = str(cfgp)
        else:
            env["BUBBLE_SHIELD_GUARD_CONFIG"] = str(Path(td) / "nope.json")
        env["CLAUDE_PROJECT_DIR"] = td
        env["HOME"] = td
        event = {"hook_event_name": "UserPromptSubmit", "prompt": prompt, "cwd": td}
        p = subprocess.run(
            [sys.executable, str(TRIPWIRE)],
            input=json.dumps(event), capture_output=True, text=True, env=env,
        )
    out = p.stdout.strip()
    res = {"_code": p.returncode, "_stdout": out}
    if out:
        try:
            res.update(json.loads(out))
        except json.JSONDecodeError:
            res["_unparsed"] = out
    return res


def is_noop(r: dict) -> bool:
    return r["_stdout"] == ""


def is_nudge(r: dict) -> bool:
    return bool(r.get("hookSpecificOutput", {}).get("additionalContext"))


def is_block(r: dict) -> bool:
    return r.get("decision") == "block"


def no_value_leak(r: dict, *values: str) -> bool:
    """The hook output must never echo back the actual PII value."""
    blob = json.dumps(r)
    return all(v not in blob for v in values)


PASS = 0
FAIL = 0


def check(name: str, cond: bool):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}")


def main():
    print("Bubble Shield tripwire tests")

    # --- No-op cases: benign prompts must pass untouched ---
    check("benign prompt → no-op", is_noop(run("Explain how compound interest works")))
    check("code prompt → no-op", is_noop(run("Write a python function for factorial")))
    check("empty prompt → no-op", is_noop(run("")))

    # --- Raw PII in the chat → soft nudge by default ---
    r = run(f"Voici l'IBAN du client : {VALID_IBAN}, analyse-le")
    check("valid IBAN → nudge", is_nudge(r))
    check("IBAN value not echoed", no_value_leak(r, VALID_IBAN))

    r = run("Le mail du client est jean.dupont@example.com, résume son dossier")
    check("email → nudge", is_nudge(r))
    check("email value not echoed", no_value_leak(r, "jean.dupont@example.com"))

    # NIR must be checksum-valid since #400 (mod-97 key = 97 - body % 97).
    # Synthetic body 1841275116001 → key 26. An invalid key must NOT nudge.
    r = run("Son numéro de sécu : 1 84 12 75 116 001 26")
    check("FR secu number → nudge", is_nudge(r))
    r = run("Son numéro de sécu : 1 84 12 75 116 001 23")
    check("FR secu invalid checksum → no nudge", not is_nudge(r))

    r = run("Tu peux le rappeler au 06 12 34 56 78 ?")
    check("FR phone → nudge", is_nudge(r))

    # --- Attachment-intent phrasing → nudge even without raw PII in text ---
    check("FR 'pièce jointe' → nudge", is_nudge(run("Analyse la pièce jointe stp")))
    check("FR 'ci-joint' → nudge", is_nudge(run("Voici le dossier ci-joint, fais une synthèse")))
    check("EN 'the file I just uploaded' → nudge",
          is_nudge(run("Summarise the file I just uploaded")))

    # --- Invalid IBAN-looking string must NOT fire (precision) ---
    check("invalid IBAN-ish → no-op",
          is_noop(run("Référence interne FR00ABCDEFGHIJ1234567890 du ticket")))

    # --- Hard block opt-in ---
    r = run(f"IBAN {VALID_IBAN}", config={"tripwire_block": True})
    check("tripwire_block=true → block", is_block(r))
    check("block reason present", bool(r.get("reason")))
    check("block: IBAN value not echoed", no_value_leak(r, VALID_IBAN))

    # --- Disabled tripwire → no-op even with PII ---
    check("tripwire_enabled=false → no-op",
          is_noop(run(f"IBAN {VALID_IBAN}", config={"tripwire_enabled": False})))

    # --- Fail-open: malformed config must not block a clean prompt ---
    # (simulate by pointing at an unparseable config via a raw write)
    with tempfile.TemporaryDirectory() as td:
        cfgp = Path(td) / "bad.json"
        cfgp.write_text("{not json")
        env = dict(os.environ)
        env["BUBBLE_SHIELD_GUARD_CONFIG"] = str(cfgp)
        env["CLAUDE_PROJECT_DIR"] = td
        env["HOME"] = td
        p = subprocess.run([sys.executable, str(TRIPWIRE)],
                           input=json.dumps({"prompt": "hello"}),
                           capture_output=True, text=True, env=env)
        check("malformed config + benign prompt → no-op (fail-open)", p.stdout.strip() == "")

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
