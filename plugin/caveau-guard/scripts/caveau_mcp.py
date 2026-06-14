#!/usr/bin/env python3
"""Caveau MCP server — anonymised file reading for Cowork ("PII from anywhere").

WHY THIS EXISTS (the Cowork workaround)
---------------------------------------
Cowork RUNS our PostToolUse hook but IGNORES `updatedToolOutput` for built-in
tools like Read/Bash (anthropics/claude-code#32105 — output rewrite only takes
effect for MCP tools). So the ambient "anonymise whatever the agent reads" tier
can't work by rewriting a built-in Read in Cowork.

The fix: make the agent read client data THROUGH this MCP tool instead. An MCP
tool's OWN returned content is what lands in context — so if `caveau_read`
returns already-anonymised text, the agent only ever sees `⟦…⟧` tokens. No
rewrite needed; we control the output at the source. The folder guard
(PreToolUse) still blocks the bare `Read` of protected files, which is what
steers the agent to `caveau_read`.

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

Exposes one tool: caveau_read(path) -> anonymised text of the file.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PLUGIN_ROOT = Path(os.environ.get(
    "CLAUDE_PLUGIN_ROOT", Path(__file__).resolve().parent.parent))
_HERE = Path(__file__).resolve().parent
CAVEAU_HOME = Path(os.environ.get("CAVEAU_HOME", Path.home() / ".caveau"))
VAULT_DIR = CAVEAU_HOME / "vaults"

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "caveau", "version": "1.0.0"}

TOOLS = [
    {
        "name": "caveau_read",
        "description": (
            "Read a client file and return it ANONYMISED — names, IBANs, emails and "
            "other identifying data are replaced with reversible ⟦…⟧ tokens before "
            "you see them. Use this INSTEAD of the plain Read tool for any file that "
            "may contain client PII (the caveau guard blocks the raw Read of protected "
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
        "name": "caveau_anonymize_text",
        "description": (
            "Anonymise a block of text and return it with reversible ⟦…⟧ tokens. Use "
            "this for client data that ISN'T a file on disk — the body of an e-mail "
            "you fetched, a message, pasted content, an API/tool result. Whenever you "
            "are about to read or quote text that may contain a client's name, IBAN, "
            "e-mail, phone, etc., pass it through this first so the real values never "
            "stay in your context. Same local vault as the rest of Caveau, so tokens "
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
        "name": "caveau_write",
        "description": (
            "Write a document to disk, restoring the REAL client values from the "
            "⟦…⟧ tokens — locally, WITHOUT showing you the real values. Use this to "
            "produce a finished client document (letter, summary, note) after you've "
            "drafted it using tokens: pass your token-bearing draft and the output "
            "path; Caveau replaces every ⟦…⟧ token with its real value from the vault "
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
        "name": "caveau_setup_ml",
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
        "name": "caveau_enable_global",
        "description": (
            "Turn the TRULY GLOBAL 'anonymise PII everywhere' switch on or off — the "
            "machine-wide setting that can't be reached from a folder marker. Use "
            "this when the user wants ambient anonymisation to apply automatically "
            "EVERYWHERE on their machine, not just in folders they mark. It writes "
            "the host config (~/.config/caveau/caveau-guard.json) for them — no "
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
]


def _vendor():
    for cand in (PLUGIN_ROOT / "vendor", _HERE / "vendor", _HERE.parent / "vendor"):
        if (cand / "caveau").is_dir():
            return cand
    return PLUGIN_ROOT / "vendor"


def _scripts_dir():
    for cand in (PLUGIN_ROOT / "scripts", _HERE):
        if (cand / "caveau_extract.py").is_file():
            return cand
    return _HERE


def _vault_path() -> Path:
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    mission = os.environ.get("CAVEAU_SESSION", "mcp-session")
    return VAULT_DIR / f"{mission}.vault.json"


def _engine(text_for_daemon: str = ""):
    """Build the shared engine: regex core + (daemon NER if up) + policy + the
    consistent per-session vault. Reused by every anonymise path."""
    sys.path.insert(0, str(_vendor()))
    sys.path.insert(0, str(_scripts_dir()))
    from caveau import AnonymizationEngine, Vault
    from caveau import policy as _policy

    detectors = []
    try:
        import posttool_anonymize as _pt
        d = _pt._daemon_detector(text_for_daemon)     # None if daemon down → regex only
        if d:
            detectors.append(d)
    except Exception:
        pass

    engine = AnonymizationEngine(
        extra_detectors=detectors,
        match_filter=_policy.make_match_filter(_policy.load_policy()))
    vpath = _vault_path()
    engine.vault = Vault.load(str(vpath)) if vpath.is_file() else Vault(mission=os.environ.get("CAVEAU_SESSION", "mcp-session"))
    return engine, vpath


def _anonymise_text(text: str) -> str:
    """Anonymise a block of text. Used by caveau_anonymize_text and caveau_read."""
    engine, vpath = _engine(text)
    res = engine.anonymize(text)
    engine.vault.save(str(vpath))
    note = "" if res.safe_to_send else (
        "\n\n[⚠️ Caveau : une relecture humaine est conseillée — "
        "une donnée potentiellement sensible est restée sous le seuil de confiance.]")
    return res.anonymized + note


def _anonymise_file(path: str) -> str:
    """Extract + anonymise a file. Raises on failure (fail-closed)."""
    sys.path.insert(0, str(_scripts_dir()))
    from caveau_extract import extract_file          # PDF/docx/text → text
    p = Path(os.path.expanduser(path)).resolve()
    if not p.is_file():
        raise FileNotFoundError(f"no such file: {p}")
    text = extract_file(p)                            # fail-closed on scanned PDFs
    return _anonymise_text(text)


def _deanonymise_to_file(path: str, content: str) -> dict:
    """Restore real values from ⟦…⟧ tokens in `content` and WRITE to `path`.

    CRITICAL: returns only a summary (path + counts) — NEVER the de-anonymised
    text — so the agent never sees the real PII it just produced. Raises if the
    vault is missing (can't restore without it)."""
    engine, vpath = _engine()
    if not vpath.is_file():
        raise RuntimeError("aucun coffre (vault) pour cette session — "
                           "lis d'abord des données via caveau_read/anonymize_text")
    sys.path.insert(0, str(_vendor()))
    from caveau.vault import TOKEN_RE
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

_SETUP_MARKER = CAVEAU_HOME / "setup.status"          # progress breadcrumb
_SETUP_LOG = CAVEAU_HOME / "setup.log"


def _setup_script() -> Path:
    for cand in (PLUGIN_ROOT / "scripts" / "caveau_setup_ml.py",
                 _HERE / "caveau_setup_ml.py"):
        if cand.is_file():
            return cand
    return PLUGIN_ROOT / "scripts" / "caveau_setup_ml.py"


def _setup_start() -> dict:
    """Spawn the bootstrap DETACHED host-side and return immediately."""
    if (CAVEAU_HOME / "ml.json").is_file():
        return {"state": "ready", "message": "Le pack ML est déjà installé."}
    script = _setup_script()
    if not script.is_file():
        return {"state": "error", "message": f"bootstrap introuvable: {script}"}
    CAVEAU_HOME.mkdir(parents=True, exist_ok=True)
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
                       "quelques minutes). Rappelle caveau_setup_ml(action='status') "
                       "pour suivre."}


def _setup_status() -> dict:
    if (CAVEAU_HOME / "ml.json").is_file():
        return {"state": "ready", "message": "Pack ML prêt — détection fine active."}
    state = _SETUP_MARKER.read_text(encoding="utf-8").strip() if _SETUP_MARKER.is_file() else "absent"
    msgs = {"installing": "Installation en cours (téléchargement du modèle)…",
            "ready": "Pack ML prêt.",
            "error": "L'installation a échoué — voir ~/.caveau/setup.log.",
            "absent": "Pack ML non installé. Lance caveau_setup_ml(action='start')."}
    return {"state": state, "message": msgs.get(state, state)}


# ---- global "anonymise everywhere" switch (host-side config) ---------------

GLOBAL_CONFIG = Path(os.path.expanduser("~/.config/caveau/caveau-guard.json"))


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
            if name == "caveau_read":
                ok(_anonymise_file(args.get("path", "")))
            elif name == "caveau_anonymize_text":
                ok(_anonymise_text(args.get("text", "")))
            elif name == "caveau_write":
                r = _deanonymise_to_file(args.get("path", ""), args.get("content", ""))
                ok(f"✅ Document écrit : {r['path']} ({r['bytes_written']} octets, "
                   f"{r['tokens_restored']} valeur(s) réelle(s) restaurée(s)"
                   + (f", ⚠️ {r['tokens_unresolved']} jeton(s) inconnu(s) laissé(s) tel quel"
                      if r['tokens_unresolved'] else "") + "). "
                   "Le contenu réel n'est PAS affiché ici (les données du client "
                   "restent hors de ton contexte).")
            elif name == "caveau_setup_ml":
                action = args.get("action", "status")
                r = _setup_start() if action == "start" else _setup_status()
                ok(f"[{r['state']}] {r['message']}")
            elif name == "caveau_enable_global":
                r = _enable_global(args.get("action", "status"))
                ok(f"[{r['state']}] {r['message']}")
            else:
                _error(id_, -32601, f"unknown tool: {name}")
        except Exception as e:
            if name == "caveau_write":
                fail(f"⛔ Caveau n'a pas pu écrire le document : {e}. "
                     "Aucun fichier n'a été produit.")
            else:
                fail(f"⛔ Caveau n'a pas pu anonymiser : {e}. "
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
