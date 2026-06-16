"""
dossier.py — anonymise a WHOLE dossier (many files of one client) coherently.

A CGP subscription dossier is 8+ documents about the SAME client. Processing them
one-by-one with a fresh vault each time gives INCONSISTENT results: the same
client can get a different token (or, in surrogate mode, a different fake) in each
file, and a client who's only mentioned in passing in one document (e.g. buried in
the entrée-en-relation boilerplate) is missed there.

Dossier mode fixes both by sharing state across all files:
  - ONE vault → the same client always gets the same token / surrogate everywhere
    (coherent across the whole dossier; the correspondence table is unified).
  - ONE self-improving profile → the client discovered in the rich KYC is then
    swept into every other document, including the ones where a single pass would
    miss him. This is the cross-file ACCURACY win.

Pure logic, no web dependency — the webapp and tests both call `anonymize_dossier`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional

from bubble_shield.engine import AnonymizationEngine, AnonymizationResult


@dataclass
class DossierFile:
    """One document's anonymisation result within a dossier."""
    name: str
    result: AnonymizationResult
    error: Optional[str] = None


@dataclass
class DossierResult:
    """The whole dossier: per-file results sharing one vault + profile."""
    files: List[DossierFile] = field(default_factory=list)
    vault: object = None                     # the shared Vault / SurrogateVault
    profile: object = None                   # the shared ClientProfile

    @property
    def total_entities(self) -> int:
        return sum(f.result.entity_count for f in self.files if f.result)

    @property
    def n_ok(self) -> int:
        return sum(1 for f in self.files if f.result and not f.error)

    @property
    def all_safe(self) -> bool:
        return all(f.result.safe_to_send for f in self.files if f.result)


def anonymize_dossier(
    docs: List[tuple],                       # [(name, text), ...]
    *,
    engine_factory: Callable[[], tuple],     # () -> (engine_with_shared_vault, allowlist|None)
    two_pass: bool = True,
) -> DossierResult:
    """Anonymise every (name, text) in `docs` through ONE shared vault and ONE
    shared self-improving profile.

    `engine_factory` returns a fresh engine that ALREADY shares the dossier vault
    (so tokens/surrogates are consistent across files) plus its allowlist. We run
    the files in TWO ROUNDS:
      Round 1 — build the client profile from every file (discover the client
                wherever he's richly described, e.g. the KYC).
      Round 2 — re-anonymise every file WITH the full profile, so a client found
                in file A is also redacted in file B where a single pass missed him.
    The shared vault guarantees the same value → the same token in both rounds and
    across all files.
    """
    from bubble_shield.profile_sweep import ClientProfile, two_pass_detect

    out = DossierResult()
    profile = ClientProfile()
    shared_engine, allowlist = engine_factory()
    out.vault = shared_engine.vault

    if not two_pass or allowlist is None:
        # Simple path: one shared vault, single pass per file.
        for name, text in docs:
            res = shared_engine.anonymize(text or "")
            out.files.append(DossierFile(name=name, result=res))
        out.profile = profile
        return out

    # Round 1 — learn the client profile from EVERY file (shared profile).
    for _name, text in docs:
        res = shared_engine.anonymize(text or "")
        profile.learn(res.entities, allowlist=allowlist)

    # Round 2 — re-anonymise every file with the full profile injected, through
    # the SAME shared vault (so tokens stay consistent across the dossier).
    sweep = lambda t: profile.sweep(t)
    base_detectors = list(shared_engine.extra_detectors)
    shared_engine.extra_detectors = base_detectors + [sweep]
    try:
        for name, text in docs:
            res = shared_engine.anonymize(text or "")
            out.files.append(DossierFile(name=name, result=res))
    finally:
        shared_engine.extra_detectors = base_detectors

    out.profile = profile
    return out
