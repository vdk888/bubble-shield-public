"""
surrogate.py — realistic surrogate mode (DontFeedTheAI idea), OPT-IN.

Instead of opaque ⟦NOM_0001⟧ tokens, replace each PII value with a PLAUSIBLE
FAKE of the same shape: "Jean Dupont" → "Sophie Laurent", a real
client IBAN → a structurally-valid fake IBAN, etc. The LLM then reasons on
natural-looking text (no bracket tokens to trip over or reformat), and the fake
is mapped back to the real value on the way out — fully reversible via the vault.

WHY IT'S OPT-IN, NOT DEFAULT
----------------------------
A realistic fake is a double-edged sword: it reads better for the model, but if
restoration ever failed you'd be looking at a plausible-but-wrong name and might
not notice (whereas a leftover ⟦NOM_0001⟧ is obviously a token). For a compliance
tool the safe default is opaque tokens that fail loudly. Surrogate mode is offered
for users who want maximum LLM fidelity and accept the trade-off. Enable with
BUBBLE_SHIELD_SURROGATE=1 (the webapp wires it through) or by passing a SurrogateVault.

Deterministic per (value, seed) so the same input always yields the same fake —
stable across a session and reproducible.
"""
from __future__ import annotations

import hashlib
import re

from bubble_shield.vault import Vault

# Small pools — enough variety to look real, deterministic selection by hash.
_PRENOMS = ("Sophie", "Lucas", "Camille", "Hugo", "Léa", "Nathan", "Chloé",
            "Adrien", "Manon", "Théo", "Julie", "Maxime", "Sarah", "Antoine",
            "Inès", "Paul", "Emma", "Louis", "Alice", "Gabriel")
_NOMS = ("Laurent", "Moreau", "Garnier", "Lefebvre", "Rousseau", "Fontaine",
         "Chevalier", "Renard", "Marchand", "Lemoine", "Perrin", "Girard",
         "Dumont", "Robin", "Faure", "Blanc", "Henry", "Roussel", "Vidal", "Aubert")
_VILLES = ("Tours", "Reims", "Angers", "Dijon", "Nancy", "Caen", "Metz",
           "Brest", "Orléans", "Rouen", "Pau", "Nîmes", "Annecy", "Valence")
_DOMAINS = ("exemple.fr", "courriel.fr", "mail-fictif.fr", "demo.example")


def _h(value: str, seed: int, salt: str = "") -> int:
    return int(hashlib.sha256(f"{seed}|{salt}|{value}".encode()).hexdigest(), 16)


def _pick(pool, value, seed, salt):
    return pool[_h(value, seed, salt) % len(pool)]


def surrogate_for(value: str, entity_type: str, seed: int = 0) -> str:
    """Return a realistic fake of the same shape as `value`. Deterministic per
    (value, seed)."""
    t = entity_type.upper()
    if t == "NOM":
        # Preserve roughly the word-count feel: 1 word → surname only, else
        # prénom + nom.
        n_words = len([w for w in re.split(r"[\s\-]+", value) if len(w) >= 2])
        nom = _pick(_NOMS, value, seed, "nom")
        if n_words <= 1:
            return nom
        return f"{_pick(_PRENOMS, value, seed, 'prenom')} {nom}"
    if t == "EMAIL":
        pre = _pick(_PRENOMS, value, seed, "epre").lower()
        sur = _pick(_NOMS, value, seed, "esur").lower()
        dom = _pick(_DOMAINS, value, seed, "edom")
        return f"{pre}.{sur}@{dom}"
    if t in ("LIEU_NAISSANCE",):
        return _pick(_VILLES, value, seed, "ville")
    if t == "ADRESSE":
        num = (_h(value, seed, "num") % 80) + 1
        return f"{num} rue des Acacias, 75000 {_pick(_VILLES, value, seed, 'adrv')}"
    if t == "TEL":
        d = _h(value, seed, "tel")
        return "06 " + " ".join(f"{(d >> (i*8)) % 100:02d}" for i in range(4))
    if t == "IBAN":
        d = _h(value, seed, "iban")
        body = "".join(str((d >> (i*3)) % 10) for i in range(23))
        return "FR76" + body
    if t in ("DATE_NAISSANCE", "DATE_EVENEMENT"):
        d = _h(value, seed, "date")
        day = d % 28 + 1
        month = (d >> 8) % 12 + 1
        year = 1950 + (d >> 16) % 55
        return f"{day:02d}/{month:02d}/{year}"
    if t in ("PIECE_IDENTITE", "NUM_FISCAL", "SECU", "NUM_CLIENT"):
        d = _h(value, seed, "id")
        return "".join(str((d >> (i*3)) % 10) for i in range(min(len(re.sub(r"\D", "", value)) or 8, 13)))
    # Fallback: a generic but distinct fake token-ish string (never the real one).
    return f"[{t.lower()}-{_h(value, seed, 'fb') % 10000:04d}]"


class SurrogateVault(Vault):
    """A Vault that mints realistic surrogates instead of opaque tokens. Fully
    reversible: restore() maps surrogate → real value. Drop-in for the engine."""

    def __init__(self, *args, seed: int = 0, **kwargs):
        super().__init__(*args, **kwargs)
        self._seed = seed

    def token_for(self, value: str, entity_type: str) -> str:
        if value in self.to_token:
            return self.to_token[value]
        # Generate a surrogate, ensuring uniqueness (re-salt on collision).
        s = surrogate_for(value, entity_type, self._seed)
        bump = 0
        while s in self.to_value and self.to_value[s] != value:
            bump += 1
            s = surrogate_for(value + ("·" * bump), entity_type, self._seed)
        self.to_token[value] = s
        self.to_value[s] = value
        return s

    def restore(self, text: str) -> str:
        # Replace each surrogate with its real value. Longest-first so a
        # surrogate that's a substring of another doesn't partially match.
        for sur in sorted(self.to_value, key=len, reverse=True):
            if sur in text:
                text = text.replace(sur, self.to_value[sur])
        return text
