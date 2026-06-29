"""
app.py — local demo webapp for the Bubble Shield anonymiser.

100% local: bind 127.0.0.1, no outbound calls, the clear text never leaves
the process. Shows before/after side by side, per-entity diff, the mapping
table, and the fail-closed verdict — so a human can *see* what would (and
would not) be sent to Claude.

Run:  uvicorn webapp.app:app --host 127.0.0.1 --port 8765
"""
from __future__ import annotations

import io
import re
import os
import zipfile
from pathlib import Path
from typing import List

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from bubble_shield.engine import AnonymizationEngine
from bubble_shield.vault import Vault
from webapp.extract import ExtractionError, extract_text
from webapp.render import highlight_after, highlight_before


def _new_vault(mission: str):
    """A fresh vault, surrogate or opaque per BUBBLE_SHIELD_SURROGATE. For the dossier
    route we build ONE of these and share it across all files."""
    if os.environ.get("BUBBLE_SHIELD_SURROGATE") == "1":
        from bubble_shield.surrogate import SurrogateVault
        return SurrogateVault(mission=mission or "demo")
    return Vault(mission=mission or "demo")


def _build_engine(mission: str, vault=None) -> tuple[AnonymizationEngine, "object"]:
    """Build the demo engine. Hybrid (regex ⊕ GLiNER ⊕ form recognizers) with the
    firm allowlist when BUBBLE_SHIELD_HYBRID=1 and GLiNER is installed; otherwise plain
    regex. Fail-open: any import/load problem degrades silently to regex-only so
    the demo never breaks. Pass `vault` to SHARE one vault across a dossier's
    files (consistent tokens/surrogates). Returns (engine, allowlist_or_None).
    """
    # Surrogate mode (opt-in, BUBBLE_SHIELD_SURROGATE=1): realistic fake values instead
    # of opaque ⟦tokens⟧. Off by default (opaque fails safer for compliance).
    if vault is None:
        vault = _new_vault(mission)
    # User cloak/keep policy (config table): drop matches the user chose to KEEP
    # (e.g. € amounts). Composed AFTER the firm allowlist so both apply.
    from bubble_shield.policy import load_policy, make_match_filter
    policy_filter = make_match_filter(load_policy())

    if os.environ.get("BUBBLE_SHIELD_HYBRID", "1") != "1":
        return AnonymizationEngine(vault=vault, match_filter=policy_filter), None
    try:
        from bubble_shield.gliner_ext import make_gliner_detector
        from bubble_shield.structured_ext import make_structured_detector
        from bubble_shield.allowlist import DEPLOYMENT_ALLOWLIST

        def _combined_filter(matches):
            # firm allowlist first (drop firm/regulator/fund), then user policy.
            return policy_filter(DEPLOYMENT_ALLOWLIST.filter(matches))

        eng = AnonymizationEngine(
            vault=vault,
            extra_detectors=[make_gliner_detector(), make_structured_detector()],
            match_filter=_combined_filter,
        )
        return eng, DEPLOYMENT_ALLOWLIST
    except Exception:
        return AnonymizationEngine(vault=vault, match_filter=policy_filter), None

BASE = Path(__file__).parent
# Local append-only processing record (RGPD art. 30). Counts/types only, never
# values.
# Priority: BUBBLE_SHIELD_AUDIT_LOG > BUBBLE_SHIELD_HOME/audit.jsonl > webapp/data/audit.jsonl
# The BUBBLE_SHIELD_HOME fallback is used by the native launcher (Phase 2) so
# audit data lives in ~/.bubble_shield/ instead of inside the repo tree.
def _resolve_audit_log() -> str:
    explicit = os.environ.get("BUBBLE_SHIELD_AUDIT_LOG")
    if explicit:
        return explicit
    shield_home = os.environ.get("BUBBLE_SHIELD_HOME")
    if shield_home:
        p = Path(shield_home) / "audit.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        return str(p)
    return str(BASE / "data" / "audit.jsonl")


AUDIT_LOG = _resolve_audit_log()


def _audit(result, *, mission: str, event: str, **meta) -> None:
    """Append an audit entry; never let logging break anonymisation."""
    try:
        from bubble_shield.audit import log_result
        log_result(AUDIT_LOG, result, mission=mission or "demo", event=event, **meta)
    except Exception:
        pass


def _audit_event(*, mission: str, event: str, **meta) -> None:
    """Append a management-event audit line (counts/types only, never raw PII).

    Used by the Tier-3 gazetteer/vault surfaces. The _audit() helper above is
    shaped around an AnonymizationResult; these UI events have none, so we call
    the low-level append_entry writer directly. Never raises.
    """
    try:
        from bubble_shield.audit import append_entry
        append_entry(AUDIT_LOG, mission=mission or "demo", event=event, **meta)
    except Exception:
        pass


def _mask_value(value: str) -> str:
    """Mask a PII value for display: keep first 2 chars of each whitespace-token, dot the rest."""
    parts = []
    for tok in str(value).split():
        if len(tok) <= 2:
            parts.append(tok[0] + "•" if tok else tok)
        else:
            parts.append(tok[:2] + "•" * min(len(tok) - 2, 6))
    return " ".join(parts) if parts else "•••"


app = FastAPI(title="Bubble Shield — Anonymiseur (démo locale)")

# Short-lived in-memory cache of generated dossier zips, so the results page can
# offer a one-click download without re-running the (slow) anonymisation. Demo
# scope only: single process, capped, never persisted to disk.
_ZIP_CACHE: dict[str, tuple[str, bytes]] = {}
templates = Jinja2Templates(directory=str(BASE / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")

# A realistic-but-fictional sample so the demo is one click away.
SAMPLE = (
    "Compte rendu — entretien patrimonial\n\n"
    "Le client Monsieur Jean Dupont (n° client 640723), né le 14/03/1968, "
    "demeurant 12 rue des Lilas, 75019 Paris, nous a contactés par e-mail "
    "(jean.dupont@example.com) et au 06 12 34 56 78.\n"
    "Il détient un PEA chez notre partenaire (IBAN FR76 3000 6000 0112 3456 7890 189) "
    "investi notamment sur l'action Air Liquide (ISIN FR0000120073) pour 45 000 €.\n"
    "Sa société, la SARL Dupont Conseil (SIREN 552 100 554), a réalisé un apport. "
    "Son épouse Marie Dupont prépare un projet de cession ; un divorce est évoqué."
)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        request, "index.html", {"sample": SAMPLE, "result": None})


@app.get("/about", response_class=HTMLResponse)
def about(request: Request):
    """Plain-language "how it works" page — for a non-technical operator/client
    who wants to understand the inner workings (not just trust a black box)."""
    return templates.TemplateResponse(request, "about.html", {})


@app.post("/anonymize", response_class=HTMLResponse)
async def anonymize(
    request: Request,
    text: str = Form(""),
    mission: str = Form("demo"),
    document: UploadFile | None = None,
):
    if document is not None and document.filename:
        raw = await document.read()
        try:
            text = extract_text(document.filename, raw)
        except ExtractionError as exc:
            # Show the reason on the home page rather than anonymising garbage.
            return templates.TemplateResponse(
                request, "index.html",
                {"sample": text or SAMPLE, "result": None, "error": str(exc)})

    engine, allowlist = _build_engine(mission)
    # Two-pass self-improving sweep when the hybrid is on: pass 1 discovers the
    # client's PII, pass 2 sweeps every occurrence (incl. detached repeats like a
    # lone spouse first name the single pass misses). Falls back to one pass if
    # the profile-sweep module isn't importable.
    result = engine.anonymize(text or "")
    if allowlist is not None:
        try:
            from bubble_shield.profile_sweep import two_pass_detect
            result, _profile = two_pass_detect(text or "", engine,
                                               allowlist=allowlist)
        except Exception:
            pass  # keep the single-pass result

    # Operator-facing breakdown (clearer than the raw binary verdict): how many
    # items were CONFIDENTLY redacted vs flagged for a quick human review. Every
    # detected entity is anonymised either way; "to review" are the lower-
    # confidence ones (often form labels the model guessed at) — a glance, not a
    # failure. This drives a 3-part verdict instead of a scary "DON'T SEND".
    confident = [e for e in result.entities if e.score >= result.threshold]
    to_review = [e for e in result.entities if e.score < result.threshold]

    _audit(result, mission=mission, event="anonymize")

    # #334 — LOUD WARNING when an identifying type is set to KEEP in the policy.
    from bubble_shield.policy import kept_identifying_types, load_policy as _load_policy
    kept_identifying = kept_identifying_types(_load_policy())

    ctx = {
        "sample": SAMPLE,
        "result": result,
        "mission": mission,
        "before_html": highlight_before(result),
        "after_html": highlight_after(result),
        "n_confident": len(confident),
        "to_review": to_review,
        # round-trip proof: restoring the anonymised text yields the original
        "roundtrip_ok": engine.deanonymize(result.anonymized) == (text or ""),
        # #334: non-empty list of FR labels when the policy keeps identifying types.
        "kept_identifying": kept_identifying,
    }
    return templates.TemplateResponse(request, "result.html", ctx)


@app.post("/anonymize-dossier")
async def anonymize_dossier_route(
    request: Request,
    mission: str = Form("dossier"),
    documents: List[UploadFile] = None,  # noqa: B008
):
    """Anonymise a WHOLE dossier (many files) through ONE shared vault + profile,
    so the same client is anonymised CONSISTENTLY across every file (same token /
    fake everywhere) and is caught even in files where a single pass would miss
    him. Returns a ZIP of the anonymised .txt files + a correspondence table.

    The clear text never leaves the process; only the ANONYMISED zip is returned.
    """
    from bubble_shield.dossier import anonymize_dossier

    documents = documents or []
    # Extract text from every uploaded file (skip those we can't read, with a note).
    docs: list[tuple[str, str]] = []
    skipped: list[tuple[str, str]] = []
    for up in documents:
        if not up or not up.filename:
            continue
        raw = await up.read()
        try:
            docs.append((up.filename, extract_text(up.filename, raw)))
        except ExtractionError as exc:
            skipped.append((up.filename, str(exc)))

    if not docs:
        return templates.TemplateResponse(
            request, "index.html",
            {"sample": SAMPLE, "result": None,
             "error": "Aucun fichier lisible. " + (skipped[0][1] if skipped else "")})

    # One shared vault (consistency) + shared profile (cross-file recall).
    shared_vault = _new_vault(mission)

    def factory():
        eng, allowlist = _build_engine(mission, vault=shared_vault)
        return eng, allowlist

    dres = anonymize_dossier(docs, engine_factory=factory)

    # Audit record per file (counts/types only, never values).
    for f in dres.files:
        _audit(f.result, mission=mission, event="dossier",
               file_count=len(dres.files))

    # Strip any client/firm name out of the OUTPUT filenames too — the source
    # PDFs are named "DCC - Monsieur NOM CLIENT…", and a privacy tool must not
    # leak the client in the filename. Collect words to strip from THREE sources:
    #   - every vault NOM value (the client + family we detected),
    #   - the firm/regulator deployment allowlist,
    #   - the French first-name gazetteer (catches a lone "Sébastien" in a name
    #     the profile happened not to learn as a full string).
    name_words = set()
    for real, tok in dres.vault.to_token.items():
        if "NOM" in str(tok):
            for w in re.split(r"[\s\-]+", real):
                if len(w) >= 4:
                    name_words.add(w.lower())
    try:
        from bubble_shield.allowlist import DEPLOYMENT_ALLOWLIST
        for p in DEPLOYMENT_ALLOWLIST.phrases:
            for w in re.split(r"[\s\-]+", p):
                if len(w) >= 4:
                    name_words.add(w.lower())
    except Exception:
        pass
    try:
        from bubble_shield.gazetteer import FRENCH_FIRST_NAMES
        first_names = {n.lower() for n in FRENCH_FIRST_NAMES}
    except Exception:
        first_names = set()

    # Known CGP/CIF document types → a clean fallback label when stripping the
    # client name leaves nothing meaningful (e.g. only a date). Maps a keyword
    # found in the ORIGINAL filename to a readable type.
    _DOC_TYPES = [
        ("convention rto", "Convention RTO"), ("convention", "Convention"),
        ("lm cif", "Lettre de mission CIF"), ("dcc", "Connaissance client (DCC)"),
        ("connaissance client", "Connaissance client (DCC)"),
        ("der", "Entrée en relation (DER)"), ("entrée en relation", "Entrée en relation (DER)"),
        ("profil", "Profil investisseur"), ("adéquation", "Déclaration d'adéquation"),
        (" da ", "Déclaration d'adéquation"), ("dic", "DIC / KID"), ("kid", "DIC / KID"),
        ("bulletin", "Bulletin de souscription"), (" bs ", "Bulletin de souscription"),
        ("annexe", "Annexe"), ("souscription", "Souscription"),
    ]

    def _doc_type(original: str):
        low = " " + Path(original).stem.lower() + " "
        for key, label in _DOC_TYPES:
            if key in low:
                return label
        return None

    def _safe_name(original: str, idx: int) -> str:
        stem = Path(original).stem
        # strip whole known values first (addresses, emails…)
        for real in dres.vault.to_value.values():
            r = str(real).strip()
            if len(r) >= 4 and r.lower() in stem.lower():
                stem = re.sub(re.escape(r), "", stem, flags=re.IGNORECASE)
        # strip name words + any gazetteer first name appearing as a token
        for w in set(re.split(r"[\s\-_]+", stem)):
            wl = w.lower()
            if len(wl) >= 4 and (wl in name_words or wl in first_names):
                stem = re.sub(r"(?i)(?<![\wÀ-ÿ])" + re.escape(w) + r"(?![\wÀ-ÿ])", "", stem)
        stem = re.sub(r"[\s\-_]{2,}", " ", stem).strip(" -_")
        # Fallback: if stripping left nothing meaningful (empty, or just a date /
        # number / short code), use the recognised document TYPE so the file stays
        # identifiable without leaking the client.
        meaningful = re.sub(r"[\d\s\-/_().]+", "", stem)  # what's left after numbers/dates
        if len(meaningful) < 3:
            stem = _doc_type(original) or f"document-{idx}"
        return f"{idx:02d} - {stem}"

    # Build the ZIP in memory: one anonymised .txt per file + a vault table.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, f in enumerate(dres.files, 1):
            zf.writestr(f"anonymise/{_safe_name(f.name, i)}.txt", f.result.anonymized)
        # Correspondence table (the vault) — stays with the operator, local only.
        lines = ["# Table de correspondance — LOCALE, ne pas partager",
                 "# jeton/substitut\tvaleur réelle", ""]
        for tok, real in sorted(dres.vault.to_value.items()):
            lines.append(f"{tok}\t{real}")
        zf.writestr("coffre/table-de-correspondance.tsv", "\n".join(lines))
        # A short manifest.
        man = [f"Dossier : {mission}",
               f"Fichiers anonymisés : {dres.n_ok}",
               f"Entités protégées (total) : {dres.total_entities}",
               f"Tous sûrs à envoyer : {'oui' if dres.all_safe else 'à revoir'}"]
        if skipped:
            man.append("\nNon lisibles (scannés / chiffrés) :")
            man += [f"  - {n} : {why}" for n, why in skipped]
        zf.writestr("LISEZMOI.txt", "\n".join(man))
    buf.seek(0)

    # Stash the zip for a one-click download from the results page (short-lived,
    # in-memory, single-process demo). Then render an on-screen results view —
    # like single-file mode — instead of dumping a silent download.
    import uuid
    dl_id = uuid.uuid4().hex
    fname = f"{mission or 'dossier'}-anonymise.zip".replace(" ", "_")
    _ZIP_CACHE[dl_id] = (fname, buf.getvalue())
    # Cap the cache so it can't grow unbounded across a long demo session.
    if len(_ZIP_CACHE) > 12:
        for k in list(_ZIP_CACHE)[:-12]:
            _ZIP_CACHE.pop(k, None)

    # Per-file summary + the before/after view (like single-file mode), so the
    # operator can expand any document and SEE what was protected. No PII values
    # leave the process — before_html is the local original (shown on-screen only).
    file_rows = []
    for i, f in enumerate(dres.files, 1):
        r = f.result
        n_review = sum(1 for e in r.entities if e.score < r.threshold)
        file_rows.append({
            "idx": i,
            "name": _safe_name(f.name, i) + ".pdf",   # display the de-identified name
            "entities": r.entity_count,
            "to_review": n_review,
            "safe": r.safe_to_send,
            "before_html": highlight_before(r),
            "after_html": highlight_after(r),
        })
    # How consistent is the client across files? Count files that reference the
    # most-used person token (the headline "même jeton partout" story).
    consistency = _dossier_consistency(dres)

    ctx = {
        "mission": mission,
        "n_files": dres.n_ok,
        "total_entities": dres.total_entities,
        "all_safe": dres.all_safe,
        "file_rows": file_rows,
        "skipped": skipped,
        "vault_size": len(dres.vault.to_value),
        "consistency": consistency,
        "download_id": dl_id,
        "surrogate": os.environ.get("BUBBLE_SHIELD_SURROGATE") == "1",
    }
    return templates.TemplateResponse(request, "dossier_result.html", ctx)


def _dossier_consistency(dres) -> dict:
    """The cross-file consistency headline: the most-referenced person token and
    in how many of the dossier's files it appears (no PII values exposed)."""
    import re as _re
    # find the person token (NOM_n, ignoring variant letters) used in most files
    best_files = 0
    best_persons = 0
    persons = set()
    for real, tok in dres.vault.to_token.items():
        m = _re.search(r"NOM_(\d+)", str(tok))
        if m:
            persons.add(m.group(1))
    best_persons = len(persons)
    if persons:
        # how many files contain the most-common person token base?
        from collections import Counter
        c = Counter()
        for num in persons:
            base = f"NOM_{num}"
            files_with = sum(1 for f in dres.files if base in f.result.anonymized)
            c[num] = files_with
        if c:
            best_files = max(c.values())
    return {"persons": best_persons, "client_in_files": best_files,
            "total_files": len(dres.files)}


@app.get("/dossier-download/{dl_id}")
def dossier_download(dl_id: str):
    """Serve the zip stashed by the dossier run (one-click from the results page)."""
    item = _ZIP_CACHE.get(dl_id)
    if not item:
        return HTMLResponse("Lien expiré — relancez l'anonymisation du dossier.",
                            status_code=404)
    fname, data = item
    return StreamingResponse(
        io.BytesIO(data), media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'})


def _detector_state() -> dict:
    """Return the current detector mode and whether OpenAI mode is actually runnable.

    Reads detector.mode from the custom_fields config (gliner | openai | both).
    OpenAI mode requires onnxruntime >= 1.27 (bubbleshield openai_pf_ext); if that
    import fails we report it as unavailable so the UI is honest.
    """
    from bubble_shield.custom_recognizers import load_custom_fields_config
    cfg = load_custom_fields_config()
    mode = cfg.get("detector", {}).get("mode", "gliner")

    # Probe whether the OpenAI privacy-filter extension can actually be loaded.
    openai_available = False
    try:
        from bubble_shield import openai_pf_ext as _opf  # noqa: F401
        openai_available = True
    except Exception:
        pass

    return {
        "mode": mode,
        "openai_available": openai_available,
        "modes": ["gliner", "openai", "both"],
    }


def _custom_fields_view() -> dict:
    """Return custom fields and keep-list for the webapp panel."""
    from bubble_shield.custom_recognizers import load_custom_fields_config, _config_locations
    cfg = load_custom_fields_config()
    regex_fields = cfg.get("regex_fields", [])
    gliner_labels = cfg.get("gliner_labels", [])
    keep_list = cfg.get("keep_list", [])
    # Surface the config file path so users know where it lives.
    locations = _config_locations()
    config_path = str(locations[0]) if locations else "~/.config/bubble_shield/custom_fields.json"
    return {
        "regex_fields": regex_fields,
        "gliner_labels": gliner_labels,
        "keep_list": keep_list,
        "config_path": config_path,
    }


def _save_detector_mode(mode: str) -> bool:
    """Persist detector.mode in the custom_fields.json config. Returns True on success."""
    import json
    from bubble_shield.custom_recognizers import load_custom_fields_config, _config_locations
    valid_modes = {"gliner", "openai", "both"}
    if mode not in valid_modes:
        return False
    locations = _config_locations()
    if not locations:
        return False
    config_path = Path(locations[0])
    cfg = load_custom_fields_config()
    if "detector" not in cfg or not isinstance(cfg["detector"], dict):
        cfg["detector"] = {}
    cfg["detector"]["mode"] = mode
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def _save_custom_field(field_type: str, kind: str, value: str, label: str = "") -> dict:
    """Validate via pii_guard and then persist a new custom field to the config.

    Returns {"ok": True} or {"ok": False, "reason": "..."}.
    Single source of truth: same pii_guard.check_input() the MCP path uses.
    """
    import json
    from bubble_shield.pii_guard import check_input
    from bubble_shield.custom_recognizers import load_custom_fields_config, _config_locations

    guard = check_input(value, kind)
    if not guard["ok"]:
        return guard

    # entity_type validation
    import re as _re
    if kind == "regex" and not _re.fullmatch(r"[A-Z][A-Z0-9_]{1,31}", field_type):
        return {"ok": False, "reason": f"entity_type invalide : {field_type!r}. "
                "Format requis : lettres majuscules, chiffres et _ (ex: MON_CHAMP)."}

    locations = _config_locations()
    if not locations:
        return {"ok": False, "reason": "Aucun chemin de configuration trouvé."}
    config_path = Path(locations[0])
    cfg = load_custom_fields_config()

    if kind == "regex":
        entry = {
            "entity_type": field_type,
            "label": label or field_type,
            "pattern": value,
            "ignore_case": False,
            "cloak": True,
            "priority": 65,
            "score_if_unvalidated": 0.6,
        }
        cfg.setdefault("regex_fields", [])
        # Replace existing entry with same entity_type
        cfg["regex_fields"] = [f for f in cfg["regex_fields"]
                               if f.get("entity_type") != field_type]
        cfg["regex_fields"].append(entry)

    elif kind == "gliner_label":
        entry = {"label": value, "entity_type": field_type or value.upper().replace(" ", "_")}
        cfg.setdefault("gliner_labels", [])
        cfg["gliner_labels"] = [f for f in cfg["gliner_labels"]
                                if f.get("label") != value]
        cfg["gliner_labels"].append(entry)

    elif kind == "keep":
        cfg.setdefault("keep_list", [])
        if value not in cfg["keep_list"]:
            cfg["keep_list"].append(value)

    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "reason": f"Erreur d'écriture : {exc}"}


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    """Post-send risk control: how the anonymiser has been used (webapp + the
    Cowork skill both log here), how many runs were flagged unsafe, errors, and
    which entity types showed up — so a human can audit the policy after the fact.
    Plus the editable cloak/keep config table (including custom fields),
    the custom-fields section, and the detector-mode selector."""
    from bubble_shield.audit import read_audit
    from webapp.dashboard import summarize
    from bubble_shield.policy import load_policy, extended_policy_view

    stats = summarize(read_audit(AUDIT_LOG))
    rows = extended_policy_view(load_policy())
    custom = _custom_fields_view()
    detector = _detector_state()
    return templates.TemplateResponse(
        request, "dashboard.html",
        {
            "stats": stats,
            "policy_rows": rows,
            "saved": False,
            "custom": custom,
            "detector": detector,
            "field_error": None,
            "field_saved": False,
            "detector_saved": False,
        },
    )


@app.post("/dashboard/policy", response_class=HTMLResponse)
async def save_policy_route(request: Request):
    """Persist the user's cloak/keep choices from the config table. A checkbox is
    present in the form iff the user wants that type CLOAKED; absent → KEEP.

    Now uses extended_policy_view so custom fields appear in the table too."""
    from bubble_shield.audit import read_audit
    from webapp.dashboard import summarize
    from bubble_shield.policy import (
        ENTITY_CATALOG, save_policy, load_policy,
        extended_policy_view, custom_entity_catalog,
        is_identifying, enforce_identifying_floor,
    )

    form = await request.form()
    # Standard entity types
    new_policy = {etype: (f"cloak_{etype}" in form) for etype in ENTITY_CATALOG}
    # Custom entity types (they appear in the extended view with the same toggle convention)
    custom_cat = custom_entity_catalog()
    for etype in custom_cat:
        # Custom fields are treated as identifying — always cloak (is_identifying
        # returns True for unknown types). Honour the form only for the rare
        # non-identifying custom field.
        new_policy[etype] = (f"cloak_{etype}" in form) or is_identifying(etype)
    # #392 floor: an all-unchecked (or partial) form must NEVER be able to save a
    # mask-nothing policy. Identifying types are forced to cloak regardless of the
    # checkboxes; only non-identifying types honour the form toggle.
    new_policy = enforce_identifying_floor(new_policy)
    save_policy(new_policy)

    stats = summarize(read_audit(AUDIT_LOG))
    rows = extended_policy_view(load_policy())
    custom = _custom_fields_view()
    detector = _detector_state()
    return templates.TemplateResponse(
        request, "dashboard.html",
        {
            "stats": stats,
            "policy_rows": rows,
            "saved": True,
            "custom": custom,
            "detector": detector,
            "field_error": None,
            "field_saved": False,
            "detector_saved": False,
        },
    )


@app.post("/dashboard/detector", response_class=HTMLResponse)
async def save_detector_route(request: Request):
    """Persist the detector mode (gliner | openai | both) to the config."""
    from bubble_shield.audit import read_audit
    from webapp.dashboard import summarize
    from bubble_shield.policy import load_policy, extended_policy_view

    form = await request.form()
    mode = str(form.get("detector_mode", "gliner"))
    _save_detector_mode(mode)

    stats = summarize(read_audit(AUDIT_LOG))
    rows = extended_policy_view(load_policy())
    custom = _custom_fields_view()
    detector = _detector_state()
    return templates.TemplateResponse(
        request, "dashboard.html",
        {
            "stats": stats,
            "policy_rows": rows,
            "saved": False,
            "custom": custom,
            "detector": detector,
            "field_error": None,
            "field_saved": False,
            "detector_saved": True,
        },
    )


@app.post("/dashboard/custom-field", response_class=HTMLResponse)
async def add_custom_field_route(request: Request):
    """Add a new custom field (regex | gliner_label | keep) through the pii_guard
    so the EXACT same guard-rail the MCP path uses is enforced in the webapp too.
    Single source of truth — the guard logic lives in pii_guard.py, not here."""
    from bubble_shield.audit import read_audit
    from webapp.dashboard import summarize
    from bubble_shield.policy import load_policy, extended_policy_view

    form = await request.form()
    kind = str(form.get("kind", "regex"))
    field_type = str(form.get("field_type", "")).strip().upper()
    value = str(form.get("value", "")).strip()
    label = str(form.get("label", "")).strip()

    result = _save_custom_field(field_type, kind, value, label)

    stats = summarize(read_audit(AUDIT_LOG))
    rows = extended_policy_view(load_policy())
    custom = _custom_fields_view()
    detector = _detector_state()
    return templates.TemplateResponse(
        request, "dashboard.html",
        {
            "stats": stats,
            "policy_rows": rows,
            "saved": False,
            "custom": custom,
            "detector": detector,
            "field_error": None if result["ok"] else result.get("reason", "Erreur inconnue."),
            "field_saved": result["ok"],
            "detector_saved": False,
        },
    )


@app.get("/health-noauth")
def health():
    return {"status": "ok"}


# ═══════════════════════════════════════════════════════════════════════════════
# REVIEW QUEUE UI — Phase 3
# Drives the Phase-1 review_queue API (confirm / dismiss / feed / expire).
# Shows real candidate values — this is the client's own data on their own
# host-native app; no telemetry, no network except localhost↔itself.
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/review", response_class=HTMLResponse)
def review_inbox(request: Request):
    """Inbox: pending candidates, most-recurring first.

    On load:
    1. Drain ALL Phase-0 sidecars (feed_from_sidecar_all) — fail-open.
    2. Expire stale items (#3 backstop) — fail-open.
    3. Render pending list with Confirmer / Ignorer buttons.
    """
    import bubble_shield.review_queue as rq

    flash = request.query_params.get("flash")

    # 1 — drain EVERY candidate sidecar (fail-open).
    # The plugin/daemon writes sub-threshold candidates under whatever mission
    # (BUBBLE_SHIELD_SESSION, default 'mcp-session') was active — NOT 'demo'.
    # Draining only one hardcoded mission left real candidates orphaned and the
    # HITL review loop inert (#394). Glob and drain all dossiers so the reviewer
    # sees every pending sub-threshold candidate in one File de révision.
    try:
        rq.feed_from_sidecar_all()
    except Exception:
        pass

    # 2 — expire stale items (fail-open)
    try:
        rq.expire_old()
    except Exception:
        pass

    # 3 — fetch pending items
    try:
        pending = rq.list_pending()
    except Exception:
        pending = []

    return templates.TemplateResponse(
        request,
        "review.html",
        {
            "pending": pending,
            "flash": flash,
            "pending_count": len(pending),
        },
    )


@app.post("/review/confirm")
async def review_confirm(request: Request, normalized: str = Form(...)):
    """HITL Gate B: confirm the candidate IS genuine PII.

    Calls review_queue.confirm(normalized) → writes to gazetteer + drains item.
    Redirects back to /review with a success flash.
    """
    import bubble_shield.review_queue as rq
    from fastapi.responses import RedirectResponse

    try:
        rq.confirm(normalized)
        flash = "confirme"
    except Exception:
        flash = "erreur"

    return RedirectResponse(url=f"/review?flash={flash}", status_code=303)


@app.post("/review/dismiss")
async def review_dismiss(request: Request, normalized: str = Form(...)):
    """Dismiss a candidate — not PII, no action needed.

    Calls review_queue.dismiss(normalized) → moves to dismissed_log.
    Redirects back to /review with a success flash.
    """
    import bubble_shield.review_queue as rq
    from fastapi.responses import RedirectResponse

    try:
        rq.dismiss(normalized)
        flash = "ignore"
    except Exception:
        flash = "erreur"

    return RedirectResponse(url=f"/review?flash={flash}", status_code=303)


@app.get("/review/dismissed", response_class=HTMLResponse)
def review_dismissed(request: Request):
    """Audit log: all confirmed + dismissed + auto-expired items (read-only)."""
    import bubble_shield.review_queue as rq

    try:
        dismissed = rq.list_dismissed()
    except Exception:
        dismissed = []

    return templates.TemplateResponse(
        request,
        "review_dismissed.html",
        {"dismissed": dismissed, "dismissed_count": len(dismissed)},
    )


@app.get("/gazetteer", response_class=HTMLResponse)
def gazetteer_view(request: Request):
    """List confirmed known-PII (masked) with a per-row remove button."""
    from bubble_shield.known_pii_store import load_gazetteer
    import base64
    try:
        gz = load_gazetteer()
        rows = [{"value_b64": base64.urlsafe_b64encode(e.value.encode()).decode(),
                 "masked": _mask_value(e.value), "entity_type": e.entity_type}
                for e in gz.entries]
    except Exception:
        rows = []
    flash = request.query_params.get("flash")
    return templates.TemplateResponse(
        request, "gazetteer.html", {"rows": rows, "flash": flash, "count": len(rows)}
    )


@app.post("/gazetteer/remove")
async def gazetteer_remove(request: Request, value: str = Form(""), value_b64: str = Form("")):
    """Remove one entry from the gazetteer; audit (no raw value).

    The listing page posts ``value_b64`` (base64 of the raw value) so the
    cleartext is never in the page DOM (masking constraint). A direct caller
    may post ``value`` (raw) instead. ``value`` takes precedence if both set.
    """
    from bubble_shield.known_pii_store import load_gazetteer, remove_pii
    from fastapi.responses import RedirectResponse
    if not value and value_b64:
        import base64
        try:
            value = base64.urlsafe_b64decode(value_b64.encode()).decode()
        except Exception:
            value = ""
    etype = "NOM"
    try:
        etype = load_gazetteer().entity_type_of(value)
    except Exception:
        pass
    try:
        ok = remove_pii(value)
        flash = "retire" if ok else "absent"
        _audit_event(mission="gazetteer", event="gazetteer_remove",
                     entity_type=etype, counts={etype: 1})
    except Exception:
        flash = "erreur"
    return RedirectResponse(url=f"/gazetteer?flash={flash}", status_code=303)


@app.post("/safe/add")
async def safe_add(request: Request, value: str = Form(""), value_b64: str = Form(""),
                   confirm: str = Form("")):
    """Mark a wrongly-masked word as NOT PII → add it to the self-improving safe-list
    (#348 Task 4). The vault / review "un-hide as not-PII" action posts here.

    Typed-confirm: requires ``confirm=SUR`` (mirrors the Tier-3 forget OUBLIER gate).
    The caller posts ``value_b64`` (base64 of the raw value) so the cleartext is never
    in the page DOM (masking constraint); a direct caller may post raw ``value``.
    Audit carries entity-type/counts only — NEVER the raw value.
    FAIL-OPEN: a safe-list write failure must never break the request.
    """
    from fastapi.responses import RedirectResponse
    if confirm != "SUR":
        return RedirectResponse(url="/review?flash=annule", status_code=303)
    if not value and value_b64:
        import base64
        try:
            value = base64.urlsafe_b64decode(value_b64.encode()).decode()
        except Exception:
            value = ""
    if not value.strip():
        return RedirectResponse(url="/review?flash=absent", status_code=303)
    try:
        from bubble_shield import safe_words as _sw
        _sw.add_safe(value)
        # NOM is the only type the safe-list governs (over-masked person-name
        # false positives). Audit value-free: type + count + event only.
        _audit_event(mission="safe", event="safe_add",
                     entity_type="NOM", counts={"NOM": 1})
        flash = "sur"
    except Exception:
        flash = "erreur"
    return RedirectResponse(url=f"/review?flash={flash}", status_code=303)


def _vaults_dir():
    from pathlib import Path
    import os
    home = Path(os.environ.get("BUBBLE_SHIELD_HOME", str(Path.home() / ".bubble_shield")))
    return home / "vaults"


def _load_vault_or_none(mission: str):
    p = _vaults_dir() / f"{mission}.vault.json"
    if not p.exists():
        return None
    try:
        return Vault.load(p)
    except Exception:
        return None


@app.get("/vault", response_class=HTMLResponse)
def vault_missions(request: Request):
    """List available missions (vault files), most-recent first."""
    d = _vaults_dir()
    missions = []
    try:
        files = sorted(d.glob("*.vault.json"), key=lambda f: f.stat().st_mtime, reverse=True)
        missions = [f.name[: -len(".vault.json")] for f in files]
    except Exception:
        missions = []
    return templates.TemplateResponse(request, "vault_missions.html", {"missions": missions})


@app.get("/vault/{mission}", response_class=HTMLResponse)
def vault_detail(request: Request, mission: str):
    """Token table for a mission. Values MASKED — raw value never in the DOM.

    Rows carry ONLY the token (not PII), its bracket-stripped inner form, and the
    masked display string. The cleartext is fetched per-row via the reveal route;
    rectify/forget act by TOKEN, so the raw value is never needed in the page.
    """
    v = _load_vault_or_none(mission)
    flash = request.query_params.get("flash")
    if v is None:
        return templates.TemplateResponse(
            request, "vault_detail.html",
            {"mission": mission, "rows": [], "missing": True, "flash": flash})
    rows = [{"token": tok, "token_inner": tok.strip("⟦⟧"),
             "masked": _mask_value(val)}
            for val, tok in v.to_token.items()]
    return templates.TemplateResponse(
        request, "vault_detail.html",
        {"mission": mission, "rows": rows, "missing": False, "flash": flash})


@app.get("/vault/{mission}/reveal/{token_inner}")
def vault_reveal(mission: str, token_inner: str):
    """Return ONE cleartext value for a token (deliberate reveal). Audited."""
    from fastapi.responses import JSONResponse
    v = _load_vault_or_none(mission)
    if v is None:
        return JSONResponse({"error": "coffre introuvable"}, status_code=404)
    token = f"⟦{token_inner}⟧"
    value = v.value_for(token)
    if value is None:
        return JSONResponse({"error": "jeton inconnu"}, status_code=404)
    etype = token_inner.split("_")[0] if "_" in token_inner else "NOM"
    _audit_event(mission=mission, event="vault_reveal", token=token_inner,
                 entity_type=etype, counts={etype: 1})
    return JSONResponse({"token": token_inner, "value": value})


@app.post("/vault/{mission}/rectify")
async def vault_rectify(request: Request, mission: str,
                        token: str = Form(...), new_value: str = Form(...)):
    """Correct the cleartext behind a token, keeping the token (RGPD art.16)."""
    from fastapi.responses import RedirectResponse
    v = _load_vault_or_none(mission)
    if v is None:
        return RedirectResponse(url=f"/vault/{mission}?flash=introuvable", status_code=303)
    full = f"⟦{token}⟧"
    old = v.value_for(full)
    etype = token.split("_")[0] if "_" in token else "NOM"
    flash = "absent"
    if old is not None and new_value.strip():
        try:
            if v.rectify(old, new_value.strip()):
                v.save(_vaults_dir() / f"{mission}.vault.json")
                _audit_event(mission=mission, event="vault_rectify", token=token,
                             entity_type=etype, counts={etype: 1})
                flash = "corrige"
        except Exception:
            flash = "erreur"
    return RedirectResponse(url=f"/vault/{mission}?flash={flash}", status_code=303)


@app.post("/vault/{mission}/forget")
async def vault_forget(request: Request, mission: str,
                       token: str = Form(...), confirm: str = Form("")):
    """Forget ONE mapping. Requires confirm=OUBLIER. Destructive (breaks round-trip)."""
    from fastapi.responses import RedirectResponse
    if confirm != "OUBLIER":
        return RedirectResponse(url=f"/vault/{mission}?flash=annule", status_code=303)
    v = _load_vault_or_none(mission)
    if v is None:
        return RedirectResponse(url=f"/vault/{mission}?flash=introuvable", status_code=303)
    full = f"⟦{token}⟧"
    val = v.value_for(full)
    etype = token.split("_")[0] if "_" in token else "NOM"
    flash = "absent"
    if val is not None:
        try:
            if v.forget(val):
                v.save(_vaults_dir() / f"{mission}.vault.json")
                _audit_event(mission=mission, event="vault_forget", token=token,
                             entity_type=etype, counts={etype: 1})
                flash = "oublie"
        except Exception:
            flash = "erreur"
    return RedirectResponse(url=f"/vault/{mission}?flash={flash}", status_code=303)


@app.get("/vault/{mission}/forget-subject-count", response_class=HTMLResponse)
def vault_forget_subject_count(request: Request, mission: str, q: str = ""):
    """Preview how many tokens a forget_subject(q) would remove (substring match)."""
    v = _load_vault_or_none(mission)
    n = 0
    if v is not None and q.strip():
        needle = q.strip().lower()
        n = sum(1 for val in v.to_token if needle in val.lower())
    return templates.TemplateResponse(
        request, "vault_forget_subject.html", {"mission": mission, "q": q, "count": n})


@app.post("/vault/{mission}/forget-subject")
async def vault_forget_subject(request: Request, mission: str,
                               q: str = Form(...), confirm: str = Form("")):
    """Erase ALL tokens whose value contains q (RGPD). Requires confirm=OUBLIER."""
    from fastapi.responses import RedirectResponse
    if confirm != "OUBLIER":
        return RedirectResponse(url=f"/vault/{mission}?flash=annule", status_code=303)
    v = _load_vault_or_none(mission)
    if v is None:
        return RedirectResponse(url=f"/vault/{mission}?flash=introuvable", status_code=303)
    flash = "absent"
    try:
        n = v.forget_subject(q.strip())
        if n:
            v.save(_vaults_dir() / f"{mission}.vault.json")
            _audit_event(mission=mission, event="vault_forget_subject",
                         counts={"removed": n})
            flash = f"oublie-{n}"
    except Exception:
        flash = "erreur"
    return RedirectResponse(url=f"/vault/{mission}?flash={flash}", status_code=303)
