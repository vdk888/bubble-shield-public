"""#568 — local Gemma NOM/MOT judge for gazetteer de-pollution.

Validated this session: 96.7% accuracy, zero false-positive leakage, the one
miss ('Petit') fails SAFE (stays masked). Runs in the gemma venv (mlx_lm).

Fail-toward-masking is the safety core of this module: `_parse_verdict`
returns "NOM" (keep masked) for ANYTHING not clearly MOT, and `classify`
returns verdict "NOM" on any inference exception for a token. An unclear
judge must keep masking — never fail open.
"""
from __future__ import annotations

MODEL_ID = "mlx-community/gemma-3n-E4B-it-lm-4bit"

_PROMPT = (
    "Réponds par un seul mot: NOM ou MOT.\n"
    "Le token suivant est-il un nom de famille français (NOM) "
    "ou un mot commun / une étiquette de formulaire (MOT) ?\n"
    "Token: '{tok}'\nRéponse:"
)


def _parse_verdict(raw: str) -> str:
    """Map model output to NOM|MOT. Fail-toward-masking: unclear → NOM (keep)."""
    t = (raw or "").strip().upper()
    # Only an UNAMBIGUOUS MOT signal (MOT present AND NOM absent) unmasks.
    # Anything else — both present, NOM only, empty, garbage — keeps masked.
    if "MOT" in t and "NOM" not in t:
        return "MOT"
    return "NOM"


# Task 2 (#589) / wedge-fix bundle → hardened to candidate "C2" (bare-surname,
# validated live 2026-07-11). C1 replaced B2 ("is this a NAME?" → un-masked real
# addresses, 102/110 leaks) by reframing to "is this a real identifying VALUE
# (person name / real postal address / company raison sociale) → PII/keep, or a
# generic label / job title / form field / boilerplate → GENERIQUE/un-mask? On
# doubt → PII." C1's exemplars were all FULL names, so BARE single-token real
# surnames that are also common words ("Petit", "Smith") or rarer patronyms
# leaned GENERIQUE and got un-masked — a real name leak. C2 adds an explicit
# bare-token rule (a lone word that could be a NOM DE FAMILLE → PII) plus 4
# bare-token few-shot exemplars (Petit→PII, Duchemin→PII, FISCAL→GENERIQUE,
# Cadre supérieur→GENERIQUE). Parse/logic unchanged (max_tokens=8, single-shot).
# Live result: the 4 leaked surnames stay masked, 0 bare-surname leaks across the
# 202-NOM/110-ADRESSE/60-POSTE sweep, no regression on boilerplate/address clean.
# The VERBATIM C2 text (French accents + guillemets «» + all 12 exemplars exact).
_JUDGE_PROMPT = (
    "Tu filtres les fausses alertes d'un outil d'anonymisation.\n"
    "On te donne une courte chaîne extraite d'un document. Réponds PII si c'est une VRAIE donnée identifiante — le nom/prénom d'une personne réelle, une adresse postale réelle, ou la raison sociale d'une entreprise (SARL, SAS, SELARL, SA, SCI...). Réponds GENERIQUE si c'est un mot commun, un intitulé de poste (consultant, cadre supérieur...), une étiquette de formulaire (déclarant 1, nom de naissance...), ou une phrase administrative générique.\n"
    "ATTENTION aux mots seuls: si un mot isolé pourrait être le NOM DE FAMILLE d'une personne (même si c'est aussi un mot courant, ex: Petit, Smith), réponds PII. Ne réponds GENERIQUE pour un mot seul que si c'est clairement un terme administratif/fiscal ou un nom commun qui n'est pas un patronyme.\n"
    "En cas de doute, réponds PII.\n"
    "Réponds par UN SEUL mot: PII ou GENERIQUE.\n"
    "\n"
    "Chaîne: «directeur général»\n"
    "Réponse: GENERIQUE\n"
    "Chaîne: «Jean-Marc DUPONTEL»\n"
    "Réponse: PII\n"
    "Chaîne: «12 rue des Acacias, 69003 Lyon»\n"
    "Réponse: PII\n"
    "Chaîne: «adresse du souscripteur»\n"
    "Réponse: GENERIQUE\n"
    "Chaîne: «Madame Sophie LEGRAND»\n"
    "Réponse: PII\n"
    "Chaîne: «SARL Lumière Patrimoine»\n"
    "Réponse: PII\n"
    "Chaîne: «cadre de la mission»\n"
    "Réponse: GENERIQUE\n"
    "Chaîne: «8 boulevard Haussmann 75009 Paris»\n"
    "Réponse: PII\n"
    "Chaîne: «Petit»\n"
    "Réponse: PII\n"
    "Chaîne: «Duchemin»\n"
    "Réponse: PII\n"
    "Chaîne: «FISCAL»\n"
    "Réponse: GENERIQUE\n"
    "Chaîne: «Cadre supérieur»\n"
    "Réponse: GENERIQUE\n"
    "\n"
    "Chaîne: «{tok}»\n"
    "Réponse:"
)


# #589-B — Gemma PII-span extraction (2nd pass for degraded tax forms).
_EXTRACT_PROMPT = (
    "Liste toute donnée personnelle identifiante dans ce texte de formulaire fiscal "
    "français. Une ligne par donnée, format « TYPE: valeur exacte ». Types autorisés: "
    "NOM, PRENOM, SIRET, DATE_NAISSANCE, LIEU_NAISSANCE, ADRESSE, TELEPHONE, RAISON_SOCIALE. "
    "N'invente rien ; recopie la valeur telle qu'elle apparaît. Si aucune donnée: réponds « (aucune) ».\n\n"
    "TEXTE:\n{text}\n\nDONNÉES:"
)
_ALLOWED_TYPES = {"NOM", "PRENOM", "SIRET", "DATE_NAISSANCE", "LIEU_NAISSANCE",
                  "ADRESSE", "TELEPHONE", "RAISON_SOCIALE"}


_AUCUNE_STRIP_CHARS = " \t\r\n\"'«»«»"


def _is_aucune_sentinel(v: str) -> bool:
    """True when `v` is Gemma's "(aucune)" sentinel, possibly echoed back wrapped
    in guillemets/straight quotes and/or extra whitespace, in any case. Robust
    check per Task-2 review: a quoted echo (e.g. « (aucune) ») must be dropped,
    not turned into a spurious span."""
    v = v.strip(_AUCUNE_STRIP_CHARS).strip().lower()
    return v in ("aucune", "(aucune)")


def _parse_extract(raw):
    """Parse Gemma's 'TYPE: value' lines into typed spans. Unknown/empty types dropped.
    Fail-toward-nothing here (an empty parse is caught downstream as suspicious → fail-closed)."""
    spans = []
    for line in (raw or "").splitlines():
        if ":" not in line:
            continue
        t, _, v = line.partition(":")
        t = t.strip().upper(); v = v.strip()
        if t in _ALLOWED_TYPES and v and not _is_aucune_sentinel(v):
            spans.append({"type": t, "text": v})
    return spans


class GemmaClassifier:
    def __init__(self, model_id: str = MODEL_ID):
        self.model_id = model_id
        self.warm = False
        self._model = None
        self._tok = None

    def warm_up(self) -> None:
        from mlx_lm import load
        self._model, self._tok = load(self.model_id)
        # Loading weights is NOT enough: the FIRST generate() of each distinct
        # prompt shape pays the full MLX graph-compile / first-token cost (several
        # seconds), which made the first real request per sweep TIME OUT even
        # though /health reported warm. Prime BOTH inference paths with a tiny
        # dummy generate so the graphs are compiled here, at warm time — not on
        # the first client document. classify and extract_pii use DIFFERENT
        # prompts, so we must prime each. Best-effort: a priming failure must not
        # block the daemon (it still serves, just cold on the first real call).
        try:
            self.classify(["Dupont"])
        except Exception:
            pass
        try:
            self.extract_pii("Nom: DUPONT")
        except Exception:
            pass
        self.warm = True

    def classify(self, tokens):
        from mlx_lm import generate
        out = []
        for tok in tokens:
            try:
                resp = generate(self._model, self._tok,
                                prompt=_PROMPT.format(tok=tok), max_tokens=4, verbose=False)
                verdict = _parse_verdict(resp)
            except Exception:
                verdict = "NOM"  # fail-safe: keep masked
            out.append({"token": tok, "verdict": verdict})
        return out

    def extract_pii(self, text):
        from mlx_lm import generate
        resp = generate(self._model, self._tok,
                        prompt=_EXTRACT_PROMPT.format(text=text[:6000]),
                        max_tokens=512, verbose=False)
        return _parse_extract(resp)

    def classify_via_extract(self, tokens, should_abort=None):
        """Task 2 (#589) — value-focused de-pollution judge (candidate C1).

        REPLACES the Task-1 extract_pii-based body (which hallucinated template
        PII on short fragments and was ~15× too slow). For each ENTRY, run one
        fast single-shot judge (`_JUDGE_PROMPT`, max_tokens=8) and parse:

          - "GENERIQUE" present AND "PII" absent (case-insensitive) → "MOT"
            (un-mask): a clean, unambiguous common-word / label / job-title.
          - anything else — "PII", both present, empty, garbage → "NOM"
            (keep masked). In doubt, keep masking.
          - generate() RAISES → "NOM" (keep masked): fail-toward-masking. An
            inference error must NEVER un-mask (that would leak real client PII).

        The MOT case is reached ONLY on a clean GENERIQUE from a SUCCESSFUL
        generate; an errored call is caught first and forced to "NOM".

        `should_abort` (OPTIONAL, wedge fix): a zero-arg callable checked at the
        TOP of each token iteration. When it returns True the loop stops early
        and returns the results computed SO FAR — the remaining tokens are
        simply absent (→ depollute keeps them masked). The daemon's worker wires
        this to the job's `abandoned` flag so an abandoned multi-token batch
        stops grinding the single MLX worker early. When None (direct calls /
        tests), behavior is UNCHANGED — every token is processed.

        (extract_pii / _EXTRACT_PROMPT are unchanged — still used for the
        #589-B second pass elsewhere; only this method's judging path changes.)
        """
        from mlx_lm import generate
        out = []
        for tok in tokens:
            # Wedge fix: bail out early if the caller (worker) has abandoned this
            # job. Remaining tokens stay absent → treated as stay-masked. Checked
            # BEFORE inference so an abandoned job stops grinding immediately.
            if should_abort is not None and should_abort():
                break
            try:
                resp = generate(self._model, self._tok,
                                prompt=_JUDGE_PROMPT.format(tok=tok),
                                max_tokens=8, verbose=False)
                up = (resp or "").upper()
                verdict = "MOT" if ("GENERIQUE" in up and "PII" not in up) else "NOM"
            except Exception:
                verdict = "NOM"  # fail-safe: keep masked
            out.append({"token": tok, "verdict": verdict})
        return out
