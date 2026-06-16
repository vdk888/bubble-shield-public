#!/usr/bin/env python3
"""Bubble Shield tripwire — UserPromptSubmit hook.

The PreToolUse guard (guard.py) protects files on disk inside a protected
folder. But a user can paste raw client PII straight into the chat box, or
drag-drop / upload a document in the Cowork UI. In that case the content is
injected into the model context *before any tool call*, so PreToolUse never
fires and there is nothing on disk for the anonymise skill to cloak.

This hook is the belt-and-suspenders for that path. UserPromptSubmit is the
ONE hook that runs the moment a prompt is submitted. Its input gives us the
prompt *text* only (no structured access to an uploaded file's bytes — that's
a platform limit, not a choice). So the tripwire does the maximum it can:

  1. Scan the visible prompt text for high-signal raw PII (IBAN, email, FR
     social-security number, phone). If found → nudge Claude to STOP and
     redirect the user to drop the data in the protected folder, where Bubble Shield
     anonymises it for real.
  2. Detect language that implies an attachment/upload ("ci-joint", "pièce
     jointe", "le document que je viens d'uploader", "attached file"…) and,
     since we can't anonymise an upload's content, nudge the same way.

By default this is a SOFT nudge (`additionalContext`), not a hard block:
blocking a UserPromptSubmit *erases the user's prompt* (per Claude Code hook
docs), which is hostile UX and prone to false positives. The nudge steers
Claude's behaviour ("don't analyse raw pasted PII; tell the user to use the
folder") without destroying their message. Hard-block is opt-in via config
(`tripwire_block: true`) for power users / strict-compliance setups.

Output contract (UserPromptSubmit hooks, per Claude Code docs):
  - Soft nudge: exit 0 with {"hookSpecificOutput": {"additionalContext": ...}}
  - Hard block: exit 0 with {"decision": "block", "reason": ...}
  - Nothing to flag: exit 0, no output (prompt proceeds untouched).

Pure stdlib. Fast: it runs on EVERY prompt, so no engine import, no ML.
Fail-OPEN by design: a tripwire is an advisory nudge, not the vault. If it
errors, the prompt must still go through (the real protection is the folder
guard). We never block the user because our own advisory crashed.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

PLUGIN_ROOT = Path(os.environ.get("CLAUDE_PLUGIN_ROOT", Path(__file__).resolve().parent.parent))

CONFIG_LOCATIONS = [
    os.environ.get("BUBBLE_SHIELD_GUARD_CONFIG"),
    os.path.join(os.environ.get("CLAUDE_PROJECT_DIR", ""), ".bubble-shield.json"),
    os.path.expanduser("~/.config/bubble_shield/bubble-shield.json"),
    os.path.expanduser("~/.bubble-shield.json"),
    str(PLUGIN_ROOT / "config" / "bubble-shield.json"),
]

# --- High-signal raw-PII patterns (cheap, validated where a checksum exists) ---
# These are deliberately HIGH precision: the cost of a false positive is an
# unnecessary nudge on a benign prompt, so we only fire on structured PII that
# is unmistakable, not on names (too noisy for a per-prompt advisory).

RE_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
# FR/EU IBAN: 2 letters + 2 digits + up to 30 alnum, spaces tolerated.
RE_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){10,30}\b")
# FR social security ("numéro de sécu"): 13 digits + 2-digit key, spaces tolerated.
RE_SECU = re.compile(r"\b[12][ ]?\d{2}[ ]?\d{2}[ ]?\d{2}[ ]?\d{3}[ ]?\d{3}(?:[ ]?\d{2})?\b")
# FR phone: 0X XX XX XX XX or +33…
RE_PHONE = re.compile(r"(?:\+33|0)\s?[1-9](?:[ .]?\d{2}){4}\b")


def _iban_valid(raw: str) -> bool:
    """mod-97 IBAN check — only count a *valid* IBAN to keep precision high."""
    s = re.sub(r"\s", "", raw).upper()
    if len(s) < 15:
        return False
    rearranged = s[4:] + s[:4]
    digits = ""
    for ch in rearranged:
        if ch.isdigit():
            digits += ch
        elif ch.isalpha():
            digits += str(ord(ch) - 55)
        else:
            return False
    try:
        return int(digits) % 97 == 1
    except ValueError:
        return False


# --- Attachment-intent phrases (FR + EN). We can't read the upload's bytes,
# so phrasing that implies "look at the file I just gave you" is our only signal.
ATTACHMENT_HINTS = [
    "pièce jointe", "piece jointe", "ci-joint", "ci joint", "ci-jointe",
    "le document joint", "le fichier joint", "que je viens d'uploader",
    "que je viens d'envoyer", "le doc que j'ai mis", "le pdf que j'ai",
    "attached", "attachment", "the file i just", "the document i just",
    "i just uploaded", "uploaded file", "the pdf i", "the doc i just",
]


def _load_config() -> dict:
    for loc in CONFIG_LOCATIONS:
        if not loc:
            continue
        p = Path(loc)
        if p.is_file():
            try:
                cfg = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                # Tripwire fails OPEN — a broken config must not block prompts.
                return {}
            return cfg
    return {}


def _find_pii(text: str) -> list[str]:
    """Return a list of PII-type labels found in the text (values NEVER returned)."""
    found: list[str] = []
    for m in RE_IBAN.finditer(text):
        if _iban_valid(m.group(0)):
            found.append("IBAN")
            break
    if RE_EMAIL.search(text):
        found.append("email")
    if RE_SECU.search(text):
        found.append("numéro de sécurité sociale")
    if RE_PHONE.search(text):
        found.append("téléphone")
    return found


def _has_attachment_hint(text: str) -> bool:
    low = text.lower()
    return any(h in low for h in ATTACHMENT_HINTS)


def _nudge_text(pii: list[str], attach: bool) -> str:
    bits = []
    if pii:
        bits.append(
            "Le message de l'utilisateur contient des données client brutes en clair "
            f"({', '.join(sorted(set(pii)))})."
        )
    if attach:
        bits.append(
            "L'utilisateur semble faire référence à une pièce jointe / un document "
            "collé ou uploadé directement dans le chat."
        )
    bits.append(
        "⚠️ Bubble Shield : ces données n'ont PAS été anonymisées (le coffre ne protège que "
        "les fichiers du dossier client protégé sur disque, pas le contenu collé/uploadé "
        "dans le chat). Avant d'analyser ou de retransmettre ces données : dis à "
        "l'utilisateur de DÉPOSER le document dans le sous-dossier client protégé "
        "(p. ex. son Dropbox/Clients), puis de te redemander — Bubble Shield l'anonymisera "
        "automatiquement et tu travailleras sur la copie cloakée. N'analyse pas et ne "
        "renvoie pas la PII brute en l'état."
    )
    return " ".join(bits)


def main() -> None:
    raw = sys.stdin.read()
    try:
        event = json.loads(raw) if raw.strip() else {}
    except Exception:
        sys.exit(0)  # fail open

    prompt = event.get("prompt", "") or ""
    if not prompt:
        sys.exit(0)

    cfg = _load_config()
    if not cfg.get("tripwire_enabled", True):
        sys.exit(0)  # explicitly disabled

    pii = _find_pii(prompt)
    attach = _has_attachment_hint(prompt)
    if not pii and not attach:
        sys.exit(0)  # nothing to flag

    reason = _nudge_text(pii, attach)

    if cfg.get("tripwire_block", False):
        # Hard block (opt-in): erases the prompt, surfaces `reason` to the user.
        print(json.dumps({
            "decision": "block",
            "reason": (
                "🔒 Bubble Shield a détecté des données client brutes dans votre message. "
                "Par sécurité, déposez le document dans votre dossier client protégé "
                "(Bubble Shield l'anonymisera), puis relancez votre demande. "
                "Ne collez pas de données client en clair dans le chat."
            ),
        }))
        sys.exit(0)

    # Default: soft nudge via additionalContext (prompt proceeds, Claude steered).
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": reason,
        }
    }))
    sys.exit(0)


if __name__ == "__main__":
    main()
