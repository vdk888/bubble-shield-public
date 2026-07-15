#!/usr/bin/env python3
"""Bubble Shield MCP server — anonymised file reading for Cowork ("PII from anywhere").

WHY THIS EXISTS (the Cowork workaround)
---------------------------------------
Cowork RUNS our PostToolUse hook but IGNORES `updatedToolOutput` for built-in
tools like Read/Bash (anthropics/claude-code#32105 — output rewrite only takes
effect for MCP tools). So the ambient "anonymise whatever the agent reads" tier
can't work by rewriting a built-in Read in Cowork.

The fix: make the agent read client data THROUGH this MCP tool instead. An MCP
tool's OWN returned content is what lands in context. `bubble_shield_read` serves
a pre-computed masked shadow when the file is already indexed (the agent sees
only `⟦…⟧` tokens); on a brand-new / not-yet-indexed file it returns the RAW
extracted text once (the background sweep masks it afterwards) — so a first read
of a fresh document is NOT guaranteed masked, and `bubble_shield_anonymize_text`
is the fail-closed always-mask path when a guarantee is needed. The folder guard
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
import re
import sys
from pathlib import Path

PLUGIN_ROOT = Path(os.environ.get(
    "CLAUDE_PLUGIN_ROOT", Path(__file__).resolve().parent.parent))
_HERE = Path(__file__).resolve().parent
BUBBLE_SHIELD_HOME = Path(os.environ.get("BUBBLE_SHIELD_HOME", Path.home() / ".bubble_shield"))
VAULT_DIR = BUBBLE_SHIELD_HOME / "vaults"

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "bubble_shield", "version": "1.0.0"}


def _mail_enabled() -> bool:
    """Mail path (IMAP/Gmail triage) is DISABLED in the shipped product — V1 is
    docs-only. The code is KEPT IN RESERVE (option A: gate, don't delete) behind
    this off-by-default env flag so it can be re-enabled without a re-deploy."""
    return os.environ.get("BUBBLE_SHIELD_ENABLE_MAIL") == "1"

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
            "Read a client file the sanctioned way (the bubble_shield guard blocks the "
            "plain Read of protected folders). Handles .pdf, .docx, .txt, .md, .csv, "
            ".json. This is a FAST hash→serve read with no models at read time:\n"
            "• Already-indexed file → returns it ANONYMISED (reversible ⟦…⟧ tokens); "
            "the real values stay in a local vault and are restored only when the final "
            "answer is handed back to the user.\n"
            "• Brand-new / not-yet-indexed file → a background sweep hasn't masked it "
            "yet, so this returns the RAW extracted text ONCE (and queues the file for "
            "the sweep). So do NOT assume a first read of a fresh document is masked. "
            "If you need a guarantee that PII is masked before you use the content "
            "(e.g. a client demo, or a doc you know is new), pass what you got through "
            "bubble_shield_anonymize_text — that runs the models and fails closed "
            "(always masks or errors, never raw)."),
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
        "name": "bubble_shield_list",
        "description": (
            "List the files and subfolders INSIDE a protected client folder so you "
            "can DISCOVER what's there and pick the right file to work on — the plain "
            "Read/Grep/Bash tools are blocked on protected folders, so use THIS to see "
            "what a folder contains. Returns each entry's NAME, type (file/dir), and "
            "for files the extension, inferred modality (pdf, scan, spreadsheet, …) and "
            "byte size, so you can choose 'the PDF', 'the scan', or 'the newest'. "
            "NON-recursive: lists only the immediate children (call again on a subfolder "
            "to go deeper). Entry NAMES are returned IN CLEAR (unmasked) — a folder/file "
            "name is a navigation label the user already owns and sees on their own "
            "machine, so you can navigate and reference folders/files BY NAME. NEVER "
            "returns file CONTENT — for that, use bubble_shield_read, which masks "
            "PII in the file's content before you see it."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "folder": {"type": "string",
                           "description": "Absolute path to a protected folder (or a "
                                          "subfolder of one) to list."}
            },
            "required": ["folder"],
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
            "Install or check ALL on-device models in ONE pass — GLiNER + OpenAI "
            "Privacy Filter + OCR (docling) + Gemma (the de-pollution judge + "
            "degraded-form masker, the largest model). Runs on the user's own "
            "machine, nothing leaves it. action='start' begins the one-time install "
            "in the background (downloads ~5-6 GB total, Gemma being the biggest — a "
            "few minutes) and returns immediately; models already on disk are "
            "SKIPPED. action='status' reports a PER-MODEL state, naming each model "
            "(GLiNER / OpenAI-PF / OCR / Gemma) with present / downloading / ready / "
            "error — and only reports 'ready' when EVERY model (Gemma included) is "
            "done. After 'start', poll 'status' every ~20s and tell the user in "
            "plain language when it's ready. No "
            "Terminal needed — this runs the full setup for them, so the client is "
            "never asked to install a model later."),
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
        "name": "bubble_shield_add_known_pii",
        "description": (
            "Ajoute un mot que le client signale comme MANQUÉ (un nom/valeur que Bubble Shield "
            "n'a pas masqué) à la liste connue (gazetteer) — il sera DÉSORMAIS TOUJOURS masqué "
            "dans tous les documents. À utiliser quand le client dit 'tu as oublié X'. "
            "⚠️ Ce mot sera masqué partout où il apparaît — si c'est un mot COURANT (un prénom "
            "très répandu, un mot du dictionnaire), cela peut SUR-MASQUER du texte légitime ; "
            "demande confirmation au client avant d'ajouter un mot ambigu."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "value": {"type": "string",
                          "description": "Le mot/valeur EXACT que le client signale comme manqué."},
                "entity_type": {"type": "string",
                                "description": "Type UPPER_SNAKE, ex. NOM, ADRESSE, EMAIL. Défaut NOM (le cas courant : un nom manqué)."},
                "confirm": {"type": "boolean",
                            "description": "Requis true. Poka-yoke : le client doit avoir été prévenu que ce mot sera masqué PARTOUT (risque de sur-masquage si mot courant)."}
            },
            "required": ["value", "confirm"]
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

# Mail path (IMAP/Gmail triage) — DISABLED from the shipped product (V1 is
# docs-only: "shield the documents in a protected folder", nothing else) but
# KEPT IN RESERVE (option A: gate, don't delete). These tool defs are only
# appended to TOOLS when BUBBLE_SHIELD_ENABLE_MAIL=1, so they're invisible/
# unreachable via tools/list in a shipped build. See _mail_enabled().
_MAIL_TOOLS = [
    {
        "name": "bubble_shield_mail_read",
        "description": (
            "Read e-mail ANONYMISED. Bubble Shield fetches the messages ITSELF over "
            "IMAP (host-side) and returns each one with names, IBANs, e-mails and "
            "other identifying data already replaced by reversible ⟦…⟧ tokens — the "
            "raw e-mail NEVER enters your context. Use this INSTEAD of a Gmail/mail "
            "connector for any mailbox that may contain client PII: the connector is "
            "removed from the trust path entirely. Same fail-CLOSED guarantee as "
            "bubble_shield_read — if the NER daemon is down, this REFUSES rather than "
            "return raw e-mail. Uses the same local vault as files, so a client masked "
            "in a PDF gets the SAME token in their e-mail (cross-source consistency). "
            "Each message block STARTS with a 'UID: <n>' line — a stable mailbox-local "
            "identifier (not PII). Pass that exact UID as the 'uid' of a decision to "
            "bubble_shield_mail_apply to label/archive/reply that SAME message; never "
            "invent a UID. "
            "Credentials live host-side (~/.bubble_shield/mail.json) and are never "
            "shown to you."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "IMAP search criterion: UNSEEN, ALL, SEEN, "
                                         "'FROM \"x@y.fr\"', etc. Default ALL."},
                "max": {"type": "integer",
                        "description": "Max messages to fetch (most-recent first, capped at 50). Default 10."},
                "since": {"type": "string",
                          "description": "Optional IMAP date filter 'dd-Mon-yyyy' e.g. '01-Jul-2026'."}
            },
            "required": [],
        },
    },
    {
        "name": "bubble_shield_mail_apply",
        "description": (
            "Apply triage DECISIONS to e-mail host-side over IMAP — add Gmail labels, "
            "archive (remove \\Inbox), and/or create a reply DRAFT — WITHOUT the raw "
            "e-mail or the client's real values ever entering your context. This is the "
            "symmetric mutation counterpart to bubble_shield_mail_read. Pass a list of "
            "decisions, each keyed by message UID (from mail_read). For a draft, write "
            "the body USING ⟦…⟧ tokens (body_tokens): Bubble Shield restores the real "
            "values from the local vault in-memory and puts them into the Gmail draft — "
            "the restored text is NEVER shown to you. It returns ONLY a per-decision "
            "success/fail count, never any body. STRUCTURAL guarantees: it can NEVER "
            "send (draft-only, no SMTP), NEVER delete (archive is the only removal), and "
            "refuses more than 60 mutations in one call. Every action is journalled "
            "host-side (uid + labels only, no PII). Credentials live host-side and are "
            "never shown to you."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "decisions": {
                    "type": "array",
                    "description": "List of per-message triage decisions to apply.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "uid": {"type": "string",
                                    "description": "Message UID (from bubble_shield_mail_read)."},
                            "add_labels": {"type": "array", "items": {"type": "string"},
                                           "description": "Gmail labels to ADD (e.g. ['🔴 Clients'])."},
                            "remove_labels": {"type": "array", "items": {"type": "string"},
                                              "description": "Gmail labels to REMOVE from this message — use to CORRECT a mistagged mail or CHANGE a category (remove the wrong label, add the right one in the same decision). Removing a label only un-tags; it never deletes the message. Do NOT put '\\Inbox' here — use 'unarchive' for that."},
                            "archive": {"type": "boolean",
                                        "description": "If true, remove \\Inbox (archive the message). This is the only removal allowed."},
                            "unarchive": {"type": "boolean",
                                          "description": "If true, ADD \\Inbox back (bring an archived message back INTO the inbox) — the inverse of archive, e.g. if a mail was archived by mistake."},
                            "draft": {
                                "type": "object",
                                "description": "Optional reply draft to create for this message.",
                                "properties": {
                                    "to": {"type": "string",
                                           "description": "Recipient (may itself be a ⟦…⟧ token to restore)."},
                                    "in_reply_to": {"type": "string",
                                                    "description": "Message-Id to thread the reply to (In-Reply-To/References)."},
                                    "subject": {"type": "string",
                                                "description": "Draft subject (may contain ⟦…⟧ tokens)."},
                                    "body_tokens": {"type": "string",
                                                    "description": "Draft body written WITH ⟦…⟧ tokens — restored to real values in-memory, never shown to you."}
                                },
                                "required": ["body_tokens"],
                            }
                        },
                        "required": ["uid"],
                    }
                }
            },
            "required": ["decisions"],
        },
    },
]

if _mail_enabled():
    TOOLS = TOOLS + _MAIL_TOOLS


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

# P0 SECURITY FIX (#589) — see ZeroDetectionError above for the full root cause.
# This message now fires ONLY for the GARBLED/low-quality split of
# zero_detection (see _text_quality_gate below) — a substantial doc where the
# extracted text itself is too degraded (OCR noise, broken extraction) for the
# recognizers to have had a fair shot. The genuinely-clean-prose split returns
# normally with _ZERO_DETECTION_CLEAN_NOTE instead of raising this.
_ZERO_DETECTION_ERROR = (
    "⛔ Bubble Shield n'a pas pu analyser ce document de façon fiable (texte "
    "illisible / extraction dégradée — probablement un PDF scanné/image ou une "
    "extraction OCR de mauvaise qualité). Le contenu n'est PAS renvoyé ; "
    "relisez le fichier original."
)

# P0 SECURITY FIX (#589) — TEXT-QUALITY GATE (Joris, validated on real data
# 2026-07-07). A "zero_detection" verdict (masking COMPLETED, substantial doc,
# entity_count==0) is ambiguous by candidate count alone: a garbage/OCR-broken
# extraction can produce a stray false candidate while genuinely clean prose
# produces none — the count doesn't separate "recognizers had nothing to find"
# from "recognizers never had a fair shot at real text". What DOES separate
# them, live-measured on real doc pairs:
#   CLEAN prose:   real_word_ratio=0.84  avg_word_len=6.4  nonword_pct=0.7
#   GARBAGE/OCR:   real_word_ratio=0.08  avg_word_len=2.4  nonword_pct=13.3
# Huge margin. Thresholds below sit well clear of both ends, with extra
# headroom on the clean side so a legit number/table-heavy CGP doc (real
# words, just also lots of digits/figures) is NOT falsely refused —
# real_word_ratio + avg_word_len are the PRIMARY signals (a table doc still
# has plenty of real words of normal length); nonword_pct is SECONDARY
# (symbol/glyph noise is the strongest OCR-garbage tell, but a legit doc with
# some punctuation/currency signs must not trip it alone).
# Tune here if a real clean-but-numeric doc ever trips this — these are the
# ONLY three numbers that decide the split.
_QUALITY_MIN_REAL_WORD_RATIO = 0.40
_QUALITY_MIN_AVG_WORD_LEN = 3.5
_QUALITY_MAX_NONWORD_PCT = 8.0

# P0 #589-B — structured-form fingerprint. A liasse fiscale / CERFA / bilan is a
# high-recall-risk document whose columnar extraction defeats regex+NER anchors, so a
# masked_ok result on it cannot be trusted (see the 2026-07-08 incident). We detect the
# DOCUMENT CLASS by French fiscal/KYC form-number fingerprints — a nameable, explainable
# signal (the quality gate's nonword score does NOT separate these: a tax form of numbers
# reads as clean prose). At >= _FORM_MARKER_MIN distinct markers, escalate to Gemma.
_FORM_MARKER_MIN = 3
_STRUCTURED_FORM_MARKERS = [
    # Form numbers — NO \b word boundary (glued/degraded extraction defeats \b; that
    # gluing is the exact incident failure mode). Match the form-number token even when
    # fused to adjacent text: "resultat2033B", "N°2065-SD".
    re.compile(r"N°\s?20\d{2}(?:-[A-Z]{1,3})?"),          # N° 2065-SD, N° 2033
    re.compile(r"20(?:3[0-9]|5[0-9]|65)-?[A-Z]{1,3}"),    # 2033-B, 2058-A, glued "2033B" (hyphen optional for glue-tolerance; suffix letter still required so bare years don't match)
    re.compile(r"CERFA", re.I),
    re.compile(r"liasse", re.I),
    re.compile(r"ETATS?\s+FISCAUX", re.I),
    # LABEL markers — a bilan/compte de résultat often has NO form numbers, only these
    # standard headings. Each is specific enough that 3 DISTINCT of them (the >=3 floor)
    # signals a structured fiscal document, not ordinary prose.
    re.compile(r"BILAN\s+(?:ACTIF|PASSIF|SIMPLIFI)", re.I),
    re.compile(r"COMPTE\s+DE\s+R[ÉE]SULTAT", re.I),
    re.compile(r"CAPITAUX\s+PROPRES", re.I),
    re.compile(r"IMMOBILISATIONS?\s+(?:CORPORELLES|INCORPORELLES|FINANCI)", re.I),
    re.compile(r"R[ÉE]GIME\s+R[ÉE]EL", re.I),
    re.compile(r"IMP[ÔO]T\s+SUR\s+LES\s+SOCI[ÉE]T[ÉE]S", re.I),
]

def _is_structured_form(text: str) -> bool:
    """True when `text` looks like a French fiscal/KYC structured form (liasse,
    CERFA, bilan). Counts DISTINCT form-number fingerprints; fires at
    _FORM_MARKER_MIN. Pure — no I/O. See #589-B."""
    if not text:
        return False
    hits = sum(1 for rx in _STRUCTURED_FORM_MARKERS if rx.search(text))
    if hits >= _FORM_MARKER_MIN:
        return True
    # A single marker regex can match many times (a real liasse repeats 2033-x); also
    # count total distinct matches of the numeric-form pattern as a fallback signal.
    nums = set(_STRUCTURED_FORM_MARKERS[1].findall(text))
    return (hits + max(0, len(nums) - 1)) >= _FORM_MARKER_MIN

# A "real word" for this gate: alphabetic (letters only, any Unicode letter —
# so accented French text counts), at least 2 characters. Deliberately
# excludes pure-digit tokens (a table of numbers is real DATA but shouldn't
# inflate "real word" count) and single letters (OCR noise is full of them).
_REAL_WORD_RE = re.compile(r"^[^\W\d_]{2,}$", re.UNICODE)
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
# Characters that are expected in normal prose/tables and must NOT count as
# "nonword" noise: alnum + whitespace + common punctuation/currency/typography.
_EXPECTED_CHARS = set(".,;:!?'\"()-/€%&@°#*+=_[]«»…–—")

_ZERO_DETECTION_CLEAN_NOTE = (
    "\n\n[⚠️ Bubble Shield : aucune donnée identifiante détectée — cela ne "
    "garantit PAS l'absence de PII. Sur un document de ce type, « rien "
    "trouvé » peut signifier qu'un nom ou une adresse est passé inaperçu. "
    "Une relecture humaine est requise avant envoi.]")


def _text_quality_gate(text: str) -> bool:
    """Return True when `text` is clean-enough prose for a zero_detection
    verdict to be trusted as "genuinely nothing to find" rather than
    "extraction too degraded for detectors to have had a fair shot".

    Computes real_word_ratio, avg_word_len, nonword_pct on `text` and compares
    against the module-top calibrated constants. Refuses (returns False) if
    ANY of the three signals is on the bad side of its threshold — see the
    constants' comment above for the calibration data and rationale.
    """
    tokens = _TOKEN_RE.findall(text or "")
    if not tokens:
        # No tokens at all can't be "clean prose" — but this path is only ever
        # reached for a substantial_text doc (>=8 words / >=40 chars per
        # engine.py), so an empty token list here would itself be suspicious.
        return False

    real_words = [t for t in tokens if _REAL_WORD_RE.match(t)]
    real_word_ratio = len(real_words) / len(tokens)
    avg_word_len = (sum(len(t) for t in real_words) / len(real_words)) if real_words else 0.0

    total_chars = len(text)
    nonword_chars = sum(
        1 for c in text
        if not (c.isalnum() or c.isspace() or c in _EXPECTED_CHARS)
    )
    nonword_pct = (nonword_chars / total_chars * 100.0) if total_chars else 0.0

    if real_word_ratio < _QUALITY_MIN_REAL_WORD_RATIO:
        return False
    if avg_word_len < _QUALITY_MIN_AVG_WORD_LEN:
        return False
    if nonword_pct > _QUALITY_MAX_NONWORD_PCT:
        return False
    return True


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


def _apply_negative_filters(matches, allowlist=None, known_pii_path=None):
    """#348 — apply the three negative filters (org allowlist, common-words,
    safe-list) with the gazetteer-ALWAYS-WINS precedence rule applied to ALL of
    them, not just the safe-list step.

    Spec rule: a value confirmed in the gazetteer (is_known_pii) must STAY MASKED
    regardless of any negative list. Pre-fix the exemption lived only in the
    safe-list step, so the allowlist and common-word steps could un-mask a
    gazetteer-confirmed value (a real PII leak).

    Approach: PARTITION up-front — pull out every match whose value is in the
    gazetteer (FORCE-KEPT, exempt from all negative filters), run allowlist +
    common-words + safe-list ONLY on the remainder, then re-add the exempt
    matches (original order preserved). Every step is fail-open: a throwing
    filter never breaks anonymisation (it just keeps the spans).

    `known_pii_path` is for TESTS (point at a tmp gazetteer); in production it is
    None so is_known_pii reads the default store — the correct daemon behaviour.
    """
    # 1) partition: gazetteer-confirmed values are exempt from ALL negative steps
    exempt_idx = set()
    try:
        from bubble_shield.known_pii_store import is_known_pii
        for i, m in enumerate(matches):
            try:
                if is_known_pii(getattr(m, "value", ""), path=known_pii_path):
                    exempt_idx.add(i)
            except Exception:
                pass  # fail toward masking: on error, treat as non-exempt (still
                      # subject to filters, which themselves fail-open to keeping)
    except Exception:
        pass  # gazetteer unavailable → no exemptions, filters still run fail-open

    remainder = [m for i, m in enumerate(matches) if i not in exempt_idx]

    # 2) org/firm/regulator allowlist (CORUM/AMF/Orias…)
    if allowlist is not None:
        try:
            remainder = allowlist.filter(remainder)
        except Exception:
            pass  # flaky filter never breaks anonymise

    # 3) common-words — drop NOM spans that are ordinary French words GLiNER
    #    mis-flags as names (marchés, investissements…). Exact-token list only.
    try:
        from bubble_shield import common_words as _cw
        remainder = _cw.filter_matches(remainder)
    except Exception:
        pass  # flaky filter never breaks anonymise

    # 4) safe-list — drop NOM spans a reviewer marked "never mask". (Gazetteer
    #    values were already partitioned out above, so this step only sees the
    #    remainder; the redundant in-step guard is gone — precedence is uniform.)
    try:
        from bubble_shield import safe_words as _sw
        def _safe_keep(m):
            if getattr(m, "entity_type", "") != "NOM":
                return True
            return not _sw.is_safe(getattr(m, "value", ""))
        remainder = [m for m in remainder if _safe_keep(m)]
    except Exception:
        pass  # flaky filter never breaks anonymise

    # 5) re-assemble: keep original order — exempt matches stay where they were,
    #    surviving remainder matches keep their relative position too.
    kept_remainder_ids = {id(m) for m in remainder}
    return [m for i, m in enumerate(matches)
            if i in exempt_idx or id(m) in kept_remainder_ids]


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

    # #348 — composed match_filter: policy THEN the deployment allowlist (and,
    # in Tasks 2-3, common-words + safe-list). Each step is fail-open: a flaky
    # allowlist must never break anonymisation (mirrors engine.py:218-221).
    def _composed_match_filter():
        base = _policy.make_match_filter(_policy.load_policy())
        try:
            from bubble_shield.allowlist import load_deployment_allowlist
            al = load_deployment_allowlist()
        except Exception:
            al = None
        def _f(matches):
            kept = base(matches)
            return _apply_negative_filters(kept, allowlist=al)
        return _f

    engine = AnonymizationEngine(
        extra_detectors=detectors,
        extra_recognizers=extra_recs,
        match_filter=_composed_match_filter())
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


# P0 SECURITY FIX (#589) — zero-detection on a SUBSTANTIAL document is NOT safe
# to return. Root cause (live-confirmed): _anonymise_text failed closed only
# when the NER daemon was DOWN, but when the engine ran fine and found ZERO
# detections on a substantial doc (engine.py's verdict_state=="zero_detection"),
# it still returned res.anonymized — which on a zero-detection result IS THE
# RAW INPUT TEXT — plus a soft "please review" note. A note is not containment:
# the raw PII is already in the model's context. This leaked a real client's raw
# PDF (43KB, 4 raw phone numbers, zero tokens) in a live session with the daemon
# UP and healthy.
class ZeroDetectionError(RuntimeError):
    """Raised by _anonymise_text when a SUBSTANTIAL document (engine.py's
    AnonymizationResult.verdict_state == "zero_detection") yields zero
    detections AND the extracted text itself fails the text-quality gate
    (_text_quality_gate — see its module-top constants/comment). "Found
    nothing" cannot be certified "safe" when the text was too garbled/degraded
    for the recognizers to have had a fair shot — so this is a hard refusal,
    not a soft note appended to the raw text.

    NOT raised when zero_detection fires on genuinely CLEAN prose — that is
    the honest "no PII in this document" case (GLiNER had real text and
    confidently found nothing) and returns normally with
    _ZERO_DETECTION_CLEAN_NOTE instead. See the text-quality gate block in
    _anonymise_text for the clean/garbled split.

    Distinct from NERDownError (daemon offline) — this fires with the daemon
    UP and healthy, on the engine's own honest verdict. Distinct from the
    "nothing_to_do" state (trivially short/empty input, e.g. "ok" or a bare
    date) which is NOT gated here — see AnonymizationResult.substantial_text
    in engine.py (>=8 words AND >=40 chars) for the exact boundary.

    Callers (the tools/call handler) must convert this to isError:true without
    including any anonymized body or raw PII text — fail-closed contract,
    same shape as NERDownError.
    """


# P0 SECURITY FIX (#589) — STRUCTURAL TRIPWIRE: fail closed whenever masking did
# NOT PROVABLY COMPLETE, regardless of cause. Root cause of the live P0 leak
# session (a 43KB PDF + a .docx, ZERO masking tokens,
# isError=false, ZERO audit "anonymize" entries for the read): a masking run
# silently failed to complete, yet the RAW extracted text was still returned.
# The two error classes above (NERDownError, ZeroDetectionError) each cover ONE
# known way completion can fail (daemon offline / substantial-doc-zero-hits).
# This is the CATCH-ALL for every other way `res` could be something other than
# a genuinely-completed AnonymizationResult by the time code reaches `return
# res.anonymized + note`: engine.anonymize() returning None/a malformed object,
# a monkeypatched/mocked engine in a future refactor, a partial object missing
# verdict_state, or any other "looked fine at a glance but never finished."
#
# THE KEY DISTINCTION (do not confuse with ZeroDetectionError): this is NOT
# about "zero PII was found" — a real completed run with verdict_state
# 'nothing_to_do' or 'zero_detection' (handled above) still returns / is
# handled by its own dedicated gate. This fires ONLY when the run itself is
# not verifiably complete: `res` is missing entirely, or `res.verdict_state`
# is not one of the engine's own canonical states. A valid state means
# AnonymizationResult's own property machinery evaluated cleanly on a real
# result object — i.e. engine.anonymize() ran to completion and returned a
# real result.
_VALID_VERDICT_STATES = frozenset(
    {"leak", "low_confidence", "zero_detection", "nothing_to_do", "masked_ok"})

_MASKING_INCOMPLETE_ERROR = (
    "⛔ Bubble Shield n'a pas pu certifier qu'un masquage complet a eu lieu sur "
    "ce document — le résultat de l'anonymisation est absent ou invalide. Par "
    "sécurité, AUCUN contenu n'est renvoyé (le texte brut n'est jamais renvoyé "
    "quand le masquage n'a pas pu être vérifié comme terminé). Relancez la "
    "lecture ; si le problème persiste, contactez le support."
)


class MaskingIncompleteError(RuntimeError):
    """Raised by _anonymise_text when engine.anonymize() did not provably run
    to completion — i.e. `res` is not a real AnonymizationResult carrying a
    known-valid `verdict_state`.

    THE STRUCTURAL TRIPWIRE (#589): this is the fail-closed backstop for every
    return path in _anonymise_text that is NOT already covered by NERDownError
    (daemon offline) or ZeroDetectionError (substantial doc, zero hits, but a
    COMPLETED run). It fires when completion itself cannot be verified — e.g.
    `res` is None, or `res.verdict_state` is missing/not one of the engine's
    canonical states — NOT when completion succeeded and simply found no PII
    ('nothing_to_do' / a completed 'zero_detection' both still return/are
    handled by their own gate; see the KEY DISTINCTION note above this class).

    Callers (the tools/call handler) must convert this to isError:true without
    including any anonymized body or raw PII text — fail-closed contract, same
    shape as NERDownError/ZeroDetectionError.
    """


# P0 #589-B — a STRUCTURED FORM (liasse/CERFA, detected by _is_structured_form)
# needs a deeper, local Gemma second pass: degraded columnar extraction on these
# forms can hide entities from the fast pass while the doc still reads as clean
# prose to the quality gate, so a completed masked_ok/low_confidence verdict on
# a structured form cannot be trusted at face value. We escalate BECAUSE the
# fast pass is unreliable on this doc — so ANY failure of the escalation itself
# (daemon down/unreachable, timeout, non-200, malformed JSON, or empty spans on
# a substantial form) must fail closed. NEVER fall back to res.anonymized here.
_STRUCTURED_FORM_UNVERIFIED_ERROR = (
    "Bubble Shield a identifié un formulaire structuré (liasse/CERFA) nécessitant une "
    "seconde passe approfondie, mais celle-ci n'a pas pu s'exécuter de façon fiable. "
    "Le contenu n'est PAS renvoyé ; relisez le fichier original."
)


class StructuredFormUnverifiedError(RuntimeError):
    """#589-B — a structured form was detected but the Gemma second pass could not be
    trusted (daemon down/timeout/malformed, or empty spans on a substantial form). We
    escalated BECAUSE the fast pass is unreliable on this doc, so we MUST NOT fall back
    to it. Callers convert this to isError:true with NO body."""


_GEMMA_PORT = 8724


def _structured_form_note() -> str:
    return ("\n\n[⚠️ Bubble Shield : formulaire structuré (liasse/CERFA) — une seconde "
            "passe approfondie (locale) a été appliquée. Traitement plus long pour ce "
            "document. Une relecture humaine reste conseillée avant envoi.]")


# #589-F (2026-07-15) — the second-pass /extract_pii timeout. Was a hard 30s,
# which is MARGINAL on a real multi-page liasse: measured 9.1s / 21.0s / >30s
# (timeout) for the SAME ~35k-char doc across runs — pure load variance on the
# single serial MLX worker. A marginal timeout makes certification a COIN FLIP,
# and a timed-out structured form is retried EVERY sweep forever, burning ~30s of
# the serial worker per retry without ever completing — strictly worse than
# letting the call finish once. 120s = 4× the worst measurement. Env-tunable per
# deployment (a Mac-mini indexer can afford more; an interactive client may want
# less): BUBBLE_SHIELD_GEMMA_EXTRACT_TIMEOUT.
_GEMMA_EXTRACT_TIMEOUT_S = float(
    os.environ.get("BUBBLE_SHIELD_GEMMA_EXTRACT_TIMEOUT", "120"))


def _gemma_extract_call(text: str):
    """POST text to the local Gemma daemon /extract_pii. Returns the spans list.
    Raises on any transport/HTTP/parse failure (caller fails closed)."""
    import urllib.request, json as _json
    data = _json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{_GEMMA_PORT}/extract_pii", data=data,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=_GEMMA_EXTRACT_TIMEOUT_S) as r:
        payload = _json.loads(r.read().decode("utf-8"))
    if not payload.get("ok"):
        raise RuntimeError("gemma extract not ok")
    return list(payload.get("spans", []))


def _gemma_second_pass(res, engine) -> str:
    """#589-B — on a structured form, use Gemma to find PII the fast pass missed and mask
    it into the SAME vault. Fail-closed on ANY failure (never return the fast-pass body).

    Note-honesty fix (reviewer flag, post-#589-B): the "seconde passe approfondie"
    note is appended HERE, and ONLY when at least one span was actually applied to
    the output. Keeping the append inside this function (rather than gating at the
    call site) keeps the "did we actually mask anything" fact local to where it's
    computed, so callers can't drift out of sync with it.

    Final-review fix (#589-B): the sub-120-char carve-out for the fail-closed check
    below has been REMOVED. A structured form applying zero masking now fails closed
    at ANY length — including short bodies, which previously fell through and
    returned an unmasked body silently. Since the note above is only appended when
    `applied` is True, and the function now always raises when `applied` is False,
    the note-is-absent-without-a-span invariant holds trivially (there is no longer
    a code path that returns un-noted, unmasked output).

    Fail-open hole fix (reviewer flag, #589-B): the fail-closed check is evaluated
    AFTER the masking loop on whether masking was actually APPLIED, not merely on
    whether Gemma returned a non-empty spans list. A non-empty spans list whose
    values don't textually match res.anonymized (stale/hallucinated/malformed
    spans) must fail closed exactly like empty spans — both mean "nothing was
    verified/masked" on a substantial form.
    """
    try:
        spans = _gemma_extract_call(res.anonymized)
    except Exception as e:
        # Gemma ACTUALLY FAILED (down/timeout/non-200/malformed). The escalation we
        # required did not run → we cannot trust the fast pass on a form → fail closed.
        # This is the #589 guarantee and is UNCHANGED.
        raise StructuredFormUnverifiedError(_STRUCTURED_FORM_UNVERIFIED_ERROR) from e
    out = res.anonymized
    applied = False
    for sp in spans:
        val, typ = sp.get("text", ""), sp.get("type", "MOT")
        if val and val in out:
            token = engine.vault.token_for(val, typ)
            out = out.replace(val, token)
            applied = True
    if applied:
        out += _structured_form_note()
        return out

    # ── applied == 0: Gemma RAN SUCCESSFULLY but found nothing to add. ────────────
    # Two very different situations hide behind "0 spans applied", and the old code
    # conflated them into a blanket fail-closed (which stranded EVERY form the fast
    # pass had already fully masked — a liasse whose PII GLiNER+regex already caught
    # could NEVER be certified, because Gemma correctly had nothing left to add):
    #
    #   (a) VERIFIED-CLEAN — the fast pass ALREADY masked real PII on this form
    #       (entity_count > 0) and left no residual, and Gemma (which ran fine)
    #       confirms nothing was missed. The form IS protected. Returning it is
    #       correct, NOT a leak — the whole point of the second pass (catch what the
    #       fast pass MISSED) is satisfied: it missed nothing.
    #
    #   (b) SUSPICIOUS-EMPTY — the fast pass found ~nothing on a SUBSTANTIAL form
    #       (zero_detection / entity_count == 0). On a structured form that is the
    #       #589 danger: degraded columnar extraction can hide entities from BOTH
    #       passes, so "clean" here is untrustworthy. This MUST still fail closed.
    #
    # So: fail closed ONLY in case (b). This preserves the #589 protection exactly
    # (a form the fast pass missed still fails closed) while letting a fully-masked
    # form through (the actual bug). fail-toward-masking: any ambiguity → (b).
    fast_pass_masked_real_pii = (res.entity_count > 0 and not res.has_residual)
    if fast_pass_masked_real_pii:
        # (a) verified-clean: fast pass covered it, Gemma confirmed no misses.
        return out + _structured_form_note()
    # (b) fast pass found ~nothing on a form → cannot trust "clean" → fail closed.
    raise StructuredFormUnverifiedError(_STRUCTURED_FORM_UNVERIFIED_ERROR)


def _gemma_additive_pass(res, engine) -> str:
    """PROSE, all-mode — Gemma is ADDITIVE, never a refusal.

    REFINEMENT (2026-07-11): unlike _gemma_second_pass (structured forms, fail-CLOSED),
    prose in all-mode uses Gemma only to ADD masking on top of the GLiNER+regex floor.
    The GLiNER+regex-masked body (`res.anonymized`) is the FLOOR that is ALWAYS returned:

      - Gemma reachable + finds extra spans that textually match → those extra values are
        ALSO masked into the SAME vault (higher recall on prose).
      - Gemma UNREACHABLE (any transport/HTTP/parse error) → FAIL-OPEN: return the
        GLiNER+regex floor. A Gemma outage must NEVER refuse a well-masked normal letter
        (the #589 over-refusal bug this function fixes).
      - Gemma reachable but applies ZERO extra spans → FAIL-OPEN: return the floor. On
        prose, "Gemma added nothing" is a perfectly valid outcome, not a failure.

    Contrast #589-B forms: there, "Gemma unreachable / zero spans" is a REFUSAL because a
    columnar form cannot be certified without verification. On prose there is nothing to
    verify — GLiNER+regex already ran; Gemma can only add. So every failure mode here is
    fail-OPEN. The whole point: never refuse prose for lack of Gemma.
    """
    out = res.anonymized
    try:
        spans = _gemma_extract_call(res.anonymized)
        for sp in spans:
            val, typ = sp.get("text", ""), sp.get("type", "MOT")
            if val and val in out:
                token = engine.vault.token_for(val, typ)
                out = out.replace(val, token)
    except Exception:
        # FAIL-OPEN: Gemma unreachable/malformed on prose -> return the GLiNER+regex
        # floor (res.anonymized), never refuse. This is the whole point of the additive
        # pass — a Gemma failure on prose must not refuse a well-masked doc.
        out = res.anonymized
    return _finalise_anonymised(res, out)


_GEMMA_MODES = frozenset({"all", "hard", "off"})


def _gemma_mode() -> str:
    """Return the background-masker Gemma mode: "all" | "hard" | "off".

    Reads `gemma_mode` from the SAME bubble-shield.json the guard resolves (same
    search order as _is_protected_folder / the guard: BUBBLE_SHIELD_GUARD_CONFIG,
    then CLAUDE_PROJECT_DIR/.bubble-shield.json, ~/.config/bubble_shield/bubble-shield.json,
    ~/.bubble-shield.json). First config found wins.

    Fail toward MORE masking: missing file, malformed JSON, missing/invalid key, or
    any error -> "all" (the fail-closed-consistent default). Never raises.
    """
    try:
        for loc in (
            os.environ.get("BUBBLE_SHIELD_GUARD_CONFIG"),
            os.path.join(os.environ.get("CLAUDE_PROJECT_DIR", ""), ".bubble-shield.json"),
            os.path.expanduser("~/.config/bubble_shield/bubble-shield.json"),
            os.path.expanduser("~/.bubble-shield.json"),
        ):
            if not loc or not Path(loc).is_file():
                continue
            try:
                cfg = json.loads(Path(loc).read_text(encoding="utf-8"))
            except Exception:
                # unreadable/malformed config -> fail toward more masking
                return "all"
            mode = cfg.get("gemma_mode", "all")
            return mode if mode in _GEMMA_MODES else "all"
    except Exception:
        return "all"
    return "all"


def _gemma_gate_decision(mode: str, is_form: bool) -> str:
    """PURE decision for the Gemma-pass gate. Returns one of FOUR values:
      "run_failclosed" — form + (all|hard): run _gemma_second_pass. Gemma VERIFIES
                         the form or FAILS CLOSED (raises, never returns a body).
                         This is the #589-B guarantee — unchanged.
      "run_additive"   — prose + all: run _gemma_additive_pass. Gemma ADDS masking
                         on top of the GLiNER+regex floor; a Gemma failure/empty
                         result is fail-OPEN (returns the GLiNER+regex-masked body),
                         NEVER a refusal. Prose is never refused for lack of Gemma.
      "fail_closed"    — off + form: REFUSE (raise StructuredFormUnverifiedError),
                         never return an unverified form body.
      "skip"           — prose + hard, prose + off: no Gemma; the GLiNER+regex result
                         falls through to the non-Gemma return path.

    SECURITY INVARIANTS:
      - off + form => "fail_closed" (never "skip"): skipping would return an
        unverified form body = re-open the #589-B liasse/CERFA PII leak.
      - form + (all|hard) => "run_failclosed" (never additive): a structured form
        is never certified without Gemma verification, in EVERY mode where Gemma runs.
      Prose is ADDITIVE only — Gemma can add masking on prose but a Gemma failure on
      prose must never refuse a doc the GLiNER+regex floor already masked.
    """
    if mode == "off":
        return "fail_closed" if is_form else "skip"
    if is_form:
        # form + (all|hard) -> verify or fail closed
        return "run_failclosed"
    if mode == "all":
        # prose + all -> additive (fail-open floor)
        return "run_additive"
    # prose + hard -> no Gemma
    return "skip"


# #568 — async on-seed de-pollution trigger.
#
# After seed_vault_into_gazetteer() feeds newly-confirmed PII into the deny-list
# gazetteer, run a de-pollution pass (Gemma-adjudicated triage of low-confidence
# gazetteer entries) so junk/label tokens don't linger as false "confirmed PII".
# This MUST be non-blocking: it runs in a daemon background thread so the Cowork
# read path returns immediately, and it MUST NEVER break or slow the read on
# failure (same fail-open doctrine as seed_vault_into_gazetteer itself).
def _run_depollute_pass():
    """Run one de-pollution pass over the gazetteer. Fail-open: never raises."""
    try:
        sys.path.insert(0, str(_vendor()))
        from bubble_shield.depollute import depollute_gazetteer, daemon_classify
        depollute_gazetteer(daemon_classify)
    except Exception:
        pass  # fail-open: de-pollution must NEVER break or slow the read path


def _fire_depollute_async():
    """Kick off _run_depollute_pass() on a daemon thread and return immediately.

    Returns the Thread (mainly so tests can join() it) — callers on the hot
    path must NOT join/wait on it.
    """
    import threading
    t = threading.Thread(target=_run_depollute_pass, daemon=True)
    t.start()
    return t


def _finalise_anonymised(res, body: str) -> str:
    """Attach the honest verdict note + #334 KEEP-warning to a masked body and return it.

    Extracted from _anonymise_text so BOTH the non-Gemma return path AND the prose
    additive Gemma path (_gemma_additive_pass) produce byte-identical framing for the
    same verdict_state — the only difference between them is `body` (additive may carry
    extra Gemma masking). `res` supplies the canonical verdict_state; `body` is the final
    masked text to frame.
    """
    _state = getattr(res, "verdict_state", None)
    # Verdict note — keyed off the engine's canonical verdict_state so each state
    # gets the HONEST message. Critically, the zero-detection state (a substantial
    # doc where NOTHING was found) must NOT be presented as safe: "found nothing"
    # is not "safe", it's "nothing found", which on free text often means a
    # name/address was MISSED. (Product-integrity fix 2026-07-02.) A zero_detection
    # verdict only reaches here when _text_quality_gate confirmed the text was
    # clean enough for that "found nothing" to be a trustworthy verdict — the
    # garbled split already raised in _anonymise_text and never gets here.
    if _state == "zero_detection":
        note = _ZERO_DETECTION_CLEAN_NOTE
    elif _state == "low_confidence":
        note = (
            "\n\n[⚠️ Bubble Shield : une relecture humaine est conseillée — "
            "une donnée potentiellement sensible est restée sous le seuil de confiance.]")
    elif _state == "leak":
        note = (
            "\n\n[⚠️ Bubble Shield : une donnée identifiante est restée en clair — "
            "NE PAS envoyer sans correction.]")
    else:
        note = ""

    # #334 — LOUD WARNING when an identifying type is set to KEEP in the policy.
    # The client keeps full autonomy (no hard floor), but the kept types are named
    # loudly so the leak is never silent. This is ADDITIVE — it never changes what
    # gets masked. Separate from the NER-down error above (both can fire).
    try:
        sys.path.insert(0, str(_vendor()))
        from bubble_shield.policy import kept_identifying_types, load_policy as _load_pol
        _kept = kept_identifying_types(_load_pol())
        if _kept:
            _types_str = ", ".join(_kept)
            kept_warning = (
                f"⚠️ Bubble Shield — MASQUAGE DÉSACTIVÉ pour : {_types_str} "
                f"(selon votre configuration). Ces données identifiantes restent "
                f"EN CLAIR dans le résultat. Vérifiez votre politique de "
                f"confidentialité (policy.json) si ce n'est pas voulu.\n\n"
            )
            return kept_warning + body + note
    except Exception:
        pass  # fail-open: warning failure must never break anonymization

    return body + note


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

    Phase 0 (Tier-2 desktop app): after the vault is saved, candidate spans
    (sub-threshold / unsafe) are written to a local host-side sidecar file via
    bubble_shield.candidate_sidecar.write_candidates().  This write is FAIL-OPEN
    and NEVER changes the string returned to the agent.
    """
    engine, vpath, daemon_up = _engine(text, filename_basename=filename_basename)
    if not daemon_up:
        # Kick the re-arm so the next call can succeed, then fail this one closed.
        _try_spawn_daemon_from_mcp()
        raise NERDownError(_NER_DOWN_ERROR)
    res = engine.anonymize(text)

    # P0 SECURITY FIX (#589) — STRUCTURAL TRIPWIRE. `res` must be a genuinely
    # COMPLETED AnonymizationResult before ANYTHING below trusts it (including
    # persisting to the vault). A valid verdict_state can only be produced by
    # AnonymizationResult's own property chain running on a real result object,
    # so this is the earliest point a "did masking actually complete?" check
    # can be made — placed BEFORE vault.save so an incomplete/invalid result
    # never even gets persisted as if it were real masking output. This is the
    # catch-all fail-closed gate for every path that is NOT already covered by
    # NERDownError (daemon offline, above) or ZeroDetectionError (a COMPLETED
    # run that legitimately found zero PII on a substantial doc, below) — see
    # MaskingIncompleteError's docstring for the full rationale.
    if res is None or getattr(res, "verdict_state", None) not in _VALID_VERDICT_STATES:
        raise MaskingIncompleteError(_MASKING_INCOMPLETE_ERROR)

    engine.vault.save(str(vpath))

    # #390 — seed the vault into the deny-list gazetteer. Every IDENTIFYING value
    # now in the vault is confirmed PII for this client, so feed it to the wired
    # known-PII recognizer: a later doc where the probabilistic NER MISSES a known
    # name is then still masked deterministically (the leak this fix closes).
    # MUST run AFTER vault.save so we read the vault's FINAL to_value/tokens for
    # this mission. Fail-open: an enhancement to FUTURE recall — the current doc's
    # masking already happened, so a seeding failure must never break anonymisation.
    try:
        sys.path.insert(0, str(_vendor()))
        from bubble_shield.known_pii_store import seed_vault_into_gazetteer
        seed_vault_into_gazetteer(engine.vault)
        _fire_depollute_async()   # #568: async de-pollution, never blocks the read
    except Exception:
        pass  # fail-open: deny-list seeding never breaks/slows anonymisation

    # Phase 0 — candidate sidecar (host-side, fail-open, never changes agent output).
    # Sub-threshold / unsafe entities are written locally for the future HITL feeder.
    # The agent-facing string below is byte-identical to the pre-Phase-0 code path.
    try:
        sys.path.insert(0, str(_vendor()))
        from bubble_shield.candidate_sidecar import write_candidates
        mission = os.environ.get("BUBBLE_SHIELD_SESSION", "mcp-session")
        write_candidates(res, mission=mission, source_doc=filename_basename)
    except Exception:
        pass  # fail-open: sidecar write never breaks anonymization

    # P0 SECURITY FIX (#589) — TEXT-QUALITY GATE on zero-detection for a
    # SUBSTANTIAL document. The host-side, fail-open side effects above (vault
    # save, deny-list seeding, de-pollution trigger, candidate sidecar) have
    # already run — this gate only controls what comes back to the AGENT.
    # verdict_state=="zero_detection" fires ONLY for a substantial doc
    # (engine.py AnonymizationResult.substantial_text: >=8 words AND >=40
    # chars) — the distinct "nothing_to_do" state (trivially short/empty
    # input) is deliberately NOT gated here, so a genuinely tiny/empty input
    # is never refused.
    #
    # Candidate COUNT alone is ambiguous here (a garbage/OCR extraction can
    # produce a stray false candidate while clean prose produces none) — so
    # the split is on TEXT QUALITY of the extracted text itself, not on
    # entity_count (which is already 0 either way inside this branch):
    #   - CLEAN prose (quality above _text_quality_gate's thresholds) → this
    #     is the genuine "no PII in this document" case: GLiNER had real text
    #     to work on and confidently found nothing. Falls through to the note
    #     logic below and RETURNS — refusing this would be over-blocking a
    #     legitimately clean document.
    #   - GARBLED/low-quality text (below thresholds) → the extraction itself
    #     was too degraded for the recognizers to have had a fair shot (OCR
    #     noise, broken PDF text layer). "Found nothing" here is not an
    #     honest verdict at all, so this is a HARD FAIL-CLOSED: raise, no
    #     body returned. This is the real-incident leak class this fix
    #     closes: a scanned-PDF financial document, OCR-degraded extraction.
    _state = getattr(res, "verdict_state", None)
    if _state == "zero_detection" and not _text_quality_gate(res.original):
        raise ZeroDetectionError(_ZERO_DETECTION_ERROR)

    # P0 #589-B — a COMPLETED masked_ok/low_confidence result on a STRUCTURED FORM cannot be
    # trusted (degraded columnar extraction hides entities from the fast pass; the doc still
    # reads as clean prose so the quality gate never fires). Escalate to a Gemma pass, or fail
    # closed. Prose docs (all-mode) get an ADDITIVE Gemma pass (fail-OPEN floor); forms are
    # verified or fail closed. Note-honesty fix: _gemma_second_pass / _gemma_additive_pass
    # append their own note, and ONLY when they actually applied a span.
    #
    # Mode-aware gate (gemma_mode: "all" | "hard" | "off", default "all"). The pure
    # _gemma_gate_decision helper holds the safety logic (unit-tested, full 8-case matrix).
    #   "run_failclosed" → form + (all|hard): Gemma verifies or FAILS CLOSED (#589-B).
    #   "run_additive"   → prose + all: Gemma ADDS masking; failure/empty is fail-OPEN
    #                      (returns the GLiNER+regex floor), never a refusal.
    #   "fail_closed"    → off + form: REFUSE (raise), never return an unverified body —
    #                      preserves the #589-B guarantee on weak Macs.
    #   "skip"           → prose + (hard|off): fall through to the non-Gemma path below.
    _decision = _gemma_gate_decision(_gemma_mode(), _is_structured_form(res.original))
    if _decision == "fail_closed":
        raise StructuredFormUnverifiedError(_STRUCTURED_FORM_UNVERIFIED_ERROR)
    if _decision == "run_failclosed":
        return _gemma_second_pass(res, engine)
    if _decision == "run_additive":
        return _gemma_additive_pass(res, engine)
    # _decision == "skip" → fall through to the non-Gemma return path

    return _finalise_anonymised(res, res.anonymized)


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
    # The extractor imports the vendored pure-python `pypdf` to read PDFs. It does
    # its own module-top vendor-path insertion, but that keys off a single
    # CLAUDE_PLUGIN_ROOT env var — fragile and the source of the client's
    # "pypdf manquant" error when that var resolves elsewhere. Insert the vendor
    # dir here too, matching every other call site in this file (e.g. line ~365),
    # so the import is robust regardless of how the env is set.
    sys.path.insert(0, str(_vendor()))
    sys.path.insert(0, str(_scripts_dir()))
    from bubble_shield_extract import extract_file          # PDF/docx/text → text
    p = Path(os.path.expanduser(path)).resolve()
    if not p.is_file():
        raise FileNotFoundError(f"no such file: {p}")
    text = extract_file(p)                            # fail-closed on scanned PDFs
    return _anonymise_text(text, filename_basename=p.name)


def _read_with_shadow(path: str) -> str:
    """Fast read path for bubble_shield_read — hash → serve, ZERO models.

    Shadow-index redesign (Phase 2). The client-facing speed win: a cache HIT
    returns pre-anonymised text with no GLiNER / Gemma / OCR at read time.

      HIT  → return the cached clean shadow immediately (zero models).
      MISS → B1 ACCEPTED GAP (client-agreed, documented in the plan): serve the
             RAW extracted text (`extract_file`, text-extraction only — no NER,
             no Gemma), NOT `_anonymise_file`. Full anonymisation happens later,
             off the read path, in the background sweep (Phase 3). We queue the
             miss via `shadow_store.mark_pending` so the sweep picks it up.

    This is the ONE place Bubble Shield deliberately does NOT fail-closed: on a
    miss we serve raw for speed, by explicit product decision. Do NOT "improve"
    this by running models here — the whole point is zero models at read time.
    Must NEVER call `_anonymise_file` (that runs models).
    """
    sys.path.insert(0, str(_vendor()))
    from bubble_shield import shadow_store
    p = Path(os.path.expanduser(path)).resolve()
    if not p.is_file():
        raise FileNotFoundError(f"no such file: {p}")
    h = shadow_store.content_hash(p)
    cached = shadow_store.get_shadow(h)
    if cached is not None:
        # HIT — zero models. Belt-and-suspenders: a cheap EXACT-STRING safety net
        # over the served shadow. For every confirmed name in the gazetteer, if it
        # sits IN CLEAR in this cached shadow, mask it before serving. This catches
        # a name that a gazetteer-confirmed entry (confirmed elsewhere) SHOULD have
        # masked but that leaked into this particular shadow. Pure str.replace —
        # NO NER, NO Gemma, NO regex — stays cheap so the fast read path stays fast.
        try:
            from bubble_shield import known_pii_store
            for name in known_pii_store.load_gazetteer().values():
                # Over-masking guard: skip empty / very-short values (< 3 chars).
                # A 1–2 char gazetteer entry replacing every occurrence of that
                # substring everywhere would destroy the doc. Fail-toward-masking
                # still holds for real names (>= 3 chars): a leaked name is worse
                # than a slight over-mask.
                if not name or len(name) < 3:
                    continue
                cached = cached.replace(name, "⟦NOM_∎⟧")
        except Exception:
            pass  # net is additive; a gazetteer failure must never break the read
        return cached
    # MISS — B1 accepted gap: serve RAW extracted text, no models at read time.
    sys.path.insert(0, str(_scripts_dir()))
    from bubble_shield_extract import extract_file
    try:
        # Queue the miss for the background sweep. `mark_pending` lands in Task 6;
        # best-effort by design so a missing/failing mark never breaks a read.
        shadow_store.mark_pending(str(p))
    except Exception:
        pass
    return extract_file(p)


# ---- folder listing (bubble_shield_list) -----------------------------------
# The sanctioned discovery path. The PreToolUse guard now ALLOWS the native Glob
# on a protected folder (names only), but a dedicated MCP tool gives a reliable,
# reversible listing with per-entry modality/size so the agent can pick "the PDF"
# / "the scan" / "the newest" — WITHOUT ever reading file CONTENT.
#
# Design decisions (documented):
#   - NON-RECURSIVE (shallow): we list only the immediate children of `folder`.
#     Recursion could enumerate an arbitrarily deep tree (cost + noise) and leak
#     structure the agent doesn't need to pick a file; the agent can list a
#     subfolder explicitly by calling the tool again on it. Subdirs are marked
#     with type="dir" so the agent knows what it can descend into.
#   - Entry NAMES are returned UNMASKED (in clear), for BOTH dirs and files. A
#     folder/file name is a navigation label the user OWNS and already SEES on
#     their own machine — the user is the one who typed/chose the client name to
#     navigate by. Masking listing names doesn't add privacy (the user already
#     knows the names) but destroys usability (can't navigate a picker full of
#     ⟦…⟧ tokens). PII protection belongs to file CONTENT, which is
#     bubble_shield_read's job (via _anonymise_file / _anonymise_text) — this
#     tool never touches content. The `protected` flag is still reported
#     (informational) but no longer drives any masking here.
#   - A hard CAP (_LIST_MAX_ENTRIES) bounds a folder with thousands of files so
#     the listing can't blow up; when hit, `truncated` is reported.

_LIST_MAX_ENTRIES = 500  # cap so a huge folder can't produce an unbounded listing

# Modality inference by extension — lets the agent reason about "the PDF" / "the
# scan" / "the spreadsheet" from the masked listing alone.
_MODALITY_BY_EXT = {
    ".pdf": "pdf", ".docx": "document", ".doc": "document", ".odt": "document",
    ".txt": "text", ".md": "text", ".rtf": "text",
    ".csv": "table", ".tsv": "table", ".xlsx": "spreadsheet", ".xls": "spreadsheet",
    ".json": "data", ".xml": "data", ".yaml": "data", ".yml": "data",
    ".jpg": "image/scan", ".jpeg": "image/scan", ".png": "image/scan",
    ".tif": "image/scan", ".tiff": "image/scan", ".gif": "image", ".webp": "image",
    ".heic": "image/scan", ".bmp": "image/scan",
    ".zip": "archive", ".tar": "archive", ".gz": "archive",
}


def _is_protected_folder(p: Path) -> bool:
    """True if `p` is a protected folder. Mirrors the guard's two protection
    mechanisms so bubble_shield_list can decide whether filename masking is
    required: (1) an in-folder `.bubble-shield.json` marker on `p` or any ancestor;
    (2) `p` sits under a global `protected_folders` entry. Best-effort and
    fail-SAFE: on any error we return True (mask), never False — over-masking a
    non-protected folder is harmless, under-masking a protected one leaks."""
    try:
        p = p.resolve()
    except Exception:
        return True
    # (1) in-folder marker walk-up (Cowork-native)
    try:
        start = p if p.is_dir() else p.parent
        for anc in [start, *start.parents]:
            try:
                if (anc / ".bubble-shield.json").is_file():
                    return True
            except OSError:
                continue
    except Exception:
        return True
    # (2) global protected_folders config
    try:
        for loc in (
            os.environ.get("BUBBLE_SHIELD_GUARD_CONFIG"),
            os.path.join(os.environ.get("CLAUDE_PROJECT_DIR", ""), ".bubble-shield.json"),
            os.path.expanduser("~/.config/bubble_shield/bubble-shield.json"),
            os.path.expanduser("~/.bubble-shield.json"),
        ):
            if not loc or not Path(loc).is_file():
                continue
            try:
                cfg = json.loads(Path(loc).read_text(encoding="utf-8"))
            except Exception:
                # unreadable config → fail safe (treat as protected)
                return True
            for raw in cfg.get("protected_folders", []) or []:
                try:
                    prot = Path(os.path.expanduser(raw)).resolve()
                except Exception:
                    continue
                try:
                    p.relative_to(prot)
                    return True
                except ValueError:
                    continue
            break  # first config found wins (same order as the guard)
    except Exception:
        return True
    return False


def _list_folder(folder: str) -> str:
    """List the immediate children of `folder` (NON-recursive). Returns a JSON
    listing: for each entry its NAME (always IN CLEAR — a navigation label the
    user already owns/sees, not PII protection scope), type (file|dir), and for
    files the extension + inferred modality + byte size. NEVER reads or returns
    file CONTENT — content masking is bubble_shield_read's job.

    The `protected` flag is reported (informational) but does not affect this
    tool's output — names are unmasked whether or not the folder is protected.
    """
    p = Path(os.path.expanduser(folder)).resolve()
    if not p.exists():
        raise FileNotFoundError(f"no such folder: {p}")
    if not p.is_dir():
        raise NotADirectoryError(f"not a folder: {p}")

    protected = _is_protected_folder(p)

    # Enumerate immediate children (shallow). Skip the marker file itself.
    raw_entries: list[os.DirEntry] = []
    try:
        for e in os.scandir(p):
            if e.name == ".bubble-shield.json":
                continue
            raw_entries.append(e)
    except OSError as ex:
        raise OSError(f"cannot list folder: {ex}")
    raw_entries.sort(key=lambda e: e.name.lower())

    truncated = len(raw_entries) > _LIST_MAX_ENTRIES
    raw_entries = raw_entries[:_LIST_MAX_ENTRIES]

    entries: list[dict] = []
    for e in raw_entries:
        try:
            is_dir = e.is_dir(follow_symlinks=False)
        except OSError:
            is_dir = False
        is_symlink = False
        try:
            is_symlink = e.is_symlink()
        except OSError:
            pass
        item: dict = {"name": e.name, "type": "dir" if is_dir else "file"}
        if is_symlink:
            item["symlink"] = True
        if not is_dir:
            ext = os.path.splitext(e.name)[1].lower()
            item["ext"] = ext
            item["modality"] = _MODALITY_BY_EXT.get(ext, "other")
            try:
                item["size"] = e.stat(follow_symlinks=False).st_size
            except OSError:
                item["size"] = None
        entries.append(item)

    # Entry NAMES are returned IN CLEAR — see design-decision comments above.
    # No _anonymise_text call here; this tool never masks names.

    result = {
        "folder": str(p),
        "protected": protected,
        "recursive": False,
        "count": len(entries),
        "truncated": truncated,
        "entries": entries,
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


def _anonymise_mail(query: str = "ALL", maxn: int = 10, since: str = None) -> str:
    """Fetch mail over IMAP and return every message ANONYMISED, fail-CLOSED.

    This is the mail mirror of _anonymise_file: Bubble Shield OWNS the read
    (fetches via IMAP host-side), then routes EACH raw body through the SAME
    fail-closed _anonymise_text core the file guard uses. Because _anonymise_text
    raises NERDownError when the daemon is down, this whole path inherits the
    daemon-up-or-refuse guarantee for free — the caller converts NERDownError to
    isError:true and NO raw e-mail is ever returned.

    The raw From/Subject/body are combined into one text block per message and
    anonymised together, so a client's name appearing in BOTH the From-header and
    the body collapses to the SAME token (cross-field consistency), and — via the
    shared per-mission vault — the SAME token as in that client's PDFs
    (cross-source consistency).

    Fail-closed ordering matters: if the daemon is down, _anonymise_text raises on
    the FIRST message before any body is appended to the output, so a daemon-down
    call returns zero anonymised content.
    """
    sys.path.insert(0, str(_scripts_dir()))
    from bubble_shield_mail import fetch_mail, load_credentials  # imaplib + email (stdlib)
    creds = load_credentials()                      # raises MailConfigError (no secret leaked) if unset/mis-perm
    msgs = fetch_mail(query=query, maxn=maxn, since=since, creds=creds)
    if not msgs:
        return "📭 Aucun message ne correspond à cette recherche."
    blocks = []
    for uid, frm, subj, body in msgs:
        raw = f"From: {frm}\nSubject: {subj}\n\n{body}"
        # fail-CLOSED: raises NERDownError if the daemon is down → caller → isError.
        # ONLY From/Subject/body go through the anonymiser — the UID is a mailbox-local
        # integer (never PII), so we PREPEND it in clear AFTER anonymising. The agent
        # must pass this UID line straight to bubble_shield_mail_apply (it identifies
        # the message in the same UID space apply's UID STORE mutates).
        cloaked = _anonymise_text(raw)
        blocks.append(f"UID: {uid}\n{cloaked}")
    header = f"📧 {len(blocks)} message(s) anonymisé(s) (le contenu brut n'a jamais quitté l'hôte) :\n"
    return header + ("\n\n" + ("─" * 40) + "\n\n").join(blocks)


def _apply_mail(decisions: list) -> str:
    """Apply a list of triage decisions host-side (labels / archive / reply-draft).

    Symmetric mutation counterpart to _anonymise_mail. For each decision:
      * apply_labels(add_labels, remove=["\\Inbox"] iff archive) — labels + archive.
      * if a draft is present, RESTORE the ⟦…⟧ token-bearing body (and subject/to)
        to real values with the SAME vault de-anonymiser bubble_shield_write uses
        (_deanonymise_string, IN-MEMORY), then feed the restored RFC822 straight to
        create_draft(). The restored real text is NEVER written to disk and NEVER
        returned — this function returns ONLY per-decision success/fail COUNTS.

    SECURITY / fail-closed:
      * Per-run cap: refuses if len(decisions) > MAX_MUTATIONS_PER_RUN.
      * NEVER embeds a body / restored PII in the returned summary (only counts + uid
        + generic reason). The caller's generic handler also strips str(e) from any
        exception text, so even an unexpected raise cannot leak the draft body.
    """
    sys.path.insert(0, str(_scripts_dir()))
    from bubble_shield_mail import (
        apply_labels, build_reply_draft, create_draft, load_credentials,
        MAX_MUTATIONS_PER_RUN,
    )
    sys.path.insert(0, str(_vendor()))
    from bubble_shield.vault import TOKEN_RE  # to detect UNRESOLVED ⟦…⟧ tokens

    if not isinstance(decisions, list):
        raise RuntimeError("decisions doit être une liste.")
    if len(decisions) > MAX_MUTATIONS_PER_RUN:
        # Fail-closed: refuse the WHOLE call rather than applying a partial batch.
        raise RuntimeError(
            f"trop de décisions ({len(decisions)}) — limite de sécurité "
            f"{MAX_MUTATIONS_PER_RUN} mutations par appel.")

    creds = load_credentials()  # raises MailConfigError (no secret leaked) if unset/mis-perm

    labels_applied = 0
    drafts_created = 0
    drafts_skipped = 0
    failures = 0
    fail_uids: list[str] = []

    for dec in decisions:
        if not isinstance(dec, dict):
            failures += 1
            continue
        uid = str(dec.get("uid", "")).strip()
        if not uid:
            failures += 1
            continue
        try:
            # USER labels (may be spaced/emoji) — kept STRICTLY separate from the
            # \Inbox system flag: mixing a spaced label + \Inbox in one STORE is a
            # Gmail-IMAP gotcha. We drop any \Inbox the caller wrongly put in the
            # user-label lists (archive/unarchive are the sanctioned way to touch it).
            add = [l for l in (dec.get("add_labels") or [])
                   if str(l).strip().lower() not in ("\\inbox", "inbox")]
            remove = [l for l in (dec.get("remove_labels") or [])
                      if str(l).strip().lower() not in ("\\inbox", "inbox")]
            did = False
            if add or remove:
                apply_labels(uid, add_labels=add, remove_labels=remove, creds=creds)
                did = True
            # \Inbox in its OWN store call (never combined with user labels).
            # archive = remove \Inbox; unarchive = add it back.
            if dec.get("archive"):
                apply_labels(uid, remove_labels=["\\Inbox"], creds=creds); did = True
            if dec.get("unarchive"):
                apply_labels(uid, add_labels=["\\Inbox"], creds=creds); did = True
            if did:
                labels_applied += 1

            draft = dec.get("draft")
            if draft:
                body_tokens = draft.get("body_tokens", "")
                subject_tokens = draft.get("subject", "")
                to_tokens = draft.get("to", "")
                # Option-A restore: real values go into the draft, restored text is
                # NEVER returned to the model (build the RFC822 and hand it straight
                # to create_draft — no disk write, no echo).
                real_body = _deanonymise_string(body_tokens) if body_tokens else ""
                real_subject = _deanonymise_string(subject_tokens) if subject_tokens else ""
                real_to = _deanonymise_string(to_tokens) if to_tokens else ""
                # SKIP-not-ship on unresolved tokens: if the vault could not restore
                # a ⟦…⟧ token (no entry for this session), the restored text still
                # carries a LITERAL token. Appending it would put a broken artifact
                # (visible ⟦NOM_1⟧) into the user's real Gmail Drafts. Skip the draft,
                # keep the labels/archive already applied above, and count it as
                # skipped. NOTE: TOKEN_RE matches ONLY the placeholder pattern, never
                # real PII — so this boolean check leaks nothing (we never read the
                # restored value, only whether a token pattern survived).
                if (TOKEN_RE.search(real_body) or TOKEN_RE.search(real_subject)
                        or TOKEN_RE.search(real_to)):
                    drafts_skipped += 1
                else:
                    irt = draft.get("in_reply_to")
                    raw = build_reply_draft(
                        to_addr=real_to, subject=real_subject, body_text=real_body,
                        in_reply_to=irt, references=irt)
                    create_draft(raw, creds=creds)
                    drafts_created += 1
        except Exception as e:
            # NEVER surface str(e) here — a restore/append error could quote the draft
            # body / restored PII. But the exception TYPE NAME is a class identifier
            # (e.g. MailConfigError, IMAP4.error, FileNotFoundError) — it carries NO
            # PII, so we DO surface it (in stderr AND the returned summary) to make a
            # failure diagnosable without leaking. Message/args are never included.
            etype = type(e).__name__
            print(f"[bubble_shield] mail_apply decision uid={uid} failed: {etype}",
                  file=sys.stderr, flush=True)
            failures += 1
            fail_uids.append(f"{uid}:{etype}")

    summary = (
        f"📬 Décisions appliquées (le contenu réel n'a jamais quitté l'hôte).\n"
        f"  • {labels_applied} message(s) ré-étiqueté(s)/archivé(s)\n"
        f"  • {drafts_created} brouillon(s) créé(s) (jamais envoyé(s))\n"
        + (f"  • {drafts_skipped} brouillon(s) ignoré(s) : jetons non résolus "
           f"(⟦…⟧ sans valeur au coffre — non ajouté aux brouillons)\n"
           if drafts_skipped else "")
        + f"  • {failures} échec(s)"
        + (f" (UID:type — {', '.join(fail_uids)})" if fail_uids else "")
    )
    return summary


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


# ---- Finding #40: refuse to write restored real PII to an unguarded path ----
#
# `bubble_shield_write` (below) restores REAL client values and writes the file to
# disk. If that file lands somewhere a later agent built-in Read is NOT blocked —
# OUTSIDE any protected folder, OR inside one but on the marker's allow_paths /
# allow_extensions exemption (e.g. the anonymize skill's `clean/`) — then a
# `Read`/`cat` pulls the real names straight back into the session in clear. The
# PostToolUse re-anonymise scrub does NOT run on built-in Read in Cowork (#32105),
# so the PreToolUse BLOCK is the ONLY protection, and an allow-listed path
# disables it. So we enforce the invariant "the restored real document must land
# on a GUARDED path" — one where a subsequent agent Read is DENIED. The human
# still opens it (Finder / the local viewer); the guard governs the agent only.
#
# `_path_is_guarded` CALLS guard.py's OWN decision function so the two can NEVER
# drift: there is now exactly ONE implementation of "would a built-in Read of
# this path be blocked?" — `guard.decide_block_for_path` — used by BOTH the guard
# hook and this write gate. (Previously this hand-copied `decide_block`'s logic
# and DRIFTED: it missed the `p.name == MARKER_NAME` short-circuit, so a write to
# a `.bubble-shield.json`-named path was wrongly treated as guarded while the guard
# ALLOWED a Read of it → leak. Calling the real function fixes that class of bug
# for good.) A path is GUARDED iff a built-in Read of it would be DENIED.

def _guard_module():
    """Import guard.py (same scripts/ dir) so we reuse its EXACT marker logic."""
    sys.path.insert(0, str(_scripts_dir()))
    import guard as _g  # noqa: E402
    return _g


def _path_is_guarded(path: str) -> bool:
    """True iff a subsequent agent built-in Read of `path` would be BLOCKED by
    the guard — i.e. `path` sits under a marker (or a global protected_folders
    entry) AND is NOT exempted by that folder's allow_paths / allow_extensions,
    AND is not the marker file itself.

    Delegates to `guard.decide_block_for_path` — the guard's REAL decision
    function — so the write-side invariant can never disagree with the read-side
    gate. Fail-SAFE: on ANY error we return False (treat as UNGUARDED) so
    bubble_shield_write REFUSES rather than risk writing real PII to a readable
    location.
    """
    try:
        g = _guard_module()
        guarded, _msg = g.decide_block_for_path(path)
        return bool(guarded)
    except Exception:
        # Fail-SAFE: unknown → treat as UNGUARDED so the write is REFUSED.
        return False


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


def _deanonymise_string(content: str) -> str:
    """Restore real values from ⟦…⟧ tokens IN-MEMORY and return the restored string.

    Same vault restore as `_deanonymise_to_file` (option-A flow), but the restored
    text is NEVER written to disk and NEVER returned to the model — the ONLY caller
    (the mail-apply tool) feeds it straight to create_draft() so the real name lands
    in the Gmail draft while the restored text stays out of the session context.

    Raises RuntimeError if there is no vault for this session (can't restore without
    it — fail-closed rather than shipping ⟦…⟧ tokens into a live draft)."""
    engine, vpath, _daemon_up = _engine()
    if not vpath.is_file():
        raise RuntimeError("aucun coffre (vault) pour cette session — "
                           "lis d'abord des données via bubble_shield_read/mail_read")
    return engine.deanonymize(content)


# ---- ML accuracy-pack setup (async, host-side) -----------------------------

_SETUP_MARKER = BUBBLE_SHIELD_HOME / "setup.status"          # progress breadcrumb
_SETUP_LOG = BUBBLE_SHIELD_HOME / "setup.log"

# Skip-if-present predicates (#387) — pure disk checks, mirror the setup
# scripts' model_present() / ocr_models_present(). A model is "present" iff its
# onnx file (ML) or the cache sentinel (OCR) exists on disk; the setup then
# skips its download. Kept inline here so the MCP stays self-contained.
_MODELS_DIR = BUBBLE_SHIELD_HOME / "models"
_GLINER_DIR = "onnx-community__gliner_multi_pii-v1"
_GLINER_ONNX = "onnx/model_quantized.onnx"
_OPENAI_DIR = "openai__privacy-filter"
_OPENAI_ONNX = "onnx/model_q4.onnx"
_OCR_SENTINEL = BUBBLE_SHIELD_HOME / "layout_model_cached.flag"


_GEMMA_ENV = BUBBLE_SHIELD_HOME / "gemma-env"
# Must match setup_ml.GEMMA_MODEL_ID and gemma_classifier.MODEL_ID.
_GEMMA_MODEL_ID = "mlx-community/gemma-3n-E4B-it-lm-4bit"


def _gemma_present() -> bool:
    """True when the Gemma judge is fully installed: its venv exists AND its
    model snapshot is staged in the HF hub cache (what warm_up() loads). Mirrors
    setup_ml.install_gemma_env() + download_gemma_model()'s skip-if-present
    check, so 'present' here means the same thing the installer means."""
    if not (_GEMMA_ENV / "bin" / "python").exists():
        return False
    # snapshot_download stages under <hub>/models--<org>--<name>/snapshots/*
    cache_dir_name = "models--" + _GEMMA_MODEL_ID.replace("/", "--")
    hub_roots = [
        Path(os.environ.get("HF_HOME", str(Path.home() / ".cache" / "huggingface"))) / "hub",
        Path.home() / ".cache" / "huggingface" / "hub",
        _GEMMA_ENV / "hf-cache" / "hub",
    ]
    for hub in hub_roots:
        snap = hub / cache_dir_name / "snapshots"
        try:
            if snap.is_dir() and any(snap.iterdir()):
                return True
        except Exception:
            continue
    return False


def _model_states() -> dict:
    """Per-model present/absent map for GLiNER, OpenAI-PF, OCR, and Gemma.

    Gemma (the de-pollution judge + degraded-form second-pass masker) is the
    LARGEST model (~4.5 GB) and is installed by default in the same setup pass —
    it MUST be tracked here, or the 'ready' signal fires while Gemma is still
    downloading several GB in the background."""
    return {
        "gliner": "present" if (_MODELS_DIR / _GLINER_DIR / _GLINER_ONNX).is_file() else "absent",
        "openai": "present" if (_MODELS_DIR / _OPENAI_DIR / _OPENAI_ONNX).is_file() else "absent",
        "ocr": "present" if _OCR_SENTINEL.is_file() else "absent",
        "gemma": "present" if _gemma_present() else "absent",
    }


def _per_model_line(states: dict, downloading: bool = False) -> str:
    """Render the per-model status the onboarding shows the user (#387).

    e.g. "GLiNER ✓ déjà présent · OpenAI-PF ↓ téléchargement · OCR ↓ téléchargement · Gemma ↓ téléchargement"
    A model already on disk shows "✓ déjà présent"; an absent one shows
    "↓ téléchargement" while installing or "✓ prêt"/"absent" otherwise."""
    names = {"gliner": "GLiNER", "openai": "OpenAI-PF", "ocr": "OCR", "gemma": "Gemma"}
    parts = []
    for key in ("gliner", "openai", "ocr", "gemma"):
        st = states.get(key, "absent")
        if st == "present":
            tag = "✓ déjà présent"
        elif st == "done":
            tag = "✓ prêt"
        elif st == "absent" and downloading:
            tag = "↓ téléchargement"
        else:
            tag = "absent"
        parts.append(f"{names[key]} {tag}")
    return " · ".join(parts)


def _setup_script() -> Path:
    for cand in (PLUGIN_ROOT / "scripts" / "bubble_shield_setup_ml.py",
                 _HERE / "bubble_shield_setup_ml.py"):
        if cand.is_file():
            return cand
    return PLUGIN_ROOT / "scripts" / "bubble_shield_setup_ml.py"


def _setup_start() -> dict:
    """Spawn the ONE-PASS bootstrap DETACHED host-side and return immediately (#387).

    Downloads ALL models — GLiNER + OpenAI Privacy Filter + Gemma (ml setup) AND OCR
    (docling, ocr setup) — in a single pass so the client is never prompted to
    install a model later. Each model is skipped if already on disk. The reply
    names every model + its current state.

    Idempotent: if all three are already present, returns 'ready' with the
    per-model "déjà présent" line and launches nothing."""
    states = _model_states()
    if all(v == "present" for v in states.values()):
        return {"state": "ready",
                "message": "Tous les modèles sont déjà installés.",
                "models": states,
                "per_model": _per_model_line(states)}
    script = _setup_script()
    if not script.is_file():
        return {"state": "error", "message": f"bootstrap introuvable: {script}"}
    ocr_script = _ocr_setup_script()
    BUBBLE_SHIELD_HOME.mkdir(parents=True, exist_ok=True)
    _SETUP_MARKER.write_text("installing", encoding="utf-8")
    _OCR_SETUP_MARKER.write_text("installing", encoding="utf-8")
    import subprocess
    logf = open(_SETUP_LOG, "a")
    # One detached wrapper runs BOTH setups in sequence (ML first, then OCR) so
    # the whole model set is pulled in a single pass. Each setup is skip-if-
    # present internally; the wrapper records each marker so /status can read
    # them. ML default now pulls GLiNER + OpenAI-PF (no --openai flag needed).
    ocr_part = ""
    if ocr_script.is_file():
        ocr_part = (
            f"rc2=subprocess.run([sys.executable,{str(ocr_script)!r}]).returncode;"
            f"open({str(_OCR_SETUP_MARKER)!r},'w').write('ready' if rc2==0 else 'error');"
        )
    wrapper = (
        f"import subprocess,sys;"
        f"rc=subprocess.run([sys.executable,{str(script)!r}]).returncode;"
        f"open({str(_SETUP_MARKER)!r},'w').write('ready' if rc==0 else 'error');"
        f"{ocr_part}"
    )
    subprocess.Popen([sys.executable, "-c", wrapper],
                     stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
                     start_new_session=True)
    return {"state": "installing",
            "models": states,
            "per_model": _per_model_line(states, downloading=True),
            "message": "Installation des modèles démarrée en une seule passe "
                       "(GLiNER + OpenAI-PF + OCR + Gemma ; ~5–6 Go au total, "
                       "Gemma étant le plus gros ; quelques minutes ; les modèles "
                       "déjà présents sont ignorés). Rappelle "
                       "bubble_shield_setup_ml(action='status') pour suivre."}


def _setup_status() -> dict:
    """Per-model status across the one-pass install (GLiNER + OpenAI-PF + OCR +
    Gemma).

    Reports each model by name with its present/downloading/ready/error state,
    so the onboarding can show the user exactly what is installed vs in flight.
    'ready' requires EVERY model — including Gemma (~4.5 GB, the largest) — so
    the client is never told 'done' while a multi-GB model is still downloading."""
    states = _model_states()
    ml_marker = _SETUP_MARKER.read_text(encoding="utf-8").strip() if _SETUP_MARKER.is_file() else "absent"
    ocr_marker = _OCR_SETUP_MARKER.read_text(encoding="utf-8").strip() if _OCR_SETUP_MARKER.is_file() else "absent"
    installing = ml_marker == "installing" or ocr_marker == "installing"

    if all(v == "present" for v in states.values()):
        state = "ready"
        message = "Tous les modèles sont prêts (GLiNER + OpenAI-PF + OCR + Gemma)."
    elif installing:
        state = "installing"
        message = "Installation en cours (téléchargement des modèles)…"
    elif "error" in (ml_marker, ocr_marker):
        state = "error"
        message = ("Une installation a échoué — voir ~/.bubble_shield/setup.log "
                   "et ~/.bubble_shield/ocr-setup.log.")
    elif any(v == "present" for v in states.values()):
        state = "partial"
        message = "Certains modèles sont prêts, d'autres restent à installer."
    else:
        state = "absent"
        message = "Aucun modèle installé. Lance bubble_shield_setup_ml(action='start')."

    return {"state": state, "message": message, "models": states,
            "per_model": _per_model_line(states, downloading=installing)}


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
                # Shadow-index fast path: hash → serve cached shadow (HIT, zero
                # models) or raw extracted text (MISS, B1). Never runs models at
                # read time; anonymisation of misses happens in the sweep.
                anon = _read_with_shadow(args.get("path", ""))
                # Surface OCR quality note when the text was extracted via OCR pack
                if _OCR_TAG in anon[:30]:
                    anon = _OCR_QUALITY_NOTE + anon
                ok(anon)
            elif name == "bubble_shield_list":
                ok(_list_folder(args.get("folder", "")))
            elif name == "bubble_shield_mail_read":
                # Mail path disabled from the shipped product (V1 is docs-only);
                # kept in reserve behind BUBBLE_SHIELD_ENABLE_MAIL. Not exposed via
                # tools/list either, but guard the dispatch too in case a caller
                # invokes it directly without listing tools first.
                if not _mail_enabled():
                    fail("Bubble Shield — le module e-mail est désactivé dans cette version.")
                    return
                anon = _anonymise_mail(
                    query=args.get("query", "ALL"),
                    maxn=args.get("max", 10),
                    since=args.get("since"))
                ok(anon)
            elif name == "bubble_shield_mail_apply":
                # Mutation counterpart to mail_read. Applies labels/archive/draft
                # host-side. Restores draft bodies IN-MEMORY via the vault (option-A)
                # and returns ONLY per-decision counts — NEVER any body/PII. Any
                # exception is caught below and converted to a FIXED fail message
                # (no str(e) → no draft body / restored PII can leak).
                if not _mail_enabled():
                    fail("Bubble Shield — le module e-mail est désactivé dans cette version.")
                    return
                ok(_apply_mail(args.get("decisions", [])))
            elif name == "bubble_shield_anonymize_text":
                ok(_anonymise_text(args.get("text", "")))
            elif name == "bubble_shield_write":
                # Finding #40: REFUSE to write restored real PII to a path a
                # later agent built-in Read is NOT blocked from (outside any
                # protected folder, OR allow-listed inside one). Enforced BEFORE
                # any restore/write so no clear PII ever touches an unguarded
                # location. The caller picks WHERE within the guarded folder —
                # we only enforce the "must be guarded" invariant, no hardcoded dir.
                _wpath = args.get("path", "")
                if not _path_is_guarded(_wpath):
                    fail(
                        "⛔ Bubble Shield — écriture refusée. Le document RESTAURÉ "
                        "(vraies valeurs client) ne peut PAS être écrit à cet "
                        "emplacement : il n'est PAS protégé, donc l'IA pourrait le "
                        "relire et les vrais noms reviendraient dans la session. "
                        "Choisis un chemin À L'INTÉRIEUR du dossier client protégé "
                        "(qui porte le marqueur .bubble-shield.json), mais PAS sous "
                        "un sous-dossier autorisé en lecture (allow_paths, ex. "
                        "`clean/`) ni avec une extension exemptée (allow_extensions). "
                        "Exemple : la racine du dossier protégé, ou un sous-dossier "
                        "`sorties/` non autorisé. Le fichier restera lisible par "
                        "l'humain (Finder / la visionneuse locale Bubble Shield) — "
                        "seul l'agent en est bloqué, et c'est la protection qui marche."
                    )
                    return
                r = _deanonymise_to_file(_wpath, args.get("content", ""))
                ok(f"✅ Document écrit : {r['path']} ({r['bytes_written']} octets, "
                   f"{r['tokens_restored']} valeur(s) réelle(s) restaurée(s)"
                   + (f", ⚠️ {r['tokens_unresolved']} jeton(s) inconnu(s) laissé(s) tel quel"
                      if r['tokens_unresolved'] else "") + "). "
                   "Le contenu réel n'est PAS affiché ici (les données du client "
                   "restent hors de ton contexte).")
            elif name == "bubble_shield_setup_ml":
                action = args.get("action", "status")
                r = _setup_start() if action == "start" else _setup_status()
                line = f"[{r['state']}] {r['message']}"
                if r.get("per_model"):
                    line += f"\n📦 {r['per_model']}"
                ok(line)
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
            elif name == "bubble_shield_add_known_pii":
                # Client-flagged MISS: the client says "you forgot X". Add X to the
                # persistent deny-list gazetteer so it is masked DETERMINISTICALLY in
                # every later doc. This is the MANUAL counterpart to the AUTO high-conf
                # path (seed_vault_into_gazetteer / maybe_add_detection) - for the
                # misses only a human catches. Gate B explicit add: no confidence check.
                value = (args.get("value") or "")
                entity_type = (args.get("entity_type") or "NOM").strip() or "NOM"
                confirm = bool(args.get("confirm", False))

                # Poka-yoke: refuse unless the agent has confirmed with the client.
                # Adding a word masks it EVERYWHERE - a common word would over-mask.
                if not confirm:
                    fail(
                        "⛔ Bubble Shield — ajout refusé : confirm=true est requis. "
                        "Avant d'ajouter, préviens le client : ce mot sera masqué "
                        "PARTOUT où il apparaît, dans tous les documents. Si c'est un "
                        "mot COURANT (prénom très répandu, mot du dictionnaire), cela "
                        "peut SUR-MASQUER du texte légitime — vérifie qu'il est assez "
                        "spécifique, puis rappelle avec confirm=true."
                    )
                    return

                # Guardrail: a literal missed WORD, not a pattern.
                if not value.strip():
                    fail("⛔ Valeur vide : fournis le mot exact que le client signale comme manqué.")
                    return
                if any(c in value for c in ("\\", "[", "]", "{", "}")):
                    fail(
                        "⛔ Cette valeur ressemble à un MOTIF (regex), pas à un mot "
                        "précis. Cet outil ajoute UN mot littéral manqué. Pour une "
                        "CATÉGORIE/motif, utilise plutôt bubble_shield_add_field "
                        "(kind=regex)."
                    )
                    return
                import re as _re
                if not _re.fullmatch(r"[A-Z][A-Z0-9_]{0,31}", entity_type):
                    fail("⛔ entity_type doit être UPPER_SNAKE, ex. NOM, ADRESSE, EMAIL.")
                    return

                sys.path.insert(0, str(_vendor()))
                from bubble_shield.known_pii_store import add_confirmed_pii as _add_known
                # path=None -> default store, which resolves BUBBLE_SHIELD_HOME at call
                # time (tests point it at a tmp store; prod uses ~/.bubble_shield).
                added = _add_known(value.strip(), entity_type)
                if added:
                    ok(
                        f"✅ « {value.strip()} » ajouté à la liste connue ({entity_type}). "
                        "Il sera DÉSORMAIS masqué automatiquement partout où il apparaît, "
                        "dans tous les documents à venir."
                    )
                else:
                    ok(
                        f"ℹ️ « {value.strip()} » est DÉJÀ dans la liste connue ({entity_type}) — "
                        "il est déjà masqué partout. Rien à faire."
                    )
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
        except ZeroDetectionError as e:
            # P0 SECURITY FIX (#589) — substantial doc, zero detections, daemon UP
            # and healthy. Fail-closed: no anonymized body, no raw PII text.
            fail(str(e))
        except MaskingIncompleteError as e:
            # P0 SECURITY FIX (#589) — STRUCTURAL TRIPWIRE: masking did not
            # provably complete (no valid AnonymizationResult/verdict_state).
            # Fail-closed: no anonymized body, no raw extracted/PII text. This
            # is the exact class of hole that let a raw 43KB client PDF return
            # with zero masking tokens and isError=false in the live P0
            # leak session — see MaskingIncompleteError's docstring.
            fail(str(e))
        except StructuredFormUnverifiedError as e:
            # P0 #589-B — a structured form (liasse/CERFA) was detected but the
            # Gemma second pass could not be trusted (daemon down/timeout/
            # malformed, or empty spans on a substantial form). Fail-closed:
            # no anonymized body, no raw text — see the class docstring.
            fail(str(e))
        except Exception as e:
            # fail-CLOSED: the exception message may embed the raw input (mail body,
            # file text, IBAN, name…) — a parser/lib error commonly quotes what it
            # choked on. NEVER interpolate str(e) into the RETURNED tool text, or raw
            # PII would leak into the model's context through this "safe" error path.
            # Log the detail host-side (STDERR only) and return a FIXED message.
            # For the two tools that RESTORE real PII (write + mail_apply), even the
            # stderr repr is unsafe (a lib error can quote the restored body, and this
            # stderr may be a persisted LaunchAgent log = PII-at-rest) → log the
            # exception TYPE only. Other tools never hold restored PII → full repr is
            # fine and preserves debuggability.
            if name in ("bubble_shield_write", "bubble_shield_mail_apply"):
                print(f"[bubble_shield] tool '{name}' failed: {type(e).__name__}",
                      file=sys.stderr, flush=True)
            else:
                print(f"[bubble_shield] tool '{name}' failed: {e!r}", file=sys.stderr, flush=True)
            if name == "bubble_shield_write":
                fail("⛔ Bubble Shield n'a pas pu écrire le document. "
                     "Aucun fichier n'a été produit. "
                     "Le contenu brut n'est PAS renvoyé (sécurité).")
            elif name == "bubble_shield_mail_apply":
                fail("⛔ Bubble Shield n'a pas pu appliquer les décisions e-mail. "
                     "Le contenu (brouillon / valeurs réelles) n'est PAS renvoyé (sécurité).")
            elif name == "bubble_shield_list":
                # bubble_shield_list does NO anonymisation — it just enumerates a
                # folder's entries (names in clear). Labelling its failures
                # "Échec de l'anonymisation" sent debugging down the wrong path
                # (daemon? indexing?) when the real cause is a plain filesystem
                # error (folder not found, unreadable, dataless cloud dir). Give
                # an honest, actionable message + the exception TYPE (never str(e),
                # which could quote a path/PII). Common cause: the folder doesn't
                # exist (a mistyped path, e.g. "client" vs "clients").
                fail("⛔ Bubble Shield n'a pas pu lister ce dossier "
                     f"({type(e).__name__}). Vérifiez que le chemin existe et est "
                     "lisible (dossier absent, non hydraté sur le cloud, ou "
                     "permissions). Ce n'est PAS une erreur d'anonymisation.")
            else:
                fail("⛔ Échec de l'anonymisation. "
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
