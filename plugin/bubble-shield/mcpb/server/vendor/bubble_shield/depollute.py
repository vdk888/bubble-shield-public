"""#568 — gazetteer de-pollution. A+D triage + orchestration.

A (frequency): wordfreq zipf. D (structural): islower.
Proven this session: A+D cannot auto-decide the CAPITALIZED ambiguous set
(French surnames ARE common words) — those go to Gemma (Task 4). There is
also no safe frequency floor for a "keep" lane: real French surnames span
the whole frequency range (Lenoir zipf ~3.1 ... Petit zipf ~5.67), overlapping
common words — so a low-frequency-keep lane is unsound and has been removed.
A+D only decides the clear lowercase-junk lane; everything else is uncertain
and goes to Gemma.
"""
from __future__ import annotations

ZIPF_JUNK_MIN: float = 4.0   # >= this AND lowercase → high-confidence common word


def _max_zipf(value: str) -> float:
    try:
        from wordfreq import zipf_frequency
    except Exception:
        return 0.0  # fail-toward-masking: no wordfreq → treat as rare → uncertain
    v = value.strip().lower()
    return max(zipf_frequency(v, "fr"), zipf_frequency(v, "en"))


def triage(value: str) -> str:
    """Return 'junk' | 'uncertain'.

    junk      = lowercase + high-frequency → high-confidence false positive
    uncertain = everything else            → Gemma must adjudicate (A+D can't)
    """
    v = value.strip()
    if not v:
        return "uncertain"
    z = _max_zipf(v)
    if v.islower() and z >= ZIPF_JUNK_MIN:
        return "junk"
    return "uncertain"


def depollute_gazetteer(classify_fn, *, gaz_path=None, queue_path=None) -> dict:
    """Run one de-pollution pass over the gazetteer.

    2-bucket triage (see module docstring / #568 amendment — the KEEP lane was
    removed as unsound): every entry is either

      - 'junk'      → un-mask candidate directly (A+D already decided)
      - 'uncertain' → sent to `classify_fn(tokens) -> [{"token","verdict"}]`
                      (injected: a fake in tests, a Gemma daemon HTTP call in
                      prod — Task 6). Only a "MOT" verdict un-masks; anything
                      else (including a missing verdict) stays masked.

    There is NO keep bucket: everything not un-masked simply stays masked —
    the safe default. Un-masking = `known_pii_store.remove_pii` (soft removal,
    no permanent allowlist). Every un-mask is logged to the review queue via
    `review_queue.add_candidate` for human audit.

    Fail-toward-masking: if classify_fn raises, the uncertain entries for THIS
    pass stay masked (never un-masked on error) — only the junk lane (which
    never touches classify_fn) is un-masked in that case.

    Returns {"unmasked": [values], "kept": [values], "logged": int} where
    "kept" is every entry that stayed masked (uncertain-and-not-MOT, plus any
    that errored).
    """
    from bubble_shield import known_pii_store as kps
    from bubble_shield import review_queue as rq

    gaz = kps.load_gazetteer(path=gaz_path)
    junk: list[str] = []
    uncertain: list[str] = []
    for e in gaz.entries:
        t = triage(e.value)
        if t == "junk":
            junk.append(e.value)
        else:
            uncertain.append(e.value)

    # Gemma adjudicates only the uncertain set. Fail-toward-masking: any error
    # from classify_fn means NONE of the uncertain entries un-mask this pass.
    mot: list[str] = []
    if uncertain:
        try:
            verdicts = classify_fn(uncertain)
            mot_tokens = {
                r.get("token") for r in verdicts if r.get("verdict") == "MOT"
            }
            mot = [v for v in uncertain if v in mot_tokens]
        except Exception:
            mot = []  # fail-toward-masking

    unmasked = junk + mot
    unmasked_set = set(unmasked)
    kept = [v for v in (junk + uncertain) if v not in unmasked_set]

    logged = 0
    for v in unmasked:
        kps.remove_pii(v, path=gaz_path)
        entity_type = gaz.entity_type_of(v)
        try:
            result = rq.add_candidate(
                v, entity_type, "depollute", path=queue_path, gaz_path=gaz_path
            )
            if result is not None:
                logged += 1
        except Exception:
            pass  # logging must never break the un-mask itself

    return {"unmasked": unmasked, "kept": kept, "logged": logged}


def daemon_classify(tokens, *, port: int = 8724, timeout: int = 30):
    """Prod classify_fn: POST tokens to the local Gemma daemon.

    Fail-toward-masking: ANY error (daemon down, timeout, non-200) → [] so the
    pipeline leaves the uncertain entries masked.
    """
    import json, urllib.request

    if not tokens:
        return []
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/classify",
            data=json.dumps({"tokens": list(tokens)}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return []
            return json.loads(r.read()).get("results", [])
    except Exception:
        return []
