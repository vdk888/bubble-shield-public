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

# Wedge fix (2026-07-11) — daemon_classify chunk size. The Gemma daemon has ONE
# serial MLX worker (MLX is not thread-safe). A single /classify_extract request
# carrying the whole uncertain batch (~372 tokens) makes the worker grind for
# minutes in one job; the HTTP timeout fires; the worker keeps grinding the
# abandoned batch and every later request stacks behind it → daemon wedge.
# daemon_classify therefore splits the batch into ≤ DEPOLLUTE_CHUNK_SIZE-token
# requests so no single request can monopolise the worker, and a per-chunk
# failure only masks that chunk's tokens (fail-toward-masking, aggregated).
DEPOLLUTE_CHUNK_SIZE: int = 8

# Task 2 (#589) — entity-type allowlist. De-pollution may ONLY reach the judge
# (or the auto-unmask junk lane) for these entity types. Every other type
# (IBAN, SIRET, SECU, EMAIL, TEL, NUM_*, LIEU_NAISSANCE, DATE_NAISSANCE,
# RAISON_SOCIALE, URL, PIECE_IDENTITE, ...) is left MASKED, untouched — never
# passed to classify_fn, never auto-unmasked. The judge is name-focused, so a
# masked IBAN handed to it would be wrongly un-masked as "not a name"; this
# allowlist makes that structurally impossible. RAISON_SOCIALE is deliberately
# EXCLUDED (raison sociale = PII, keep masked).
DEPOLLUTE_ALLOWLIST: frozenset[str] = frozenset({"NOM", "POSTE", "ADRESSE"})


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
    from bubble_shield import depollute_state as ds

    gaz = kps.load_gazetteer(path=gaz_path)
    # JUDGE-ONCE (FIX 3): skip entries already de-pollution-judged on a prior pass.
    # A NOM verdict is stable, so re-judging a kept name every sweep is pure waste
    # (~6s Gemma call each). Load the judged-set once; skip any value in it. First
    # pass judges the backlog (slow, once); later passes judge only NEW entries.
    _already = ds.load_judged()
    junk: list[str] = []
    uncertain: list[str] = []
    for e in gaz.entries:
        # Task 2 allowlist gate (applied to the WHOLE entry loop, BEFORE triage):
        # only NOM/POSTE/ADRESSE entries may be de-polluted. Every other type is
        # skipped entirely — it stays masked, is never triaged, never handed to
        # classify_fn (name-focused judge), and never auto-unmasked by the junk
        # lane. This is the P0 structural guarantee: no IBAN/SIRET/SECU/EMAIL/
        # TEL/NUM_*/LIEU_NAISSANCE/DATE_NAISSANCE/RAISON_SOCIALE/URL/
        # PIECE_IDENTITE entry can EVER be un-masked here.
        if gaz.entity_type_of(e.value) not in DEPOLLUTE_ALLOWLIST:
            continue
        if ds.was_judged(e.value, _already):
            continue  # already judged on a prior pass — verdict is stable, skip
        t = triage(e.value)
        if t == "junk":
            junk.append(e.value)
        else:
            uncertain.append(e.value)

    # Gemma adjudicates only the uncertain set. Fail-toward-masking: any error
    # from classify_fn means NONE of the uncertain entries un-mask this pass.
    mot: list[str] = []
    _judged_ok = False
    if uncertain:
        try:
            verdicts = classify_fn(uncertain)
            mot_tokens = {
                r.get("token") for r in verdicts if r.get("verdict") == "MOT"
            }
            mot = [v for v in uncertain if v in mot_tokens]
            _judged_ok = True  # Gemma actually ran → these were genuinely judged
        except Exception:
            mot = []  # fail-toward-masking
            # _judged_ok stays False — a down/erroring Gemma must NOT mark these
            # as judged, or they'd be skipped forever without ever being judged.

    # Mark the judged NOM entries so later passes skip them (FIX 3). Only the ones
    # that STAYED masked (uncertain minus MOT) — a MOT entry is removed from the
    # gazetteer below, so it never recurs and needn't be remembered. Junk-lane
    # values (auto-unmasked, no Gemma) are also removed. Only mark when Gemma
    # actually ran, so an error pass re-judges next time instead of skipping.
    if _judged_ok:
        try:
            ds.mark_judged([v for v in uncertain if v not in set(mot)])
        except Exception:
            pass  # best-effort; a missed mark just re-judges next pass

    unmasked = junk + mot
    unmasked_set = set(unmasked)
    kept = [v for v in (junk + uncertain) if v not in unmasked_set]

    logged = 0
    for v in unmasked:
        kps.remove_pii(v, path=gaz_path)
        entity_type = gaz.entity_type_of(v)
        # STICKY UN-MASK (2026-07-14): make Gemma's un-mask PERSIST without a human
        # confirm. Add the value to the self-improving safe-list — the SAME thing
        # review_queue.dismiss() does for a human "not PII" verdict. The masking
        # engine checks safe_words.is_safe() and won't re-mask a safe word, so this
        # value stays un-masked across future re-indexing / re-seeds instead of the
        # old fail-toward-masking behaviour re-hiding it on the next pass. Gemma is
        # the accurate judge here; we trust its un-mask by default. FAIL-OPEN: a
        # safe-list write failure must never break the un-mask itself. (A human can
        # still CONFIRM a value back to masked via the review queue if Gemma erred —
        # confirm() removes it from safe_words + re-seeds the gazetteer.)
        try:
            from bubble_shield import safe_words as _sw
            _sw.add_safe(v)
        except Exception:
            pass
        try:
            result = rq.add_candidate(
                v, entity_type, "depollute", path=queue_path, gaz_path=gaz_path
            )
            if result is not None:
                logged += 1
        except Exception:
            pass  # logging must never break the un-mask itself

    return {"unmasked": unmasked, "kept": kept, "logged": logged}


def _classify_chunk(chunk, *, port: int, timeout: int):
    """POST ONE ≤DEPOLLUTE_CHUNK_SIZE-token chunk to the daemon.

    Fail-toward-masking: ANY error (daemon down, timeout, non-200) → [] so this
    chunk contributes NO verdicts and its tokens stay masked. Never raises.
    """
    import json, urllib.request

    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/classify_extract",
            data=json.dumps({"tokens": list(chunk)}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return []
            return json.loads(r.read()).get("results", [])
    except Exception:
        return []


def daemon_classify(tokens, *, port: int = 8724, timeout: int = 30):
    """Prod classify_fn: POST tokens to the local Gemma daemon, CHUNKED.

    The batch is split into chunks of at most DEPOLLUTE_CHUNK_SIZE tokens; each
    chunk is an independent /classify_extract request (`timeout` is now
    per-chunk). The chunks' `results` lists are concatenated in input order.

    Fail-toward-masking is preserved PER CHUNK: if one chunk's request errors /
    times out / returns non-200, that chunk simply contributes NO verdicts (its
    tokens are absent from the result → depollute keeps them masked) and the
    remaining chunks still run. A partial failure NEVER un-masks a token it did
    not get a clean GENERIQUE→MOT verdict for; it only ever removes tokens from
    the MOT set. The whole pass is never aborted by one bad chunk.
    """
    if not tokens:
        return []
    toks = list(tokens)
    results: list = []
    for i in range(0, len(toks), DEPOLLUTE_CHUNK_SIZE):
        chunk = toks[i : i + DEPOLLUTE_CHUNK_SIZE]
        results.extend(_classify_chunk(chunk, port=port, timeout=timeout))
    return results
