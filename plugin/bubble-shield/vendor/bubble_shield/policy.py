"""policy.py — per-entity-type anonymisation policy (cloak vs keep).

The advisor's real problem isn't "anonymise everything" — it's "anonymise what
*identifies* the person, but KEEP what I actually need Claude to reason about."

Concrete example from a real CGP review: euro amounts on the accounts must be
*kept* in clear, because the whole point is to ask Claude "is this allocation
coherent with the client's risk profile?" — which needs the numbers. Meanwhile a
job title ("directeur marketing chez TotalEnergies") must be *cloaked*, because
it identifies the person as surely as a name.

So each entity type carries a policy: CLOAK (replace with a ⟦TOKEN⟧) or KEEP
(leave in clear). This module owns that policy — its defaults, its persistence,
and the engine `match_filter` that enforces it. It reads/writes a small JSON file
so a non-technical user can change the policy from the webapp and have it stick.

The policy holds NO PII — only entity-type names and a boolean. Safe to persist.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping

# Every entity type the engine can emit, with a human FR label and whether it
# identifies a person. Defaults follow the rule "cloak what identifies, keep what
# you need to reason about". MONTANT (amounts) defaults to KEEP for the CGP
# allocation/risk-coherence use-case; everything identifying defaults to CLOAK.
#
# `identifying` is advisory metadata for the UI (a warning when someone flips an
# identifying type to KEEP) — it does not by itself change behaviour.
ENTITY_CATALOG: Dict[str, Dict[str, Any]] = {
    "NOM":            {"label": "Nom / prénom",            "identifying": True,  "default_cloak": True},
    "ADRESSE":        {"label": "Adresse postale",         "identifying": True,  "default_cloak": True},
    "EMAIL":          {"label": "E-mail",                  "identifying": True,  "default_cloak": True},
    "TEL":            {"label": "Téléphone",               "identifying": True,  "default_cloak": True},
    "DATE_NAISSANCE": {"label": "Date de naissance",       "identifying": True,  "default_cloak": True},
    "LIEU_NAISSANCE": {"label": "Lieu de naissance",       "identifying": True,  "default_cloak": True},
    "NUM_CLIENT":     {"label": "N° client",               "identifying": True,  "default_cloak": True},
    "NUM_FISCAL":     {"label": "N° fiscal",               "identifying": True,  "default_cloak": True},
    "SECU":           {"label": "N° sécurité sociale",     "identifying": True,  "default_cloak": True},
    "IBAN":           {"label": "IBAN / compte bancaire",  "identifying": True,  "default_cloak": True},
    "PIECE_IDENTITE": {"label": "Pièce d'identité",        "identifying": True,  "default_cloak": True},
    "SIREN":          {"label": "SIREN (société)",         "identifying": True,  "default_cloak": True},
    "SIRET":          {"label": "SIRET (établissement)",   "identifying": True,  "default_cloak": True},
    "POSTE":          {"label": "Poste / fonction en entreprise", "identifying": True, "default_cloak": True},
    # Kept-by-default: useful to reason about, not directly identifying on its own.
    "MONTANT":        {"label": "Montant en euros",        "identifying": False, "default_cloak": False},
    "ISIN":           {"label": "ISIN (titre financier)",  "identifying": False, "default_cloak": False},
    "DATE_EVENEMENT": {"label": "Date (événement)",        "identifying": False, "default_cloak": True},
    # Phase 2 additions — emitted by OpenAI Privacy Filter soft layer only.
    # The regex/checksum core never emits these (vault/token format is type-agnostic).
    "URL":            {"label": "URL / lien web",          "identifying": True,  "default_cloak": True},
    "SECRET":         {"label": "Secret / credential",     "identifying": True,  "default_cloak": True},
}

DEFAULT_POLICY_PATH = os.environ.get(
    "BUBBLE_SHIELD_POLICY",
    str(Path(os.environ.get("BUBBLE_SHIELD_HOME", Path.home() / ".bubble_shield")) / "policy.json"),
)


def default_policy() -> Dict[str, bool]:
    """The out-of-the-box cloak/keep map: {entity_type: cloak?}."""
    return {etype: meta["default_cloak"] for etype, meta in ENTITY_CATALOG.items()}


def load_policy(path: str | os.PathLike[str] | None = None) -> Dict[str, bool]:
    """Load the cloak/keep policy, merged over defaults.

    Unknown keys in the file are ignored; missing keys fall back to the default.
    A missing or unreadable file → pure defaults (so the tool always works, and
    a corrupted policy can never accidentally turn cloaking OFF for a type)."""
    policy = default_policy()
    p = Path(path or DEFAULT_POLICY_PATH)
    if not p.exists():
        return policy
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return policy  # fail to defaults — never silently disable cloaking
    if isinstance(raw, Mapping):
        for etype, cloak in raw.items():
            if etype in ENTITY_CATALOG:
                policy[etype] = bool(cloak)
    return policy


def save_policy(policy: Mapping[str, bool], path: str | os.PathLike[str] | None = None) -> None:
    """Persist the cloak/keep policy (entity-type → bool). Only known types are
    written, so the file stays clean. Creates the parent dir if needed."""
    clean = {etype: bool(policy.get(etype, ENTITY_CATALOG[etype]["default_cloak"]))
             for etype in ENTITY_CATALOG}
    p = Path(path or DEFAULT_POLICY_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")


def make_match_filter(policy: Mapping[str, bool]) -> Callable[[List[Any]], List[Any]]:
    """Build an engine `match_filter` that DROPS matches whose type is set to KEEP.

    The engine only substitutes the matches this returns, so dropping a match
    leaves that value in clear — exactly "keep this entity type". An unknown type
    is cloaked (fail-closed: a type we don't recognise is treated as sensitive)."""
    def _filter(matches: List[Any]) -> List[Any]:
        return [m for m in matches if policy.get(getattr(m, "entity_type", ""), True)]
    return _filter


def policy_view(policy: Mapping[str, bool]) -> List[Dict[str, Any]]:
    """Render the policy as an ordered list of rows for the config table UI."""
    rows = []
    for etype, meta in ENTITY_CATALOG.items():
        rows.append({
            "type": etype,
            "label": meta["label"],
            "identifying": meta["identifying"],
            "cloak": bool(policy.get(etype, meta["default_cloak"])),
        })
    # Show identifying types first, then the kept-for-reasoning ones.
    rows.sort(key=lambda r: (not r["identifying"], r["type"]))
    return rows


def custom_entity_catalog(path=None) -> Dict[str, Dict[str, Any]]:
    """Return catalog rows for custom fields (regex + gliner_label kinds).

    Does NOT modify ENTITY_CATALOG — returns a separate dict for merging in
    views. Custom types are always treated as identifying and cloaked by default
    (a firm only adds a custom field because it identifies something)."""
    try:
        from bubble_shield.custom_recognizers import load_custom_fields_config
        cfg = load_custom_fields_config(path)
    except Exception:
        return {}
    result: Dict[str, Dict[str, Any]] = {}
    for entry in cfg.get("regex_fields", []):
        etype = entry.get("entity_type", "")
        if etype and etype not in ENTITY_CATALOG:
            label = entry.get("label", etype)
            result[etype] = {"label": label, "identifying": True,
                             "default_cloak": entry.get("cloak", True)}
    for entry in cfg.get("gliner_labels", []):
        etype = entry.get("entity_type", "")
        if etype and etype not in ENTITY_CATALOG and etype not in result:
            result[etype] = {"label": entry.get("label", etype),
                             "identifying": True, "default_cloak": True}
    return result


def extended_policy_view(policy: Mapping[str, bool], custom_path=None) -> List[Dict[str, Any]]:
    """Like policy_view() but includes custom field types.

    Custom rows carry an extra ``custom=True`` key so UIs can visually
    distinguish them from the built-in catalog rows.
    """
    custom = custom_entity_catalog(custom_path)
    rows = policy_view(policy)
    for etype, meta in custom.items():
        rows.append({
            "type": etype,
            "label": meta["label"],
            "identifying": meta["identifying"],
            "cloak": bool(policy.get(etype, meta["default_cloak"])),
            "custom": True,
        })
    rows.sort(key=lambda r: (not r["identifying"], r["type"]))
    return rows
