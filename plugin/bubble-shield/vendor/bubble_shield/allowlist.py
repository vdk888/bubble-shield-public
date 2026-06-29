"""
allowlist.py — "this is NOT the client" filter (the big precision lever).

THE INSIGHT (validated on real client DERs)
-------------------------------------------
A CGP subscription dossier is ~90% the ADVISORY FIRM's own boilerplate, not the
client. A real client DER produced ~35 raw detections of which nearly ALL were
false positives: the firm's own name/address, its advisors' names, its e-mail
domain, plus regulators (AMF, ACPR, CNIL), a mediator, the insurer and dozens of
fund houses (Corum, Nortia, Primonial…). Anonymising any of those is WRONG — it
isn't the secret, and it destroys the document's meaning.

So the hard problem isn't "find names", it's "distinguish CLIENT data from
FIRM / REGULATOR / COUNTERPARTY boilerplate". This module is the deterministic,
cheap, high-precision half of that: a configurable allowlist of entities that are
known NOT to be the client, applied as a post-detection filter. Whatever the
detectors flag that matches the allowlist is dropped (kept in clear).

It is intentionally DATA, not code: the firm's own identity + the public third
parties are configured PER DEPLOYMENT in a local, gitignored config file
(`deployment_allowlist.json` — see `deployment_allowlist.example.json` for the
schema). The firm's own identity is client business data, so it never lives in
source / version control. The LLM/context layer then handles the residual
ambiguity ("is this remaining name the client or a relative?"). Allowlist =
precision floor; context layer = the judgment.

Conservative by default: matching is case-insensitive substring/lemma on the
detected span. We only ever REMOVE a detection (fail toward anonymising): if a
client genuinely shared a surname with an advisor, the allowlist could suppress a
real client hit — so the allowlist holds FULL identifiers (full names, full
addresses, the e-mail domain), never bare first names, to keep that risk near zero.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Sequence

from bubble_shield.recognizers import Match


def _norm(s: str) -> str:
    """Lower, collapse whitespace/newlines, strip accents lightly for matching."""
    s = s.lower()
    s = s.replace("\n", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _digits(s: str) -> str:
    """Keep only digits — for format-agnostic phone comparison."""
    return re.sub(r"\D", "", s or "")


# A "short single token" allowlist phrase (e.g. "axa", "eres", "corum") is one
# word and ≤ this many chars. Such tokens are dangerous as substrings — they hide
# inside real surnames ("Eres Martin", "Corumbel Dupont"). They must match a WHOLE
# token, never an arbitrary substring. Multi-word phrases (firm names, addresses,
# "autorité des marchés financiers") keep the safe substring/phrase matching.
_SHORT_TOKEN_MAX = 6
# Split a value into alphabetic tokens on whitespace AND punctuation (so
# "Marie-Axandre" → ["marie", "axandre"], not one token containing "axa").
_TOKEN_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def _tokens(s: str) -> List[str]:
    return _TOKEN_RE.findall(s)


def _short_token_allowlists(phrase: str, value: str) -> bool:
    """Word-boundary match for a short single-token org phrase.

    The org token allowlists `value` ONLY when it appears as a whole token AND
    `value` carries no OTHER capitalised, name-like token. This keeps
    "ERES"/"souscription ERES" allowlisted (the org is the only name there) but
    drops "Eres Martin" (Martin is a real surname) — the substring leak #348.
    """
    raw_tokens = _TOKEN_RE.findall(value)
    norm_tokens = [t.lower() for t in raw_tokens]
    if phrase not in norm_tokens:
        return False
    # Reject if any token (other than the org token) looks like a proper name —
    # i.e. starts with an uppercase letter in the ORIGINAL value. Generic
    # lowercase context words ("souscription") are fine.
    for raw in raw_tokens:
        if raw.lower() == phrase:
            continue
        if raw[:1].isupper():
            return False
    return True


@dataclass
class Allowlist:
    """Entities known NOT to be the client. A detection is dropped if its
    normalised value contains (or is contained by) any allowlist phrase, or if
    its value contains an allowlisted e-mail domain."""

    phrases: Sequence[str] = field(default_factory=tuple)   # full names, addresses, org names
    email_domains: Sequence[str] = field(default_factory=tuple)  # e.g. "acme-patrimoine.fr"
    phones: Sequence[str] = field(default_factory=tuple)    # firm phone numbers (any format)

    def __post_init__(self):
        self._phrases = [_norm(p) for p in self.phrases if p.strip()]
        self._domains = [d.lower().lstrip("@") for d in self.email_domains if d.strip()]
        # Digit-only normalised firm phones — so "01 23 45 67 89", "0123456789"
        # and "+33 1 23 45 67 89" all match (the unspaced form leaked before).
        self._phones = {_digits(p) for p in self.phones if _digits(p)}

    def is_allowlisted(self, value: str) -> bool:
        v = _norm(value)
        if not v:
            return False
        # phone match on digits-only (format-agnostic). Compare last 9 digits to
        # ignore +33 / 0 trunk-prefix differences.
        dv = _digits(value)
        if len(dv) >= 9 and any(
            dv[-9:] == p[-9:] for p in self._phones if len(p) >= 9
        ):
            return True
        # e-mail domain match (advisor mailboxes share the firm domain)
        if "@" in value:
            dom = value.split("@")[-1].strip().lower()
            if any(dom == d or dom.endswith("." + d) for d in self._domains):
                return True
        for p in self._phrases:
            if not p:
                continue
            # #348 — a SHORT SINGLE-TOKEN org phrase (axa/eres/corum) must match a
            # WHOLE token, not an arbitrary substring: "eres" must not allow-list
            # "Eres Martin" (Martin is a real surname). Multi-word phrases stay on
            # substring matching — they're safe (full firm names, addresses,
            # "autorité des marchés financiers") and intended.
            if " " not in p and len(p) <= _SHORT_TOKEN_MAX:
                if _short_token_allowlists(p, value):
                    return True
                continue
            # substring either way: detected "Jean CONSEILLER" vs phrase
            # "jean conseiller"; or detected "12, rue de l'Exemple, 75000 PARIS"
            # vs phrase "rue de l'exemple". (Names no longer span newlines — see the
            # recognizers.py NOM fix — so the old glued-span false-drop is gone.)
            if p in v or v in p:
                return True
        return False

    def filter(self, matches: Iterable[Match]) -> List[Match]:
        """Return only matches that are NOT allowlisted (i.e. plausibly client)."""
        return [m for m in matches if not self.is_allowlisted(m.value)]

    def make_filter(self):
        """Return a post-filter callable for engine integration."""
        return self.filter


# ── Public third parties (regulators, mediators, fund houses) ──────────────
# These are NOT client-specific — they're the same boilerplate in every French
# CGP dossier, so they live in source. The firm's OWN identity (its name,
# address, advisors, e-mail domain, phone) is client business data and is loaded
# separately from a gitignored deployment config (see load_deployment_allowlist).
PUBLIC_THIRD_PARTIES = Allowlist(
    phrases=(
        # Regulators / mediators / public bodies + their addresses
        "amf", "autorité des marchés financiers", "acpr", "cnil",
        "place de la bourse", "place de budapest", "place de fontenoy",
        "cmap", "avenue franklin d", "orias",
        # Public professional associations (boilerplate in every CGP dossier — the
        # advisor's mandatory affiliation, not a client-specific identity)
        "aspim", "anacofi", "cncgp", "la compagnie des cgp",
        # Major fund houses / platforms (boilerplate lists in the DER/LM)
        "corum", "nortia", "primonial", "generali", "swiss life", "axa",
        "edmond de rothschild", "la française", "vatel capital", "m capital",
        "inter invest", "june reim", "tilvest", "alpheys", "cardif", "bnp paribas",
        "abeille", "april", "entoria", "eres", "one life", "lombard", "utwin",
        "mma", "amundi",
    ),
)


def _deployment_config_locations() -> tuple[str, ...]:
    """Where to look for the firm-identity config. If the explicit env override
    is set, ONLY that path is consulted (so a test/deployment can fully control
    which config is used, including asserting the no-config fallback)."""
    override = os.environ.get("BUBBLE_SHIELD_DEPLOYMENT_ALLOWLIST")
    if override:
        return (override,)
    return (
        str(Path(__file__).resolve().parent / "deployment_allowlist.json"),
        os.path.expanduser("~/.config/bubble_shield/deployment_allowlist.json"),
    )


def _merge(*allowlists: "Allowlist") -> "Allowlist":
    """Combine several Allowlists into one (concat phrases/domains/phones)."""
    phrases: list[str] = []
    domains: list[str] = []
    phones: list[str] = []
    for al in allowlists:
        phrases.extend(al.phrases)
        domains.extend(al.email_domains)
        phones.extend(al.phones)
    return Allowlist(phrases=tuple(phrases), email_domains=tuple(domains), phones=tuple(phones))


def load_deployment_allowlist() -> "Allowlist":
    """Load the per-deployment allowlist = PUBLIC_THIRD_PARTIES + the firm's own
    identity from a local, gitignored `deployment_allowlist.json`.

    The firm config is the advisory firm's business data (its name, address,
    advisors, e-mail domain, switchboard), so it is NEVER committed. If no config
    is found, we fall back to the public third parties alone — the engine still
    works, it just won't allow-list this particular firm's boilerplate.

    Schema (see deployment_allowlist.example.json):
        { "phrases": [...], "email_domains": [...], "phones": [...] }
    """
    for loc in _deployment_config_locations():
        if not loc:
            continue
        p = Path(loc)
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            # A broken deployment config must not crash the engine; fall through
            # to the public third parties (fail toward MORE anonymisation).
            break
        firm = Allowlist(
            phrases=tuple(data.get("phrases", ())),
            email_domains=tuple(data.get("email_domains", ())),
            phones=tuple(data.get("phones", ())),
        )
        return _merge(PUBLIC_THIRD_PARTIES, firm)
    return PUBLIC_THIRD_PARTIES


# ── Firm-config writers (add/remove keep entries) ─────────────────────────
# The keep-list ("this is the firm's OWN identifier, never a client's") is the
# ONLY place we persist a literal value, and only via the guarded MCP path
# (pii_guard refuses real client PII). Writes go to the writable firm config —
# NEVER the vendored example — using atomic temp-file + os.replace so a
# concurrent reader never sees a half-written file.

_KEY_MAP = {"phrase": "phrases", "email_domain": "email_domains", "phone": "phones"}


def _firm_config_path() -> Path:
    """The writable firm config path (never the vendored example)."""
    override = os.environ.get("BUBBLE_SHIELD_DEPLOYMENT_ALLOWLIST")
    if override:
        return Path(override)
    return Path(os.path.expanduser("~/.config/bubble_shield/deployment_allowlist.json"))


def _read_firm_config(path: Path) -> dict:
    if not path.is_file():
        return {"phrases": [], "email_domains": [], "phones": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    for k in ("phrases", "email_domains", "phones"):
        data.setdefault(k, [])
    return data


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def add_allowlist_entry(kind: str, value: str, path: "Path | None" = None) -> None:
    """Add an entry to the firm deployment allowlist. kind ∈ {phrase, email_domain, phone}."""
    key = _KEY_MAP.get(kind)
    if key is None:
        raise ValueError(f"unknown allowlist kind: {kind!r}")
    p = path or _firm_config_path()
    data = _read_firm_config(p)
    if value not in data[key]:
        data[key].append(value)
    _atomic_write(p, data)


def remove_allowlist_entry(kind: str, value: str, path: "Path | None" = None) -> bool:
    """Remove an entry. Returns True if found+removed, False if not found."""
    key = _KEY_MAP.get(kind)
    if key is None:
        raise ValueError(f"unknown allowlist kind: {kind!r}")
    p = path or _firm_config_path()
    data = _read_firm_config(p)
    if value not in data[key]:
        return False
    data[key] = [v for v in data[key] if v != value]
    _atomic_write(p, data)
    return True


def is_allowlisted(value: str) -> bool:
    """Module-level convenience: is `value` allow-listed by the PUBLIC third
    parties (regulators / fund houses) shipped in source? Does NOT include the
    per-deployment firm config (use load_deployment_allowlist().is_allowlisted
    for that). Handy for tests and quick membership checks."""
    return PUBLIC_THIRD_PARTIES.is_allowlisted(value)


# The active allowlist for this deployment. Importers use this; the firm-specific
# part comes from the local config, never from source.
DEPLOYMENT_ALLOWLIST = load_deployment_allowlist()
