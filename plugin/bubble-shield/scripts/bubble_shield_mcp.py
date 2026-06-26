#!/usr/bin/env python3
"""Bubble Shield MCP server — anonymised file reading for Cowork ("PII from anywhere").

WHY THIS EXISTS (the Cowork workaround)
---------------------------------------
Cowork RUNS our PostToolUse hook but IGNORES `updatedToolOutput` for built-in
tools like Read/Bash (anthropics/claude-code#32105 — output rewrite only takes
effect for MCP tools). So the ambient "anonymise whatever the agent reads" tier
can't work by rewriting a built-in Read in Cowork.

The fix: make the agent read client data THROUGH this MCP tool instead. An MCP
tool's OWN returned content is what lands in context — so if `bubble_shield_read`
returns already-anonymised text, the agent only ever sees `⟦…⟧` tokens. No
rewrite needed; we control the output at the source. The folder guard
(PreToolUse) still blocks the bare `Read` of protected files, which is what
steers the agent to `bubble_shield_read`.

DESIGN
------
- Pure-stdlib stdio JSON-RPC (MCP). No `mcp` pip package → stays zero-install,
  consistent with the rest of the plugin. Reads requests as line-delimited JSON
  on stdin, writes responses on stdout.
- Reuses the vendored engine + extractor + policy + the warm NER daemon (same
  detection the PostToolUse hook uses), and the same session vault (so tokens
  are consistent across the folder path and this path; reversible locally).
- Fail-safe: if anonymisation can't run, it returns an ERROR, never the raw
  text. (Unlike the ambient hook which fails open — here, returning raw PII
  would defeat the tool's whole purpose, so it fails CLOSED.)

Exposes one tool: bubble_shield_read(path) -> anonymised text of the file.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PLUGIN_ROOT = Path(os.environ.get(
    "CLAUDE_PLUGIN_ROOT", Path(__file__).resolve().parent.parent))
_HERE = Path(__file__).resolve().parent
BUBBLE_SHIELD_HOME = Path(os.environ.get("BUBBLE_SHIELD_HOME", Path.home() / ".bubble_shield"))
VAULT_DIR = BUBBLE_SHIELD_HOME / "vaults"

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "bubble_shield", "version": "1.0.0"}

# Prefix added by the OCR extractor to signal OCR-sourced text to callers.
_OCR_TAG = "[OCR]"
_OCR_QUALITY_NOTE = (
    "ℹ️ Ce document a été lu via OCR (PDF scanné) — relecture humaine conseillée "
    "pour les champs critiques (noms, dates, numéros). La mise en page peut être "
    "partiellement altérée.\n\n"
)

TOOLS = [
    {
        "name": "bubble_shield_status",
        "description": (
            "Check the current operational status of Bubble Shield: whether the NER "
            "(GLiNER ML) daemon is active or down, the model name if loaded, whether "
            "the ML pack is installed, and liveness diagnostics (daemon reachable from "
            "this process, LaunchAgent loaded). Use this to confirm that fine-grained "
            "name/address detection is active before processing sensitive documents. "
            "If NER is down, bubble_shield_read will refuse to process documents until "
            "the daemon is re-armed."),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "bubble_shield_read",
        "description": (
            "Read a client file and return it ANONYMISED — names, IBANs, emails and "
            "other identifying data are replaced with reversible ⟦…⟧ tokens before "
            "you see them. Use this INSTEAD of the plain Read tool for any file that "
            "may contain client PII (the bubble_shield guard blocks the raw Read of protected "
            "folders). Handles .pdf, .docx, .txt, .md, .csv, .json. The real values "
            "never enter your context; they stay in a local vault and are restored "
            "when the final answer is handed back to the user."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "Absolute path to the file to read anonymised."}
            },
            "required": ["path"],
        },
    },
    {
        "name": "bubble_shield_anonymize_text",
        "description": (
            "Anonymise a block of text and return it with reversible ⟦…⟧ tokens. Use "
            "this for client data that ISN'T a file on disk — the body of an e-mail "
            "you fetched, a message, pasted content, an API/tool result. Whenever you "
            "are about to read or quote text that may contain a client's name, IBAN, "
            "e-mail, phone, etc., pass it through this first so the real values never "
            "stay in your context. Same local vault as the rest of Bubble Shield, so tokens "
            "are consistent and reversible."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string",
                         "description": "The raw text to anonymise (e.g. an email body)."}
            },
            "required": ["text"],
        },
    },
    {
        "name": "bubble_shield_write",
        "description": (
            "Write a document to disk, restoring the REAL client values from the "
            "⟦…⟧ tokens — locally, WITHOUT showing you the real values. Use this to "
            "produce a finished client document (letter, summary, note) after you've "
            "drafted it using tokens: pass your token-bearing draft and the output "
            "path; Bubble Shield replaces every ⟦…⟧ token with its real value from the vault "
            "and writes the final file. It returns only a success confirmation + the "
            "path — NOT the de-anonymised content — so the client's real data never "
            "enters your context. The end user gets a complete, real document."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "Absolute path to write the final (real-PII) document to."},
                "content": {"type": "string",
                            "description": "Your draft, containing ⟦…⟧ tokens to be restored to real values."}
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "bubble_shield_setup_ml",
        "description": (
            "Install or check the optional on-device ML accuracy pack (better "
            "detection of names/addresses the rules miss). Runs on the user's own "
            "machine, nothing leaves it. action='start' begins the one-time install "
            "in the background (downloads a model, a few hundred MB — takes a few "
            "minutes) and returns immediately; action='status' reports progress "
            "(installing / downloading / ready / error). After 'start', poll 'status' "
            "every ~20s and tell the user in plain language when it's ready. No "
            "Terminal needed — this runs the setup for them."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["start", "status"],
                           "description": "'start' to begin install, 'status' to check progress."}
            },
            "required": ["action"],
        },
    },
    {
        "name": "bubble_shield_setup_ocr",
        "description": (
            "Install or check the optional on-device OCR pack (reads scanned/image PDFs "
            "locally). action='start' begins the one-time install in the background "
            "(downloads ~150MB of Python packages — takes a few minutes) and returns "
            "immediately; action='status' reports progress. After 'start', poll 'status' "
            "every ~20s and tell the user when it's ready. No Terminal needed."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["start", "status"],
                           "description": "'start' to begin install, 'status' to check progress."}
            },
            "required": ["action"],
        },
    },
    {
        "name": "bubble_shield_enable_global",
        "description": (
            "Turn the TRULY GLOBAL 'anonymise PII everywhere' switch on or off — the "
            "machine-wide setting that can't be reached from a folder marker. Use "
            "this when the user wants ambient anonymisation to apply automatically "
            "EVERYWHERE on their machine, not just in folders they mark. It writes "
            "the host config (~/.config/bubble_shield/bubble-shield.json) for them — no "
            "Terminal. action='on' enables, 'off' disables, 'status' reports the "
            "current value. Existing settings (protected folders, etc.) are "
            "preserved."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["on", "off", "status"],
                           "description": "'on'/'off' to set the global switch, 'status' to read it."}
            },
            "required": ["action"],
        },
    },
    {
        "name": "bubble_shield_add_field",
        "description": (
            "Add a custom PII field (regex pattern, GLiNER label, or keep-list entry). "
            "Patterns must be CATEGORY DESCRIPTORS (regex metacharacters like \\d, [A-Z], {5}), "
            "NEVER a real PII value. The guard-rail will refuse any concrete PII instance."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["regex", "gliner_label", "keep"]},
                "entity_type": {"type": "string", "description": "UPPER_SNAKE id e.g. DOSSIER_CODE (regex/gliner only)"},
                "label": {"type": "string", "description": "Human-readable FR label"},
                "pattern": {"type": "string", "description": "For kind=regex: a REGEX TEMPLATE never a real value"},
                "gliner_label": {"type": "string", "description": "For kind=gliner_label: a CATEGORY phrase e.g. 'employer name'"},
                "keep_kind": {"type": "string", "enum": ["phrase", "email_domain", "phone"], "description": "For kind=keep"},
                "keep_value": {"type": "string", "description": "For kind=keep: the firm's OWN non-client identifier"},
                "validator": {"type": "string", "enum": ["none", "luhn", "iban", "isin", "mod97"]},
                "confirm": {"type": "boolean", "description": "Required true to store a kind=keep literal"}
            },
            "required": ["kind"]
        }
    },
    {
        "name": "bubble_shield_list_fields",
        "description": "List active custom PII fields (patterns/labels/keep counts). Never echoes PII instances.",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "bubble_shield_remove_field",
        "description": "Remove a custom PII field from the configuration.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["regex", "gliner_label", "keep"]},
                "entity_type": {"type": "string"},
                "gliner_label": {"type": "string"},
                "keep_kind": {"type": "string", "enum": ["phrase", "email_domain", "phone"]},
                "keep_value": {"type": "string"}
            },
            "required": ["kind"]
        }
    },
]


def _vendor():
    for cand in (PLUGIN_ROOT / "vendor", _HERE / "vendor", _HERE.parent / "vendor"):
        if (cand / "bubble_shield").is_dir():
            return cand
    return PLUGIN_ROOT / "vendor"


def _scripts_dir():
    for cand in (PLUGIN_ROOT / "scripts", _HERE):
        if (cand / "bubble_shield_extract.py").is_file():
            return cand
    return _HERE


def _vault_path() -> Path:
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    mission = os.environ.get("BUBBLE_SHIELD_SESSION", "mcp-session")
    return VAULT_DIR / f"{mission}.vault.json"


_NER_DOWN_ERROR = (
    "⛔ Bubble Shield — NER (détection fine) hors-ligne. "
    "Ce document NE PEUT PAS être certifié sûr en mode dégradé (regex seul) : "
    "des noms en texte libre (ex. DUPONT MARC) ne seraient pas masqués. "
    "Le daemon NER est en cours de réarmement — réessayez dans quelques secondes. "
    "Si le problème persiste, relancez bubble_shield_setup_ml(action='status')."
)


def _try_spawn_daemon_from_mcp() -> None:
    """Best-effort daemon re-arm from the MCP server path. Fails open (never
    blocks the error response). Delegates to posttool_anonymize._try_spawn_daemon
    which already handles the ml.json / venv_python resolution."""
    try:
        sys.path.insert(0, str(_scripts_dir()))
        import posttool_anonymize as _pt
        _pt._try_spawn_daemon()
    except Exception:
        pass  # spawn failure is acceptable; the per-call gate is what enforces safety


def _engine(text_for_daemon: str = "", filename_basename: str = ""):
    """Build the shared engine: regex core + structured_ext form detectors +
    (daemon NER if up) + policy + the consistent per-session vault.
    Reused by every anonymise path.

    Returns (engine, vault_path, daemon_up: bool). The third value lets callers
    surface a degraded-mode warning when the daemon is down (regex-only).

    FIX #257: structured_ext (deterministic FR état-civil FORM recognizers) is now
    always wired in as an extra_detector so it runs in the bubble_shield_read path
    even without the GLiNER daemon. It covers Nom/Prénom, Lieu de naissance, and
    Pièce d'identité label-value lines that GLiNER misses in FORM layouts.

    FIX #280: filename_basename threads the file's basename into make_structured_detector()
    so person-name tokens extracted from the filename (e.g. "DURAND Théophile" from
    "DURAND Théophile - DER 012026.pdf") seed the doc-level repetition pass and catch
    footer boilerplate leaks.  Empty string = no filename seeding (text-only calls)."""
    sys.path.insert(0, str(_vendor()))
    sys.path.insert(0, str(_scripts_dir()))
    from bubble_shield import AnonymizationEngine, Vault
    from bubble_shield import policy as _policy
    from bubble_shield import custom_recognizers as _cr

    # structured_ext: always-on deterministic FR KYC FORM safety net (daemon-independent)
    # fix #280: pass filename_basename so footer/boilerplate name leak is seeded.
    detectors = []
    try:
        from bubble_shield.structured_ext import make_structured_detector
        detectors.append(make_structured_detector(filename_basename=filename_basename))
    except Exception:
        pass  # fail-open: if import fails, continue without it

    daemon_up = False
    try:
        import posttool_anonymize as _pt
        d = _pt._daemon_detector(text_for_daemon)     # None if daemon down → regex only
        if d:
            detectors.append(d)
            daemon_up = True
    except Exception:
        pass

    # #326 — known-PII deny-list: wire in as an extra_recognizer (zero-cost when
    # empty; deterministic masking of cross-session confirmed PII).
    extra_recs = list(_cr.load_custom_recognizers())
    try:
        from bubble_shield.known_pii_recognizer import make_known_pii_recognizer
        kpr = make_known_pii_recognizer()
        if kpr is not None:
            extra_recs.append(kpr)
    except Exception:
        pass  # fail-open: gazetteer failure never breaks anonymisation

    engine = AnonymizationEngine(
        extra_detectors=detectors,
        extra_recognizers=extra_recs,
        match_filter=_policy.make_match_filter(_policy.load_policy()))
    vpath = _vault_path()
    engine.vault = Vault.load(str(vpath)) if vpath.is_file() else Vault(mission=os.environ.get("BUBBLE_SHIELD_SESSION", "mcp-session"))
    return engine, vpath, daemon_up


# ---- custom-field config management (Phase 1) ------------------------------

def _custom_fields_path() -> Path:
    """Resolve custom_fields.json path: env override → vendor dir → ~/.config."""
    override = os.environ.get("BUBBLE_SHIELD_CUSTOM_FIELDS")
    if override:
        return Path(override)
    vendor_path = _vendor() / "bubble_shield" / "custom_fields.json"
    if vendor_path.is_file():
        return vendor_path
    return Path(os.path.expanduser("~/.config/bubble_shield/custom_fields.json"))


def _load_custom_fields() -> dict:
    p = _custom_fields_path()
    if not p.is_file():
        return {"version": 1, "regex_fields": [], "gliner_labels": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "regex_fields": [], "gliner_labels": []}


def _save_custom_fields(cfg: dict) -> None:
    """Atomic write of custom_fields.json (temp file + os.replace)."""
    p = _custom_fields_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def _guard_check(value: str, kind: str, confirm: bool = False) -> dict:
    """Run pii_guard.check_input. Returns {"ok": True} or {"ok": False, "reason": str}.

    Fail-CLOSED: any error becomes a refusal (never silently allow an unchecked
    value into the config)."""
    try:
        sys.path.insert(0, str(_vendor()))
        from bubble_shield import pii_guard
        return pii_guard.check_input(value, kind, confirm=confirm)
    except Exception as e:
        return {"ok": False, "reason": f"guard error: {e}"}


class NERDownError(RuntimeError):
    """Raised by _anonymise_text/_anonymise_file when the NER daemon is offline.

    Callers (the tools/call handler) must convert this to isError:true without
    including any anonymized body or raw PII text — fail-closed contract.
    """


def _anonymise_text(text: str, filename_basename: str = "") -> str:
    """Anonymise a block of text. Used by bubble_shield_anonymize_text and bubble_shield_read.

    FAIL-CLOSED: raises NERDownError when the NER daemon is offline. Regex-only
    mode CANNOT safely catch context-free all-caps name blocks (e.g. 'DUPONT MARC'),
    so returning a partial result would let the agent certify a leaking document.
    The caller must convert NERDownError to isError:true — no anonymized body, no
    raw PII.

    A re-arm spawn is triggered before raising so the NEXT call can succeed.

    fix #280 — filename_basename parameter:
    When provided (always set by _anonymise_file), the structured_ext detector is
    rebuilt with that basename so person-name tokens from the filename are seeded
    into the doc-level repetition pass.
    """
    engine, vpath, daemon_up = _engine(text, filename_basename=filename_basename)
    if not daemon_up:
        # Kick the re-arm so the next call can succeed, then fail this one closed.
        _try_spawn_daemon_from_mcp()
        raise NERDownError(_NER_DOWN_ERROR)
    res = engine.anonymize(text)
    engine.vault.save(str(vpath))
    note = "" if res.safe_to_send else (
        "\n\n[⚠️ Bubble Shield : une relecture humaine est conseillée — "
        "une donnée potentiellement sensible est restée sous le seuil de confiance.]")
    return res.anonymized + note


def _anonymise_file(path: str) -> str:
    """Extract + anonymise a file. Raises on failure (fail-closed).

    fix #280: threads the file basename into the anonymisation engine so that
    person-name tokens extracted from the filename (e.g. "DURAND Théophile" from
    "DURAND Théophile - DER 012026.pdf") are seeded into the doc-level repetition
    pass.  This catches the footer boilerplate leak:
      "Page de signatures complémentaire au document DURAND Théophile - DER 012026..."
    which contains the client's name verbatim but has no label for any content
    recognizer to anchor on.
    """
    sys.path.insert(0, str(_scripts_dir()))
    from bubble_shield_extract import extract_file          # PDF/docx/text → text
    p = Path(os.path.expanduser(path)).resolve()
    if not p.is_file():
        raise FileNotFoundError(f"no such file: {p}")
    text = extract_file(p)                            # fail-closed on scanned PDFs
    return _anonymise_text(text, filename_basename=p.name)


def _ner_status() -> dict:
    """Return NER daemon status + liveness diagnostics. Read-only; triggers a
    best-effort re-arm spawn when the daemon is down (never blocks).

    Returns a dict with keys:
      ner           — "active" | "down"
      model         — model name from ml.json, or null
      ml_pack_installed — bool
      daemon_reachable  — bool (HTTP /health reachable from this process)
      launchagent_loaded — bool (launchctl list shows com.bubbleinvest.bubble-shield-nerd)
      ml_json_exists    — bool (~/.bubble_shield/ml.json present)
    """
    import subprocess as _sp

    ml_json = BUBBLE_SHIELD_HOME / "ml.json"
    ml_pack_installed = ml_json.is_file()

    model_name = None
    if ml_pack_installed:
        try:
            man = json.loads(ml_json.read_text(encoding="utf-8"))
            model_name = man.get("model") or man.get("model_id") or man.get("name")
        except Exception:
            pass

    # Check if /health is reachable from THIS process (no import of posttool_anonymize needed)
    try:
        import urllib.request as _ur
        _ur.urlopen(
            _ur.Request(f"http://127.0.0.1:{int(os.environ.get('BUBBLE_SHIELD_NERD_PORT', '8723'))}/health",
                        method="GET"), timeout=0.5)
        daemon_reachable = True
    except Exception:
        daemon_reachable = False

    # LaunchAgent check (macOS) — non-fatal on non-Mac / error
    launchagent_loaded = False
    try:
        result = _sp.run(
            ["launchctl", "list"],
            capture_output=True, text=True, timeout=2)
        launchagent_loaded = "com.bubbleinvest.bubble-shield-nerd" in result.stdout
    except Exception:
        pass

    ner_active = daemon_reachable
    if not ner_active:
        # Best-effort re-arm (non-blocking)
        _try_spawn_daemon_from_mcp()

    return {
        "ner": "active" if ner_active else "down",
        "model": model_name,
        "ml_pack_installed": ml_pack_installed,
        "daemon_reachable": daemon_reachable,
        "launchagent_loaded": launchagent_loaded,
        "ml_json_exists": ml_pack_installed,
    }


def _deanonymise_to_file(path: str, content: str) -> dict:
    """Restore real values from ⟦…⟧ tokens in `content` and WRITE to `path`.

    CRITICAL: returns only a summary (path + counts) — NEVER the de-anonymised
    text — so the agent never sees the real PII it just produced. Raises if the
    vault is missing (can't restore without it)."""
    engine, vpath, _daemon_up = _engine()
    if not vpath.is_file():
        raise RuntimeError("aucun coffre (vault) pour cette session — "
                           "lis d'abord des données via bubble_shield_read/anonymize_text")
    sys.path.insert(0, str(_vendor()))
    from bubble_shield.vault import TOKEN_RE
    n_tokens = len(set(TOKEN_RE.findall(content)))
    restored = engine.deanonymize(content)
    out = Path(os.path.expanduser(path)).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(restored, encoding="utf-8")
    # how many tokens still remain (unknown to this vault) — surfaced, not the values
    remaining = len(set(TOKEN_RE.findall(restored)))
    return {"path": str(out), "tokens_restored": n_tokens - remaining,
            "tokens_unresolved": remaining, "bytes_written": len(restored.encode("utf-8"))}


# ---- ML accuracy-pack setup (async, host-side) -----------------------------

_SETUP_MARKER = BUBBLE_SHIELD_HOME / "setup.status"          # progress breadcrumb
_SETUP_LOG = BUBBLE_SHIELD_HOME / "setup.log"


def _setup_script() -> Path:
    for cand in (PLUGIN_ROOT / "scripts" / "bubble_shield_setup_ml.py",
                 _HERE / "bubble_shield_setup_ml.py"):
        if cand.is_file():
            return cand
    return PLUGIN_ROOT / "scripts" / "bubble_shield_setup_ml.py"


def _setup_start() -> dict:
    """Spawn the bootstrap DETACHED host-side and return immediately."""
    if (BUBBLE_SHIELD_HOME / "ml.json").is_file():
        return {"state": "ready", "message": "Le pack ML est déjà installé."}
    script = _setup_script()
    if not script.is_file():
        return {"state": "error", "message": f"bootstrap introuvable: {script}"}
    BUBBLE_SHIELD_HOME.mkdir(parents=True, exist_ok=True)
    _SETUP_MARKER.write_text("installing", encoding="utf-8")
    import subprocess
    logf = open(_SETUP_LOG, "a")
    # wrapper writes the final state to the marker so /status can read it
    wrapper = (
        f"import subprocess,sys;"
        f"rc=subprocess.run([sys.executable,{str(script)!r}]).returncode;"
        f"open({str(_SETUP_MARKER)!r},'w').write('ready' if rc==0 else 'error')"
    )
    subprocess.Popen([sys.executable, "-c", wrapper],
                     stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
                     start_new_session=True)
    return {"state": "installing",
            "message": "Installation du pack ML démarrée (téléchargement du modèle, "
                       "quelques minutes). Rappelle bubble_shield_setup_ml(action='status') "
                       "pour suivre."}


def _setup_status() -> dict:
    if (BUBBLE_SHIELD_HOME / "ml.json").is_file():
        return {"state": "ready", "message": "Pack ML prêt — détection fine active."}
    state = _SETUP_MARKER.read_text(encoding="utf-8").strip() if _SETUP_MARKER.is_file() else "absent"
    msgs = {"installing": "Installation en cours (téléchargement du modèle)…",
            "ready": "Pack ML prêt.",
            "error": "L'installation a échoué — voir ~/.bubble_shield/setup.log.",
            "absent": "Pack ML non installé. Lance bubble_shield_setup_ml(action='start')."}
    return {"state": state, "message": msgs.get(state, state)}


# ---- OCR pack setup (async, host-side) --------------------------------------

_OCR_SETUP_MARKER = BUBBLE_SHIELD_HOME / "ocr-setup.status"
_OCR_SETUP_LOG = BUBBLE_SHIELD_HOME / "ocr-setup.log"


def _ocr_setup_script() -> Path:
    for cand in (PLUGIN_ROOT / "scripts" / "bubble_shield_setup_ocr.py",
                 _HERE / "bubble_shield_setup_ocr.py"):
        if cand.is_file():
            return cand
    return PLUGIN_ROOT / "scripts" / "bubble_shield_setup_ocr.py"


def _ocr_setup_start() -> dict:
    if (BUBBLE_SHIELD_HOME / "ocr.json").is_file():
        return {"state": "ready", "message": "Le pack OCR est déjà installé."}
    script = _ocr_setup_script()
    if not script.is_file():
        return {"state": "error", "message": f"script OCR introuvable: {script}"}
    BUBBLE_SHIELD_HOME.mkdir(parents=True, exist_ok=True)
    _OCR_SETUP_MARKER.write_text("installing", encoding="utf-8")
    import subprocess
    logf = open(_OCR_SETUP_LOG, "a")
    wrapper = (
        f"import subprocess,sys;"
        f"rc=subprocess.run([sys.executable,{str(script)!r}]).returncode;"
        f"open({str(_OCR_SETUP_MARKER)!r},'w').write('ready' if rc==0 else 'error')"
    )
    subprocess.Popen([sys.executable, "-c", wrapper],
                     stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
                     start_new_session=True)
    return {"state": "installing",
            "message": "Installation du pack OCR démarrée (téléchargement des paquets, "
                       "quelques minutes). Rappelle bubble_shield_setup_ocr(action='status') "
                       "pour suivre."}


def _ocr_setup_status() -> dict:
    if (BUBBLE_SHIELD_HOME / "ocr.json").is_file():
        return {"state": "ready", "message": "Pack OCR prêt — lecture de PDF scannés active."}
    state = _OCR_SETUP_MARKER.read_text(encoding="utf-8").strip() if _OCR_SETUP_MARKER.is_file() else "absent"
    msgs = {"installing": "Installation en cours (téléchargement des paquets)…",
            "ready": "Pack OCR prêt.",
            "error": "L'installation a échoué — voir ~/.bubble_shield/ocr-setup.log.",
            "absent": "Pack OCR non installé. Lance bubble_shield_setup_ocr(action='start')."}
    return {"state": state, "message": msgs.get(state, state)}


# ---- global "anonymise everywhere" switch (host-side config) ---------------

GLOBAL_CONFIG = Path(os.path.expanduser("~/.config/bubble_shield/bubble-shield.json"))


def _enable_global(action: str) -> dict:
    """Set/read posttool_enabled in the host global config, MERGING (never
    clobbering protected_folders etc.). Runs host-side via the MCP server, so it
    works from Cowork where the agent's own shell can't reach ~/.config."""
    cfg = {}
    if GLOBAL_CONFIG.is_file():
        try:
            cfg = json.loads(GLOBAL_CONFIG.read_text(encoding="utf-8")) or {}
        except Exception:
            cfg = {}
    if action == "status":
        on = bool(cfg.get("posttool_enabled", False))
        return {"state": "on" if on else "off",
                "message": ("La protection globale « partout » est ACTIVE."
                            if on else "La protection globale « partout » est INACTIVE.")}
    cfg.setdefault("protected_folders", cfg.get("protected_folders", []))
    cfg["posttool_enabled"] = (action == "on")
    GLOBAL_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    GLOBAL_CONFIG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    if action == "on":
        return {"state": "on",
                "message": "Protection « partout » ACTIVÉE pour toute la machine. "
                           "Désormais, tout ce que l'assistant lit est anonymisé "
                           "automatiquement, où que ce soit — sans marquer de dossier."}
    return {"state": "off",
            "message": "Protection « partout » désactivée. Les dossiers marqués "
                       "restent protégés."}


# ---- minimal JSON-RPC / MCP plumbing (stdio) -------------------------------

def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _result(id_, result) -> None:
    _send({"jsonrpc": "2.0", "id": id_, "result": result})


def _error(id_, code, message) -> None:
    _send({"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}})


def _handle(req: dict) -> None:
    method = req.get("method")
    id_ = req.get("id")
    params = req.get("params", {}) or {}

    if method == "initialize":
        _result(id_, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        })
    elif method == "notifications/initialized":
        pass  # notification, no response
    elif method == "tools/list":
        _result(id_, {"tools": TOOLS})
    elif method == "tools/call":
        name = params.get("name")
        args = params.get("arguments", {}) or {}

        def ok(text):
            _result(id_, {"content": [{"type": "text", "text": text}]})

        def fail(msg):
            # fail-CLOSED for the anonymise paths: error, never raw content
            _result(id_, {"content": [{"type": "text", "text": msg}], "isError": True})

        try:
            if name == "bubble_shield_status":
                st = _ner_status()
                # Human-readable summary + machine-readable JSON
                ner_label = "✅ NER actif" if st["ner"] == "active" else "⛔ NER hors-ligne"
                model_label = st["model"] or "(inconnu)"
                ml_label = "installé" if st["ml_pack_installed"] else "non installé"
                la_label = "chargé" if st["launchagent_loaded"] else "non chargé"
                summary = (
                    f"{ner_label} | Pack ML : {ml_label} | Modèle : {model_label} | "
                    f"LaunchAgent : {la_label} | "
                    f"daemon /health : {'OK' if st['daemon_reachable'] else 'KO'}\n\n"
                    + json.dumps(st, ensure_ascii=False, indent=2)
                )
                ok(summary)
            elif name == "bubble_shield_read":
                anon = _anonymise_file(args.get("path", ""))
                # Surface OCR quality note when the text was extracted via OCR pack
                if _OCR_TAG in anon[:30]:
                    anon = _OCR_QUALITY_NOTE + anon
                ok(anon)
            elif name == "bubble_shield_anonymize_text":
                ok(_anonymise_text(args.get("text", "")))
            elif name == "bubble_shield_write":
                r = _deanonymise_to_file(args.get("path", ""), args.get("content", ""))
                ok(f"✅ Document écrit : {r['path']} ({r['bytes_written']} octets, "
                   f"{r['tokens_restored']} valeur(s) réelle(s) restaurée(s)"
                   + (f", ⚠️ {r['tokens_unresolved']} jeton(s) inconnu(s) laissé(s) tel quel"
                      if r['tokens_unresolved'] else "") + "). "
                   "Le contenu réel n'est PAS affiché ici (les données du client "
                   "restent hors de ton contexte).")
            elif name == "bubble_shield_setup_ml":
                action = args.get("action", "status")
                r = _setup_start() if action == "start" else _setup_status()
                ok(f"[{r['state']}] {r['message']}")
            elif name == "bubble_shield_setup_ocr":
                action = args.get("action", "status")
                r = _ocr_setup_start() if action == "start" else _ocr_setup_status()
                ok(f"[{r['state']}] {r['message']}")
            elif name == "bubble_shield_enable_global":
                r = _enable_global(args.get("action", "status"))
                ok(f"[{r['state']}] {r['message']}")
            elif name == "bubble_shield_add_field":
                kind = args.get("kind", "")
                if kind == "regex":
                    entity_type = args.get("entity_type", "")
                    pattern = args.get("pattern", "")
                    label = args.get("label", entity_type)
                    validator = args.get("validator")
                    if not entity_type or not pattern:
                        fail("entity_type et pattern sont requis pour kind=regex")
                        return
                    guard = _guard_check(pattern, "regex")
                    if not guard["ok"]:
                        fail(f"⛔ Bubble Shield guard-rail : {guard['reason']}")
                        return
                    import re as _re
                    if not _re.fullmatch(r'[A-Z][A-Z0-9_]{1,31}', entity_type):
                        fail("entity_type doit être [A-Z][A-Z0-9_]{1,31}")
                        return
                    cfg = _load_custom_fields()
                    cfg.setdefault("regex_fields", [])
                    cfg["regex_fields"] = [f for f in cfg["regex_fields"] if f.get("entity_type") != entity_type]
                    entry = {"entity_type": entity_type, "label": label, "pattern": pattern,
                             "ignore_case": False, "cloak": True}
                    if validator and validator != "none":
                        entry["validator"] = validator
                    cfg["regex_fields"].append(entry)
                    _save_custom_fields(cfg)
                    ok(f"✅ Champ regex ajouté : {entity_type} (pattern stocké, valeur jamais journalisée)")
                elif kind == "gliner_label":
                    gliner_label = args.get("gliner_label", "")
                    entity_type = args.get("entity_type", "")
                    if not gliner_label or not entity_type:
                        fail("gliner_label et entity_type sont requis pour kind=gliner_label")
                        return
                    guard = _guard_check(gliner_label, "gliner_label")
                    if not guard["ok"]:
                        fail(f"⛔ Bubble Shield guard-rail : {guard['reason']}")
                        return
                    cfg = _load_custom_fields()
                    cfg.setdefault("gliner_labels", [])
                    cfg["gliner_labels"] = [f for f in cfg["gliner_labels"]
                                            if f.get("entity_type") != entity_type or f.get("label") != gliner_label]
                    cfg["gliner_labels"].append({"label": gliner_label, "entity_type": entity_type})
                    _save_custom_fields(cfg)
                    ok(f"✅ Étiquette GLiNER ajoutée : '{gliner_label}' → {entity_type}")
                elif kind == "keep":
                    keep_kind = args.get("keep_kind", "")
                    keep_value = args.get("keep_value", "")
                    confirm = bool(args.get("confirm", False))
                    if not keep_kind or not keep_value:
                        fail("keep_kind et keep_value sont requis pour kind=keep")
                        return
                    guard = _guard_check(keep_value, "keep", confirm=confirm)
                    if not guard["ok"]:
                        fail(f"⛔ Bubble Shield guard-rail : {guard['reason']}")
                        return
                    sys.path.insert(0, str(_vendor()))
                    from bubble_shield import allowlist as _al
                    _al.add_allowlist_entry(keep_kind, keep_value)
                    ok(f"✅ Entrée liste blanche ajoutée ({keep_kind}) — confirm=True requis et fourni")
                else:
                    fail(f"kind inconnu: {kind}. Valeurs valides: regex, gliner_label, keep")
            elif name == "bubble_shield_list_fields":
                cfg = _load_custom_fields()
                sys.path.insert(0, str(_vendor()))
                try:
                    from bubble_shield import allowlist as _al
                    al_path = _al._firm_config_path()
                    al_data = json.loads(al_path.read_text()) if al_path.is_file() else {}
                except Exception:
                    al_data = {}
                summary = {
                    "regex_fields": len(cfg.get("regex_fields", [])),
                    "gliner_labels": len(cfg.get("gliner_labels", [])),
                    "keep_phrases": len(al_data.get("phrases", [])),
                    "keep_email_domains": len(al_data.get("email_domains", [])),
                    "keep_phones": len(al_data.get("phones", [])),
                    "regex_entity_types": [f["entity_type"] for f in cfg.get("regex_fields", [])],
                    "gliner_label_list": [f["label"] for f in cfg.get("gliner_labels", [])],
                }
                ok(json.dumps(summary, ensure_ascii=False, indent=2))
            elif name == "bubble_shield_remove_field":
                kind = args.get("kind", "")
                if kind == "regex":
                    entity_type = args.get("entity_type", "")
                    cfg = _load_custom_fields()
                    before = len(cfg.get("regex_fields", []))
                    cfg["regex_fields"] = [f for f in cfg.get("regex_fields", []) if f.get("entity_type") != entity_type]
                    _save_custom_fields(cfg)
                    removed = before - len(cfg["regex_fields"])
                    ok(f"✅ {removed} champ(s) regex supprimé(s) pour entity_type={entity_type}")
                elif kind == "gliner_label":
                    gliner_label = args.get("gliner_label", "")
                    entity_type = args.get("entity_type", "")
                    cfg = _load_custom_fields()
                    before = len(cfg.get("gliner_labels", []))
                    cfg["gliner_labels"] = [f for f in cfg.get("gliner_labels", [])
                                            if not (f.get("entity_type") == entity_type and f.get("label") == gliner_label)]
                    _save_custom_fields(cfg)
                    removed = before - len(cfg["gliner_labels"])
                    ok(f"✅ {removed} étiquette(s) GLiNER supprimée(s)")
                elif kind == "keep":
                    keep_kind = args.get("keep_kind", "")
                    keep_value = args.get("keep_value", "")
                    sys.path.insert(0, str(_vendor()))
                    from bubble_shield import allowlist as _al
                    removed = _al.remove_allowlist_entry(keep_kind, keep_value)
                    ok(f"✅ Entrée liste blanche {'supprimée' if removed else 'introuvable'} ({keep_kind})")
                else:
                    fail(f"kind inconnu: {kind}")
            else:
                _error(id_, -32601, f"unknown tool: {name}")
        except NERDownError as e:
            # NER daemon is offline — fail-closed. No anonymized body, no raw PII.
            fail(str(e))
        except Exception as e:
            if name == "bubble_shield_write":
                fail(f"⛔ Bubble Shield n'a pas pu écrire le document : {e}. "
                     "Aucun fichier n'a été produit.")
            else:
                fail(f"⛔ Bubble Shield n'a pas pu anonymiser : {e}. "
                     "Le contenu brut n'est PAS renvoyé (sécurité).")
    elif id_ is not None:
        _error(id_, -32601, f"method not found: {method}")


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        try:
            _handle(req)
        except Exception as e:
            if isinstance(req, dict) and req.get("id") is not None:
                _error(req.get("id"), -32603, f"internal error: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
