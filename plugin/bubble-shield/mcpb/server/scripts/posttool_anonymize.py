#!/usr/bin/env python3
"""Bubble Shield — PostToolUse anonymiser ("PII from anywhere").

WHAT IT DOES
------------
After a tool runs, this hook inspects the tool's RESULT. If the result contains
validated PII, it rewrites the result to an anonymised copy BEFORE Claude sees
it (`updatedToolOutput`, per the Claude Code hooks contract). So client data
Claude reads from ANYWHERE a tool ingests it — a fetched email, a script's
stdout, an opened Excel — gets cloaked in context, not just data in a marked
folder. The shipped folder guard (PreToolUse) blocks marked folders; this scrubs
everything else a tool surfaces.

DETECTION
---------
- ALWAYS the regex/checksum core (IBAN mod-97, ISIN/SIREN Luhn, email, NIR,
  FR phone, amounts, titled names) — fast, no deps.
- PLUS, if the warm NER daemon (bubble_shield_nerd.py) is up on localhost, the GLiNER
  ONNX layer for bare names/addresses regex can't catch (~40ms). If the daemon
  is down, we fall back to regex-only — never block on it.

SAFETY POSTURE (read this — it differs from the folder guard on purpose)
-----------------------------------------------------------------------
- OPT-IN: does nothing unless `posttool_enabled: true` in config/marker.
- FAIL-OPEN: any error → return nothing → Claude gets the ORIGINAL output. This
  is a broad safety net on already-allowed data, NOT the primary control. The
  fail-CLOSED guarantee for KNOWN client folders stays the PreToolUse guard. A
  PostToolUse that failed closed could wedge the whole session.
- PII-PRESENCE GATE: only rewrites output that actually contains validated PII,
  so benign tool output is never mangled (no over-redaction of normal work).
- MCP-TOOL SAFETY: this hook is matched on Read|Bash|mcp__workspace__bash ONLY,
  NOT on arbitrary mcp__.* connectors. Rewriting an MCP connector's result
  (e.g. Gmail search_threads → {threads:[...]}) breaks the Cowork harness, which
  measures the result as a content-block array — a flat rewrite throws
  'H.reduce is not a function' (live-diagnosed 2026-06-14). PII inside MCP
  connector results is handled by the explicit bubble_shield_anonymize_text/bubble_shield_read
  tools instead. The `safe`-shape gate in _extract_text is a second guard.
- Honours the POLICY PANEL (the masquer/conserver entity table the client
  configures) — same toggles as the folder path.
- VAULT per session/mission → the same person gets the same token across the
  folder path and the ambient path; values stay local, restored via deanonymize.

Pure-stdlib for the hook shell. Imports the vendored engine (regex core works
with zero deps). The daemon call is a localhost HTTP GET/POST, no extra deps.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

PLUGIN_ROOT = Path(os.environ.get(
    "CLAUDE_PLUGIN_ROOT", Path(__file__).resolve().parent.parent))

# This file ships in two layouts:
#  - the plugin:  <root>/scripts/posttool_anonymize.py  + <root>/vendor + <root>/scripts/tripwire.py
#  - the stable self-installer dir: <root>/posttool_anonymize.py (flat) + <root>/vendor + <root>/tripwire.py
# Resolve vendor/ and the tripwire module from whichever layout we're in.
_HERE = Path(__file__).resolve().parent
def _vendor_dir() -> Path:
    for cand in (PLUGIN_ROOT / "vendor", _HERE / "vendor", _HERE.parent / "vendor"):
        if (cand / "bubble_shield").is_dir():
            return cand
    return PLUGIN_ROOT / "vendor"
def _tripwire_dir() -> Path:
    for cand in (PLUGIN_ROOT / "scripts", _HERE):
        if (cand / "tripwire.py").is_file():
            return cand
    return _HERE
NERD_PORT = int(os.environ.get("BUBBLE_SHIELD_NERD_PORT", "8723"))
NERD_URL = f"http://127.0.0.1:{NERD_PORT}"

# Where the per-session vault lives so de-anonymisation can restore later.
BUBBLE_SHIELD_HOME = Path(os.environ.get("BUBBLE_SHIELD_HOME", Path.home() / ".bubble_shield"))
VAULT_DIR = BUBBLE_SHIELD_HOME / "vaults"

# Config search mirrors guard.py (global config + in-folder marker compose).
CONFIG_LOCATIONS = [
    os.environ.get("BUBBLE_SHIELD_GUARD_CONFIG"),
    os.path.join(os.environ.get("CLAUDE_PROJECT_DIR", ""), ".bubble-shield.json"),
    os.path.expanduser("~/.config/bubble_shield/bubble-shield.json"),
    os.path.expanduser("~/.bubble-shield.json"),
    str(PLUGIN_ROOT / "config" / "bubble-shield.json"),
]


def _noop() -> None:
    """Emit nothing → tool output unchanged. The fail-open / nothing-to-do path."""
    sys.exit(0)


def _load_config() -> dict:
    for loc in CONFIG_LOCATIONS:
        if not loc:
            continue
        p = Path(loc)
        if p.is_file():
            try:
                return json.loads(p.read_text(encoding="utf-8")) or {}
            except Exception:
                return {}
    return {}


# Keys whose value is the plain-text payload of a simple tool result.
_TEXT_KEYS = ("text", "content", "stdout", "output", "body", "result")


def _extract_text(tool_response):
    """Pull printable text AND decide if the response is a SAFE-to-rewrite plain
    text shape. Returns (text, safe).

    safe is True only when the response IS the text (a bare string, a {type,text}
    block, or a dict whose payload is purely a text string/text-block list).

    safe is False for STRUCTURED results — a dict/array carrying its own schema
    (e.g. Gmail's {threads:[...]}). Rewriting those with a flat string would
    destroy the shape the connector expects and break it
    (the 'H.reduce is not a function' bug). For structured results we leave the
    output untouched; PII inside them is handled by the explicit
    bubble_shield_anonymize_text / bubble_shield_read tools, not the ambient rewrite."""
    if isinstance(tool_response, str):
        return tool_response, True
    if isinstance(tool_response, dict):
        # a plain {type:'text', text:'...'} block, or {text:'...'} alone → safe
        for k in _TEXT_KEYS:
            v = tool_response.get(k)
            if isinstance(v, str) and v:
                # safe only if there's no OTHER structured payload alongside the text
                others = [kk for kk, vv in tool_response.items()
                          if kk not in _TEXT_KEYS and kk != "type"
                          and not (vv is None or isinstance(vv, (str, int, float, bool)))]
                return v, (len(others) == 0)
        # content may be a list of pure text blocks → safe; anything else → not
        c = tool_response.get("content")
        if isinstance(c, list):
            if all(isinstance(b, dict) and b.get("type") == "text" for b in c) and c:
                return "\n".join(b.get("text", "") for b in c), True
    return "", False


def _daemon_up() -> bool:
    """Return True only if the daemon is reachable AND its self-test passes.

    Fix (daemon-onnx-detection): a daemon that answers /health with ok=true
    but returns [] on every /detect call is "healthy but blind" — the worst
    failure mode for a privacy tool.  We gate on self_test=="pass" from the
    /health JSON so a blind daemon is treated as DOWN (fail-closed), not UP.

    Backward compat: old daemon builds return plain "ok" bytes from /health
    (not JSON).  In that case we can't check self_test, so we trust the 200
    (same behaviour as before this fix — no regression on old builds).
    """
    try:
        resp = urllib.request.urlopen(
            urllib.request.Request(NERD_URL + "/health", method="GET"), timeout=0.4)
        body = resp.read(4096)
    except Exception:
        return False
    try:
        data = json.loads(body)
    except Exception:
        # Old build: plain text response — trust the 200
        return True
    if not data.get("ok"):
        return False
    st = data.get("self_test")
    if st is None:
        # warm-up not done yet (--no-warm flag) OR old build without self_test
        # field — treat as UP (fail-open) since the daemon hasn't reported fail.
        return True
    # "pass" → UP; "fail" → DOWN (blind daemon, treat as missing)
    return st == "pass"


def _nerd_script() -> Path | None:
    """Resolve the daemon script path. Tries candidates in priority order:
    1. _HERE/bubble_shield_nerd.py   — flat stable-install layout (~/.claude/bubble-shield/)
    2. PLUGIN_ROOT/scripts/bubble_shield_nerd.py — plugin/dev layout (scripts/ sub-dir)
    3. ~/.claude/bubble-shield/bubble_shield_nerd.py — explicit stable-dir fallback
       (handles Cowork host-session stale-path scenarios where PLUGIN_ROOT points
       at a session dir that has already been cleaned up)
    Returns the first existing Path, or None if none found."""
    _stable = Path.home() / ".claude" / "bubble-shield" / "bubble_shield_nerd.py"
    for cand in (
        _HERE / "bubble_shield_nerd.py",
        PLUGIN_ROOT / "scripts" / "bubble_shield_nerd.py",
        _stable,
    ):
        if cand.is_file():
            return cand
    return None


def _try_spawn_daemon() -> None:
    """If the ML pack is installed but the daemon isn't running, start it
    detached (safety net for the LaunchAgent). Best-effort, never blocks: we
    spawn and return immediately; the model warms in the background, so THIS
    call still falls back to regex, but the NEXT tool result gets ML."""
    manifest = BUBBLE_SHIELD_HOME / "ml.json"
    if not manifest.is_file():
        return  # ML pack not installed → nothing to start
    nerd = _nerd_script()
    if nerd is None:
        return  # can't find daemon script → nothing to spawn
    try:
        man = json.loads(manifest.read_text(encoding="utf-8"))
        vpy = man.get("venv_python")
        if not vpy or not Path(vpy).exists():
            return
        import subprocess
        env = dict(os.environ)
        env["BUBBLE_SHIELD_HOME"] = str(BUBBLE_SHIELD_HOME)
        subprocess.Popen(
            [vpy, str(nerd), "--port", str(NERD_PORT)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, start_new_session=True, env=env)
    except Exception:
        pass  # spawn failure → stay on regex; never break the hook


def _daemon_detector(text: str):
    """Return a text->[Match] detector backed by the warm NER daemon, or None
    if the daemon isn't reachable (fail-open to regex-only). If the daemon is
    down but the ML pack is installed, kick off a detached start for next time."""
    if not _daemon_up():
        _try_spawn_daemon()   # warm it for subsequent calls
        return None  # this call: regex only

    # vendored Match type for the engine
    sys.path.insert(0, str(_vendor_dir()))
    from bubble_shield.recognizers import Match  # noqa

    def detect(t: str):
        try:
            body = json.dumps({"text": t}).encode("utf-8")
            req = urllib.request.Request(
                NERD_URL + "/detect", data=body,
                headers={"Content-Type": "application/json"}, method="POST")
            resp = json.load(urllib.request.urlopen(req, timeout=8))
            out = []
            for m in resp.get("matches", []):
                out.append(Match(
                    start=int(m["start"]), end=int(m["end"]),
                    entity_type=m["entity_type"], value=m["value"],
                    score=float(m.get("score", 0.6)), priority=5))
            return out
        except Exception:
            return []  # a flaky daemon call never breaks the pass
    return detect


def _is_mail_tool(tool_name: str) -> bool:
    tl = tool_name.lower()
    return tl.startswith("mcp__") and (
        "thread" in tl or "message" in tl or "mailbox" in tl
        or "mail" in tl or "gmail" in tl or "imap" in tl)


def _anonymise_json_strings(obj, engine):
    """Walk a JSON structure and anonymise every string value IN PLACE, keeping
    the exact shape. Returns (new_obj, n_changed). This is how mail containment
    preserves the connector's structure (so no H.reduce) while cloaking PII."""
    n = 0
    if isinstance(obj, str):
        if obj.strip():
            red = engine.anonymize(obj).anonymized
            return red, (1 if red != obj else 0)
        return obj, 0
    if isinstance(obj, list):
        out = []
        for v in obj:
            nv, c = _anonymise_json_strings(v, engine); out.append(nv); n += c
        return out, n
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            nv, c = _anonymise_json_strings(v, engine); out[k] = nv; n += c
        return out, n
    return obj, 0


def _try_mail_containment(event, cfg) -> None:
    """SHAPE-VALIDATED mail rewrite. Only acts if the tool_response is exactly the
    shape we know how to rewrite safely; otherwise emits NOTHING (pass-through →
    the PreToolUse steer still applies, connector never breaks). This is the
    'safety net' design: containment when shapes match, graceful no-op when they
    drift — never a malformed rewrite that crashes the connector."""
    tr = event.get("tool_response")
    # The shapes we accept: a dict (the connector's structured result) OR a
    # content-block list. Anything else → bail (don't touch).
    if not isinstance(tr, (dict, list)):
        _noop()
    # Build engine (regex + daemon if up). Daemon text seed: best-effort.
    try:
        engine, vpath = None, None
        sys.path.insert(0, str(_vendor_dir()))
        from bubble_shield import AnonymizationEngine, Vault
        from bubble_shield import policy as _policy
        from bubble_shield import custom_recognizers as _cr
        VAULT_DIR.mkdir(parents=True, exist_ok=True)
        mission = event.get("session_id", "ambient") or "ambient"
        vpath = VAULT_DIR / f"{mission}.vault.json"
        vault = Vault.load(str(vpath)) if vpath.is_file() else Vault(mission=mission)
        eng = AnonymizationEngine(vault=vault,
                                  extra_recognizers=_cr.load_custom_recognizers(),
                                  match_filter=_policy.make_match_filter(_policy.load_policy()))
        new_tr, n = _anonymise_json_strings(tr, eng)
        if n == 0:
            _noop()  # nothing cloaked → leave original untouched
        vault.save(str(vpath))
        # Emit the rewrite in the EXACT same shape we received (dict→dict, list→list).
        # We do NOT coerce to {type,text} — preserving shape is the whole point.
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "updatedToolOutput": new_tr,
                "additionalContext": ("ℹ️ Bubble Shield a anonymisé les données sensibles "
                                      "de ce résultat e-mail (jetons ⟦…⟧)."),
            }
        }))
        sys.exit(0)
    except Exception:
        _noop()  # ANY error → pass through; never crash the connector


def main() -> None:
    raw = sys.stdin.read()
    try:
        event = json.loads(raw) if raw.strip() else {}
    except Exception:
        _noop()

    cfg = _load_config()
    tool_name = event.get("tool_name", "")


    # MAIL CONTAINMENT (shape-validated). Runs INDEPENDENTLY of posttool_enabled;
    # default ON. Anonymises a mail connector's result in place, shape-preserving,
    # bailing to pass-through on any mismatch (fail-safe → never crashes).
    #
    # ⚠️ PROVEN LIMITATION (live probe 2026-06-14): COWORK DOES NOT FIRE
    # PostToolUse ON THIRD-PARTY MCP CONNECTORS (0 of 19 real Gmail calls fired
    # this hook). So in Cowork this NEVER runs on mail — it is effectively
    # CLI-only. Kept on by default because (a) it works + protects in plain
    # Claude Code CLI, and (b) it's fail-safe everywhere. In Cowork, mail relies
    # on the PreToolUse steer (guard.py) + the bubble_shield_read-via-protected-folder
    # path; this hook is not the mail guarantee there. See onboarding skill.
    if _is_mail_tool(tool_name) and cfg.get("mail_containment", True):
        _try_mail_containment(event, cfg)
        return  # always exits (rewrite or _noop)

    # Everything below is the AMBIENT ML rewrite, which IS opt-in.
    if not cfg.get("posttool_enabled", False):
        _noop()  # OPT-IN: off by default

    # Scope: which tools to scrub (default = ingestion tools, not Claude's own writes)
    matcher_substrs = cfg.get("posttool_tools")  # optional explicit allow-list
    if matcher_substrs and not any(s in tool_name for s in matcher_substrs):
        _noop()

    text, safe = _extract_text(event.get("tool_response", ""))
    # Only rewrite SIMPLE text results. A structured result (e.g. an MCP
    # connector returning {threads:[...]}) must be left untouched — replacing it
    # with a flat string destroys the shape the connector expects and breaks it
    # ('H.reduce is not a function'). PII inside structured results is covered by
    # the explicit bubble_shield_anonymize_text / bubble_shield_read tools.
    if not safe or not text or len(text) > 200_000:
        _noop()

    # CHEAP GATE: only do real work if validated PII is present.
    sys.path.insert(0, str(_tripwire_dir()))
    try:
        from tripwire import _find_pii
        if not _find_pii(text):
            # regex pre-check found nothing high-signal. Still run the engine ONLY
            # if the daemon is up (ML may catch a bare name regex can't); else bail.
            daemon = _daemon_detector(text)
            if daemon is None:
                _noop()
        else:
            daemon = _daemon_detector(text)
    except Exception:
        _noop()

    # Build the engine: regex core + structured_ext (always-on) +
    # (optional) daemon NER + policy panel + vault.
    #
    # FIX #257: structured_ext is now wired in so the deterministic FR KYC FORM
    # safety net (Nom/Prénom/Lieu de naissance/Pièce d'identité label lines) fires
    # even when the GLiNER daemon is down. Fail-open: if import fails, falls back.
    try:
        sys.path.insert(0, str(_vendor_dir()))
        from bubble_shield import AnonymizationEngine, Vault
        from bubble_shield import policy as _policy
        from bubble_shield import custom_recognizers as _cr

        VAULT_DIR.mkdir(parents=True, exist_ok=True)
        mission = event.get("session_id", "ambient") or "ambient"
        vault_path = VAULT_DIR / f"{mission}.vault.json"
        vault = Vault.load(str(vault_path)) if vault_path.is_file() else Vault(mission=mission)

        pol = _policy.load_policy()                 # the masquer/conserver table
        match_filter = _policy.make_match_filter(pol)

        # Always include structured_ext (daemon-independent deterministic recognizers)
        extra = []
        try:
            from bubble_shield.structured_ext import make_structured_detector
            extra.append(make_structured_detector())
        except Exception:
            pass  # fail-open
        if daemon:
            extra.append(daemon)

        engine = AnonymizationEngine(
            vault=vault, extra_detectors=extra,
            extra_recognizers=_cr.load_custom_recognizers(), match_filter=match_filter)
        res = engine.anonymize(text)

        # If nothing was actually cloaked, don't rewrite (avoid churn).
        if res.anonymized == text:
            _noop()

        vault.save(str(vault_path))   # persist so the answer can be de-anonymised

        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "updatedToolOutput": {"type": "text", "text": res.anonymized},
                "additionalContext": (
                    "ℹ️ Bubble Shield a anonymisé des données sensibles dans ce résultat "
                    "(jetons ⟦…⟧). Les vraies valeurs restent locales ; la réponse "
                    "sera restaurée au moment de la rendre à l'utilisateur."),
            }
        }))
        sys.exit(0)
    except Exception:
        _noop()   # fail-open: on ANY error Claude gets the original output


if __name__ == "__main__":
    main()
