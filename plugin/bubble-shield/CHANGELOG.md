# Changelog — bubble-shield

All notable changes to the plugin. Bump the version in BOTH
`plugin/bubble-shield/.claude-plugin/plugin.json` and the repo-root
`.claude-plugin/marketplace.json` (two places) on every release, or clients'
`claude plugin update` will report "already at latest" and skip the new code.

## 1.17.0 — 2026-06-24 — integrate/v1.17.0: #267+#269+#276+#280(rev3) composed

Consolidated integration of 4 reviewed+approved features:

- **#269** (`fix/269-tableformer-cache`): OCR setup pre-caches TableFormer model (`setup_ocr.py`)
  — prevents silent table-OCR miss on HF_HUB_OFFLINE runtime after fresh install.
- **#276** (`feat/276-ocr-route-garbled`): `_is_garbled_extraction` detects glued PDF text
  and reroutes to OCR (`bubble_shield_extract.py`) — eliminates the entire glue-artifact class.
- **#267** (`chore/267-harden-surname-guard-v2`): expanded `_COMMON_FRENCH_SURNAMES` to ~186
  entries + `bypass_common_surname_guard` parameter so RAISON_SOCIALE-anchored surnames
  mask everywhere (`structured_ext.py`).
- **#280 rev3** (`fix/280-filename-footer-leak`): `filename_footer_matches` returns
  `(matches, footer_nom_spans)` tuple; footer-sourced spans are excluded from the
  corroboration pool in `doc_level_person_repetition_matches` — closes the self-corroboration
  loop that caused brand/insurer names to over-mask body-wide (`structured_ext.py`,
  `bubble_shield_mcp.py`, `__init__.py`).

All 4 features verified green: 255+ pytest tests, 19/19 MCP, 19/19 posttool.
MCPB re-packed at 1.17.0 (9 tools, no .bak). All vendor copies in sync.

---

## 1.16.5 — 2026-06-24 (fix #280 rev 3: close self-corroboration loop in filename-footer masking)

### Rev 3 — self-corroboration bug fix

**Root cause (rev 2 confirmed bug):** Layer 1 `filename_footer_matches()` emits a footer
NOM for every filename token (incl. brand names like ZEPHYRA). Layer 2
`doc_level_person_repetition_matches()` was building its corroboration pool
`nom_detected_words` from the SAME `found` list — which already included those Layer-1
footer NOMs. So ANY filename token in the footer self-corroborated → seeded body-wide →
over-masked insurer/brand names.

**Proof:** "ZEPHYRA DUPONT - DER.pdf", body mentions ZEPHYRA (insurer Multisupport) →
ZEPHYRA masked body-wide (0 un-masked body occurrences). PREDICA passed rev2 ONLY because
it's on the stop-list — a false pass, not a real fix.

**Fix (rev 3):**
- `filename_footer_matches()` now returns `(matches, footer_nom_spans)` where
  `footer_nom_spans` is a `frozenset` of `(start, end)` tuples covering all emitted NOMs.
- `_detector` in `make_structured_detector` captures this frozenset and passes it to Layer 2.
- `doc_level_person_repetition_matches()` accepts `footer_nom_spans` parameter and EXCLUDES
  those spans when building `nom_detected_words`. Only independent body-recognizer NOMs
  (civility "M. DUPONT", form-label, signataire) count for corroboration.

**Result:**
- ZEPHYRA (insurer): footer NOM excluded from pool → no independent NOM → NOT corroborated
  → NOT seeded body-wide → body occurrences survive. Footer still masks via Layer 1 ✓.
- DUPONT (client, "M. DUPONT" body): civility NOM in pool (not footer) → corroborated →
  body-wide mask ✓.
- TESTONI + "M. TESTONI" signal: independent civility NOM → corroborated → body mask ✓.
- Footer-only TESTONI (no body signal): Layer 1 positional masks footer; body survives ✓.

**Files changed (rev 3):**
- `bubble_shield/structured_ext.py` + 2 vendor copies (3 in sync):
  - `filename_footer_matches` → returns `(matches, footer_nom_spans)`
  - `make_structured_detector._detector` → captures and passes `footer_nom_spans`
  - `doc_level_person_repetition_matches` → `footer_nom_spans` parameter, exclusion logic
- `tests/test_280_filename_footer.py` → D15 updated (unpack tuple), D16 replaced with
  4 regression tests: D16a ZEPHYRA, D16b KORRIGAN, D16c corroborated TESTONI, D16d footer-only.
- D5 round-trip: updated to use "M. TESTONI" for independent corroboration.
- MCPB re-packed at 1.16.5 (no version bump).

**Test suite (rev 3):** 255 tests, all green (4 new D16 regression tests added).
Scripts: posttool 19/19, MCP 19/19, test_259/264/266/267/273/275 all pass.

---

## 1.16.5 — 2026-06-24 (fix #280 rev 2: corroboration + positional — over-mask ship-blockers closed)

### High-severity fix: systematic client-name leak via footer boilerplate (#280)
### Rev 2 (same release, same version): over-mask ship-blockers closed

**Root cause (#280):** Every signed CGP PDF ends with:
  `"Page de signatures complémentaire au document DURAND Théophile - DER 012026..."`
The client's full name was leaking verbatim in this footer. No content recognizer caught it.

**Original fix (rev 1):** Seeded ALL non-stop-list filename tokens body-wide.
**Problem (reviewer-proven):** The stop-list is inherently incomplete. Major FR insurers
PREDICA, HELVETIA, PREVOIR, ARIAL, AG2R, ALLIANZ, MONCEAU, CNP, SEQUOIA were not in it.
"PREDICA DUPONT.pdf" → seeded PREDICA → masked "PREDICA" (insurer name) in body everywhere.
Similarly: BOURSE missing → over-mask; FINAL missing; LIASSE/FISCALE missing.

**Rev 2 fix — corroboration + positional approach (wins over extending the stop-list):**

Layer 1 — **`filename_footer_matches(text, candidates)`** — new positional function:
  Directly emits NOM matches where filename person-tokens appear in footer boilerplate
  (`"au document <name-fragment>"`). Covers the pure-footer case (name ONLY in footer,
  no body corroboration needed) without body-wide seeding. Safe even for PREDICA —
  the footer reference to the filename is correctly masked, but no body-wide seed.

Layer 2 — **Corroboration in `doc_level_person_repetition_matches`**:
  A filename candidate token is promoted to a body-wide seed ONLY when it is
  corroborated — i.e. the token already appears in a NOM match detected by another
  recognizer (civility, form-label, signataire, or the footer NOM from Layer 1).
  - "DUPONT" from civility "M. DUPONT" → NOM → corroborated → body-wide seed → masks ✓
  - "PREDICA" → no NOM detection anywhere → NOT corroborated → NOT seeded → body untouched ✓
  - Footer-only case: Layer 1 NOM provides corroboration for Layer 2 → body seed activates ✓

Belt-and-suspenders: stop-list extended with reviewer-missing tokens:
  PREDICA, HELVETIA, PREVOIR, ARIAL, AG2R, ALLIANZ, MONCEAU, CNP, SEQUOIA,
  BOURSE, FINAL, FINALE, LIASSE, FISCALE, and other major FR insurers.

**Recall preserved:** TESTONI (footer-only) → Layer 1 positional catches footer ✓.
TESTONI (body+footer, via "M. TESTONI") → Layer 1 covers footer, Layer 2 covers body ✓.

**1. `extract_person_tokens_from_filename(basename)`** — extended stop-list (rev 2).

**2. `filename_footer_matches(text, candidates)`** — new, emits NOM for footer-quoted tokens.

**3. `make_structured_detector(filename_basename="")`** — now calls `filename_footer_matches`
   before the repetition pass (Layer 1), so footer NOMs are in `found` for corroboration.

**4. `doc_level_person_repetition_matches(text, found, filename_seeds=None)`** — rev 2:
   corroboration filter before body-wide seeding.

**5. `bubble_shield/__init__.py`** — `__version__` aligned to 1.16.5 (was stuck at 0.2.0).

**6. Threading in `bubble_shield_mcp.py`** (carried from rev 1).

**Files changed:**
- `bubble_shield/structured_ext.py` + 2 vendor copies (3 in sync)
- `bubble_shield/__init__.py` + 2 vendor copies (version 0.2.0 → 1.16.5)
- `plugin/bubble-shield/scripts/bubble_shield_mcp.py` + mcpb/server copy (3 in sync)
- `plugin/bubble-shield/.claude-plugin/plugin.json` — version 1.16.5 (unchanged)
- `tests/test_280_filename_footer.py` — 6 new tests (D11–D16): PREDICA/HELVETIA body
  no-mask, BOURSE/LIASSE/FISCALE no-mask, positional footer, corroboration feedback.
- MCPB re-packed at 1.16.5.

**Test suite:** 246 existing + 6 new = 252 tests, all green.
Scripts: test_257/259/264/266/267/273/275/posttool/256/260 — all pass.

## 1.16.4 -- 2026-06-24 (chore #267 v2: recall regression fix — raison-sociale bypass)

### Recall regression fix (#267-v2, reviewer-reported)

**Root cause (regression introduced by #267 v1):** `_person_name_seeds()` applied the
`_COMMON_FRENCH_SURNAMES` guard to ALL lone-token seeds, including those derived from a
confirmed RAISON_SOCIALE match (e.g. "SELARL GARCIA TESTONI"). A newly-listed common
surname that IS the client's name would be excluded from lone-token seeds, causing
standalone body repetitions ("GARCIA agit seul", "GARCIA a décidé") to LEAK even
though the header ("SELARL GARCIA TESTONI") was correctly masked.

**Fix:** Added `bypass_common_surname_guard=False` parameter to `_person_name_seeds()`.
The RAISON_SOCIALE path in `doc_level_person_repetition_matches` now calls with
`bypass_common_surname_guard=True` — the company match already confirms the token IS
the client's name, so the "common word in prose" concern does not apply.

**Both properties preserved:**
1. Known-client surname (anchored to raison sociale) masks everywhere, including
   standalone body repetitions and right-glued PDF artifacts. (RECALL)
2. Unanchored common surname in a doc with NO matching company → NOT masked. (PRECISION)

**Scope of change:**
- `structured_ext.py`: `_person_name_seeds()` new `bypass_common_surname_guard` param
  + `doc_level_person_repetition_matches()` RAISON_SOCIALE call-site uses `bypass=True`.
- `test_267_surname_guard.py`: D23 corrected (lone GARCIA IS in seeds via bypass);
  D32-D36 added (standalone LECLERC masked, standalone PEREZ unanchored NOT masked,
  PEREZ-as-client masked everywhere).
- `test_275_right_glue.py`: D10 updated (LEBLANC as known-client name IS right-glue-
  masked — old "NOT masked" expectation was based on the pre-fix behavior).
- All three copies synced; MCPB re-packed at 1.16.4.

**Data-only change from #267 v1 still in place:**
- `_COMMON_FRENCH_SURNAMES` expanded from ~60 to ~186 entries (GARCIA, NGUYEN, PEREZ,
  LECLERC, CHEVALIER, FERNANDEZ, GONZALEZ, MARTINEZ, LAURENT, SIMON, MICHEL, ROUX,
  FONTAINE, etc.) — guard still applies on the unanchored/prose path.

## 1.17.0 — 2026-06-24 (feat #276: route GARBLED-extraction PDFs through OCR)

### Feature — GARBLED native PDF extraction routed through OCR pack (#276)

**Root cause:** pypdf extracts text from the liasse fiscale but GLUES words together
(no spaces between tokens): "gérantETESTONI", "FAKENAMESignature". Per-boundary fixes
(#273 left-glue, #275 right-glue) catch most of these, but a 4-char forename like
"FAKENAME" cannot be safely caught without lowering the length floor and over-masking
common names (JEAN, PAUL, etc.).

**DURABLE fix:** when native PDF extraction returns non-empty text that looks GARBLED
and the OCR pack is installed, re-extract via OCR (clean, properly-spaced,
layout-aware text) and anonymise THAT — eliminating the entire glue-artifact class
at once.  Fail-open: if the OCR pack is absent or fails, the native text is used
as before (with the #273/#275 per-boundary fixes still applied).

**Heuristic `_is_garbled_extraction(text) -> bool`** — all three signals must fire:
1. **Long-token rate ≥ 3 %**: tokens > 25 chars (excluding URLs/email addresses)
   as a fraction of all tokens.  Glued liasse text produces many super-long tokens
   ("SIGNATAIREFAKENAMEETESTONISignature"); normal French prose has almost none.
2. **Low space density**: fewer than 1 space per 10 characters.  Normal French prose
   is ~1 space per 5–6 chars; garbled liasse extractions are much denser.
3. **CamelCase-glue signature ≥ 3**: occurrences of `[a-z][A-Z]` or `[A-Z]{2,}[a-z]`
   transitions.  These are the exact signature of PDF-extraction word-fusion:
   the casing of one word collides with the next.  URLs/headers don't produce this.

**Conservative by design:** all three signals must fire together (AND logic).
Any single signal alone would be too noisy.  When unsure → return False (keep native
text).  False-positive (OCR a clean doc) is a perf/recall cost; false-negative
(miss garbled) just falls through to #273/#275 — so bias toward NOT-garbled.

**Wiring in `extract_pdf_text`:**
- After pypdf extraction yields non-empty text, call `_is_garbled_extraction(text)`.
- If True AND `_ocr_pdf_if_pack_present` returns text → return the OCR text.
- The `[OCR]` quality note is already prepended by `_ocr_pdf_if_pack_present`, so
  callers see the caveat automatically.
- If False OR OCR unavailable/fails → return native text unchanged (current behaviour
  + #273/#275 per-boundary fixes still apply).

**Location:** `bubble_shield_extract.py`
- `_is_garbled_extraction()` — new function added before `extract_pdf_text` (line ~137).
- `extract_pdf_text()` — garbled-route block added after the empty-text check.

### Test `scripts/test_276_garbled_route.py` — 22/22

- **Test 1:** garbled liasse-like text (glued tokens, long tokens, CamelCase
  transitions) → `_is_garbled_extraction` = True (2 fixtures: GARBLED_TEXT,
  DENSE_GARBLED with tokens > 25 chars).
- **Test 2:** clean texts → `_is_garbled_extraction` = False (7 cases: clean French
  prose, ALL-CAPS headings, long URL, long legitimate words, empty, single word,
  label-value pairs).
- **Test 3a:** garbled native text + OCR pack present (mocked) → OCR text used;
  `[OCR]` tag present; mock called exactly once.
- **Test 3b:** garbled native text + OCR pack absent (mock returns None) → native
  text returned, no crash (fail-open verified).
- **Test 4:** clean native text → OCR mock NOT called (clean docs never OCR'd).
- **Test 5:** edge cases (one-artifact-in-otherwise-clean doc, all-caps non-glued,
  mixed doc) — heuristic does not crash; all-caps-clean correctly returns False.

### Note on real-liasse confirmation
The real-liasse end-to-end test (re-running the actual liasse through OCR) requires
the OCR pack + model installed.  Tony to confirm post-merge that the liasse is now
extracted cleanly and "FAKENAME" is masked.  All tests in the PR use synthetic PII.

### All copies synced + MCPB re-packed at 1.17.0 (9 tools, no .bak)
- Root `bubble_shield/` → `plugin/bubble-shield/vendor/` →
  `plugin/bubble-shield/mcpb/server/vendor/` (engine unchanged for this PR).
- `scripts/bubble_shield_extract.py` → `mcpb/server/scripts/bubble_shield_extract.py`.
- `scripts/test_276_garbled_route.py` → `mcpb/server/scripts/test_276_garbled_route.py`.
- MCPB re-packed (9 tools intact, no .bak).

### All suites green
`test_276_garbled_route` 22/22, `test_275_right_glue` 47/47,
`test_273_glued_token` 32/32, `test_266_person_name_corporate` 57/57,
`test_264_repeated_company` 31/31, `test_259_corporate_kyc` 20/20,
`test_257_form_layout` 42/42, `test_bubble_shield_mcp` 19/19,
`test_posttool_anonymize` 19/19, `test_256_daemon_path_fail_loud` 10/10,
`test_260_ocr` green, `test_option_b_e2e` pre-existing Python 3.9 compat skip.
pytest 229/229.

## 1.16.4 — 2026-06-24 (fix #269: pre-cache TableFormer at OCR setup)

### Bug fixed — TableFormer (table-structure model) not pre-cached at setup (risk:LOW, table OCR miss)

- **Root cause (#269):** The OCR setup warm-script used `do_table_structure=False`, so the
  TableFormer model (which lives in the same `docling-models` HF repo as the layout model
  but is fetched separately only when `do_table_structure=True`) was never downloaded during
  setup.  On a fresh install with `HF_HUB_OFFLINE=1` at runtime, any table-heavy scanned
  PDF silently produced zero table output (cache-miss → fail-closed, no crash, no privacy
  risk — just missing tables).  The review of #260 only passed because the test machine
  already had `docling-models` cached locally.

- **Fix:** `_WARM_MODEL_SCRIPT` now uses `do_table_structure=True` (aligned with the runtime
  setting), so both the layout model AND TableFormer are exercised and downloaded in the
  same `DocumentConverter()` instantiation during setup.  The `~750MB` estimate in the
  docstring is updated accordingly.

- **Sentinel hardening:** `ensure_models_cached()` (renamed from `ensure_layout_model_cached`,
  old name kept as alias) writes `layout_model_cached.flag` ONLY after the warm script exits
  with `returncode=0` and `stdout.startswith("OK")`.  A partial cache (e.g. TableFormer
  download error → non-zero returncode) raises `RuntimeError` and leaves the sentinel absent,
  so the next setup re-run picks up from scratch.

- **Privacy guarantee unchanged:** `HF_HUB_OFFLINE=1` + `TRANSFORMERS_OFFLINE=1` still
  enforced in every runtime subprocess invocation (Test 0 re-confirmed).

- **Tests extended (`test_260_ocr.py`):**
  - Test 3 (#269): mocked subprocess returns `"OK models cached"` → sentinel written.
  - Test 4 (#269): mocked subprocess returns non-zero → RuntimeError raised, sentinel absent.
  - All 5 tests pass (Tests 0–4).

- **Note:** #267 also targets 1.16.4 — if both land, Tony resolves at merge; this PR uses
  1.16.4 as instructed.

## 1.16.3 — 2026-06-24 (fix #275: right-glued forename leak — mirror of #273)

### Ship-blockers fixed (v2 — over-mask guards, 2026-06-24)

Two over-masking regressions found in PR review. Both fixed in the same version
(no version bump — SAME 1.16.3, hotfixed before merge):

**Ship-blocker 1 — Forename over-mask** (ANDREA→ANDREAssistant, CLAIRE→CLAIREment):
- Root cause: common French forenames (len≥6) are frequent word-prefixes in French.
  The right-glue pass would fire for ANDREA in "ANDREAssistant" (next char 'A' = letter),
  masking the legitimate French word instead of just the client name.
- Fix: added `_COMMON_FRENCH_FORENAMES` exclusion set (~80 entries: CLAIRE, JULIEN,
  ANTOINE, ANDREA, ISABELLE, NATHALIE, SANDRINE, CAROLINE, CHRISTELLE, THEOPHILE,
  FREDERIC, NICOLAS, STEPHANE, CATHERINE, VERONIQUE, DOMINIQUE, EMMANUEL, GUILLAUME,
  etc.). Seeds in this set do NOT get the right-glue pass. Standard + left-glue (#273)
  passes still fire for these seeds (they're still masked when isolated or left-glued).

**Ship-blocker 2 — Seed-prefix-of-longer-word** (TESTONImania, TESTONIal):
- Root cause: the right-glue check fired for ANY following alphabetic char, including
  lowercase continuations that form a single inflected French word (TESTONImania,
  CLAIREment, ANTOINEtte, JULIENne).
- Fix: the right-glue pass now only fires when the FOLLOWING char is UPPERCASE
  (`[A-Z\xc0-\xd6\xd8-\xde]`). CamelCase = PDF extraction glue artifact
  ("FAKENAMESignature", "FAKENAMESignature" — capital S). Lowercase continuation =
  inflected French word — never mask.
- `structured_ext.py` line: `re.match(r"[A-Z\xc0-\xd6\xd8-\xde]", text[end])`.

**Both guards combined:** forename exclusion + uppercase-next-char.

**Verified still masks (real artifact):** "FAKENAMESignature" (F, not a forename),
"TESTONIDocument" (uppercase D), "FAKENAMESignature" (FAKENAME len=4 < 6, still masked by
standard pass anyway). "ANDREA DUPONT" pair still masked via standard pair-seed pass.

**Verified does NOT mask (ship-blockers):** "ANDREAssistant" (ANDREA in forenames),
"CLAIREment" (CLAIRE in forenames + lowercase), "JULIENne" (JULIEN in forenames),
"ANTOINEtte" (ANTOINE in forenames), "TESTONImania" (lowercase 'm' next).

### Bug fixed (v1) — FORENAME LEAK when right-glued to following word by PDF extraction artifact (risk:HIGH)

- **Root cause:** the mirror of #273. PDF text extraction omits the space between
  a known forename/surname ("FAKENAME") and the FOLLOWING word ("Signature"),
  producing "FAKENAMESignature" with no whitespace. The standard right word-boundary
  check `(?![A-Za-z])` fails because "S" IS a letter immediately after the seed, so
  the known forename stayed in clear — the surname was already masked as a separate
  token. Real output observed: `⟦NOM_0002⟧ FAKENAMESignature`.

### Fix — Option B (detection fix) — `structured_ext.py` `doc_level_person_repetition_matches`

- For RAISON_SOCIALE-derived lone-token seeds (len >= 6, not a common surname AND
  not a common French forename), compile a `right_glued_pattern` with a loose RIGHT
  boundary but a strict LEFT boundary `(?<![A-Za-z\xc0-\xff])`.
- After the standard word-boundary + left-glue (#273) scans, run the right-glue
  pattern and emit any occurrence whose FOLLOWING character IS an UPPERCASE letter —
  confirming a genuine CamelCase PDF-glue artifact (not a French word continuation).
- **Match value:** only the seed itself (e.g. "FAKENAME" — 8 chars), not the following
  word ("Signature"). The following word survives in the anonymised output.
- Precision guards:
  1. Seeds shorter than 6 chars never enter `raison_sociale_lone_seeds`.
  2. Seeds in `_COMMON_FRENCH_SURNAMES` excluded by `_person_name_seeds()`.
  3. Seeds in `_COMMON_FRENCH_FORENAMES` excluded from right-glue pass (new, v2).
  4. Following char must be UPPERCASE (new, v2).
  5. Strict LEFT boundary: no partial-tail matches.
- **Location:** `structured_ext.py` `doc_level_person_repetition_matches`
  (right_glued_pattern gate + scanning loop, after the #273 left-glue pass).

### Note on signataire label lines
When `FAKENAMESignature` appears as the value of a signataire/gérant label
("Signataire : FAKENAMESignature"), the `signataire_matches` recognizer correctly
masks the ENTIRE value as NOM (it is the complete name field). The right-glue fix
provides additional coverage for occurrences in free-text / unlabeled positions
(headers, footers, free-text paragraphs).

### Test `scripts/test_275_right_glue.py` — extended (47/47)
- D1-D15: original right-glue tests (all pass).
- D16-D21: forename exclusion set — CLAIRE/JULIEN/ANTOINE/ANDREA/ISABELLE in set;
  FAKENAME NOT in set.
- D22-D24: ANDREA forename does NOT over-mask ANDREAssistant / JULIENne.
- D25-D26: ship-blocker 2 — TESTONImania NOT masked (lowercase next);
  TESTONIDocument IS masked (uppercase next).
- 47/47 passed.

### Other
- All 3 copies synced (root `bubble_shield/` → `plugin/bubble-shield/vendor/` →
  `plugin/bubble-shield/mcpb/server/vendor/`). Test scripts synced to both
  `scripts/` and `mcpb/server/scripts/`. MCPB re-packed at 1.16.3 (no .bak,
  OCR 9 tools intact).
- All suites green: `test_275_right_glue` 47/47, `test_273_glued_token` 32/32,
  `test_266_person_name_corporate` 57/57, `test_264_repeated_company` 31/31,
  `test_259_corporate_kyc` 20/20, `test_257_form_layout` 42/42,
  `test_bubble_shield_mcp` 19/19, `test_posttool_anonymize` 19/19,
  `test_256_daemon_path_fail_loud` 10/10, `test_260_ocr` green,
  `test_option_b_e2e` pre-existing Python 3.9 compat skip (str|None syntax).
  pytest 229/229.

## 1.16.2 — 2026-06-24 (fix #273: glued-token surname leak in liasse fiscale)

### Bug fixed — SURNAME LEAK when glued to preceding token by PDF extraction artifact (risk:HIGH)

- **Root cause:** in some liasse fiscale PDFs, text extraction omits the space
  between a preceding token (e.g. a POSTE/role word such as "gérant") and the
  following surname, producing "gérantETESTONI" — with a trailing artifact char
  ("E") belonging to the preceding word. The anonymised output therefore showed
  `⟦POSTE_0003⟧ESURNAME` (the POSTE token immediately followed by the bare
  surname). The doc-level person-repetition pass (#266) uses a strict left word
  boundary `(?<![A-Za-z])SURNAME` that fails here because "E" IS a letter, so
  the glued surname was never detected and leaked in clear.
  The same SURNAME was correctly masked in a separate, clean occurrence
  (`⟦NOM_0001⟧`) — confirming this is purely a boundary/extraction miss, not
  a vault or recognizer error.
  DCC .docx variants are CLEAN; this is liasse/PDF-extraction-specific.

### Fix — Option A + Option B (both applied)

**Option B (detection fix) — `structured_ext.py` `doc_level_person_repetition_matches`**
- Track which seeds came from RAISON_SOCIALE extraction separately as
  `raison_sociale_lone_seeds` (lone tokens with no space, length ≥ 6).
- For these seeds only, compile a second `glued_pattern` with NO left-char
  restriction (only the strict right boundary `(?![A-Za-z])` remains).
- After the standard word-boundary scan, run the glued pattern and emit any
  occurrence whose PRECEDING character IS alphabetic or a digit — confirming
  it is a genuine glue artifact the standard scan missed.
- Precision guards: seeds shorter than 6 chars or in `_COMMON_FRENCH_SURNAMES`
  are excluded by `_person_name_seeds()` BEFORE this code runs, so "MARTIN",
  "BLANC", "PETIT", etc. are never loose-boundary-scanned. The right boundary
  still prevents partial matches inside longer words (e.g. "TESTONIAN").
- **Location:** `structured_ext.py` `doc_level_person_repetition_matches`
  (seed collection loop + per-seed scanning loop).

**Option A (output normalisation) — `engine.py` `anonymize()`**
- Added `_GLUED_TOKEN_RE = re.compile(r"(⟧)([A-Za-z\xc0-\xff])")` at module
  level.
- After all token substitutions, apply `_GLUED_TOKEN_RE.sub(r"\1 \2", out)` to
  insert a space between `⟧` and any immediately adjacent alphabetic character.
  This fixes the output representation (`⟦POSTE_0003⟧ESURNAME` →
  `⟦POSTE_0003⟧ ESURNAME`) even if Option B missed the detection.
  Display normalisation only — does not change detection coverage.
- **Location:** `engine.py` (module-level constant + `anonymize()` post-loop).

### Precision (same guards as #264/#266 — over-mask is a ship-blocker)
- Common-surname lone-token seeds (MARTIN/BLANC/PETIT) excluded by
  `_COMMON_FRENCH_SURNAMES` BEFORE the loose-boundary pass — never scanned
  loosely.
- Lone seeds shorter than 6 chars never enter `raison_sociale_lone_seeds`.
- Right boundary always strict: no partial word matches.
- "Forme juridique : SELARL", "La SELARL exerce", "blanc" in prose → NOT masked.
- All #266/#264/#259/#257/#256 precision controls green.

### New test `scripts/test_273_glued_token.py`
- Part A (daemon DOWN): glued TESTONI masked; FAKENAME masked; 'Forme juridique'
  preserved; precision block unchanged.
- Part A2 (precision): common-word names NOT masked.
- Part B (de-anon round-trip via Python API): anon has NOM tokens, TESTONI/FAKENAME
  gone; de-anon restores both.
- Part C (daemon UP if available): same checks.
- Part D unit tests D1–D12: glued detection, raison_sociale_lone_seeds set,
  common-word guards, full combined detector, Option A engine normalisation,
  de-anon round-trip, precision controls.
- 36/36 passed.

### Other
- All 3 copies synced (root `bubble_shield/` → `plugin/bubble-shield/vendor/` →
  `plugin/bubble-shield/mcpb/server/vendor/`). Test scripts synced to both
  `scripts/` and `mcpb/server/scripts/`. MCPB re-packed at 1.16.2 (no stray .bak).
- All suites green: `test_273_glued_token` 36/36, `test_266_person_name_corporate`
  62/62, `test_264_repeated_company` 37/37, `test_259_corporate_kyc` 24/24,
  `test_257_form_layout` 51/51, `test_bubble_shield_mcp` 19/19,
  `test_posttool_anonymize` 19/19, `test_256_daemon_path_fail_loud` 16/16,
  `test_260_ocr` 3/3. pytest 50/50.
  (`test_guard`/`test_guard_marker`/`test_tripwire`/`test_option_b` require
  Python ≥ 3.10; pre-existing on this machine's Python 3.9.)

## 1.16.1 — 2026-06-24 (fix #266: practitioner's personal name leaks in corporate/fiscal docs)

### Bug fixed — PERSONAL NAME leaks in signature blocks and label-less table cells (risk:HIGH)
- **Root cause:** #259/#264 mask the company name ("SELARL DU DOCTEUR FORENAME SURNAME"
  → one RAISON_SOCIALE token), but the practitioner's bare personal name standing ALONE
  (e.g. "TESTONI FAKENAME" in "Signataire : GÉRANT  TESTONI FAKENAME" or in a label-less
  table cell "/ / / TESTONI FAKENAME") was missed — GLiNER needs prose context; form
  recognizers need a "Nom :" label (absent here).

### Fix 1 — `extract_person_name_from_raison_sociale` (new, `structured_ext.py`)
- Strips the forme-juridique prefix and honorific words from a detected RAISON_SOCIALE
  ("SELARL DU DOCTEUR FAKENAME TESTONI" → residual ["FAKENAME", "TESTONI"]).
- Uses `_RAISON_SOCIALE_PREFIXES` frozenset (type words + honorifics + connectors).

### Fix 2 — `_person_name_seeds` (new, `structured_ext.py`)
- Given name tokens, returns seed strings for the doc-level repetition pass.
- Always includes the full PAIR ("FAKENAME TESTONI" + "TESTONI FAKENAME") — distinctive.
- Lone tokens only if NOT a common French surname (`_COMMON_FRENCH_SURNAMES`) and length ≥ 4.
- Precision guard: "MARTIN", "BLANC", "DUPONT", etc. are NOT lone seeds (common words).
  A practitioner named "MARTIN" will have the pair masked but not every "martin" in prose.

### Fix 3 — `signataire_matches` (new recognizer, `structured_ext.py`)
- Matches labeled signature/role blocks → NOM:
    "Signataire : TESTONI FAKENAME"
    "Gérant : FAKENAME TESTONI"
    "Nom (et qualité) du signataire/déclarant : TESTONI FAKENAME"
    "Représentant légal : FAKENAME TESTONI"
- Strips leading role/position words ("GÉRANT TESTONI FAKENAME" → value "TESTONI FAKENAME").
- Precision guards: placeholder values skipped; value capped at 80 chars.

### Fix 4 — `doc_level_person_repetition_matches` (new post-pass, `structured_ext.py`)
- Reuses the #264 doc-level repetition machinery for person names.
- After all primary detectors run, derives the person name from:
    1. RAISON_SOCIALE matches: strips company prefix → personal-name sub-span.
    2. High-confidence NOM matches with ≥2 CAPS tokens (signataire / form_nom / civility).
- Scans the FULL document for every verbatim occurrence of each seed and emits NOM.
- This catches all uncovered occurrences: label-less table cells, doubled signataire,
  "N° département  TESTONI FAKENAME", etc.
- Vault consistency: all repetition matches use the full PAIR as their value → one NOM token.
- Word-boundary-aware matching: no partial substring matches.
- ADD-only, fail-open.

### Precision guards (critical — same risk class as #264)
- `_COMMON_FRENCH_SURNAMES`: frozenset of ~60 high-frequency surnames / common words.
  These are NOT used as lone-token seeds. Pair seeds (always) + lone seeds (if distinctive only).
- `_RAISON_SOCIALE_PREFIXES` guard: forme-juridique words never used as person name seeds.
- Minimum lone-seed length: 4 characters.
- Precision: "La SELARL exerce" → NOT masked; "Forme juridique : SELARL" → NOT masked;
  common words "blanc", "martin" in prose → NOT masked.

### make_structured_detector updated
- Now calls `signataire_matches(text)` after the #264 repetition pass.
- Now calls `doc_level_person_repetition_matches(text, matches)` as the final pass.

### New test `scripts/test_266_person_name_corporate.py`
- Part A (daemon DOWN): all 5 person-name occurrences masked in CORP_BLOCK.
- Part A2 (precision): common-word names NOT masked in ordinary prose.
- Part B (daemon UP if available): same as A with NER running.
- Part C (de-anon round-trip): anon output has NOM tokens, FAKENAME/TESTONI gone, ≤2 NOM token types.
- Part D (unit tests D1–D25): extract, seeds, signataire, repetition pass, full detector, vault consistency.

### Other
- All 3 copies synced (root `bubble_shield/` → `plugin/bubble-shield/vendor/` →
  `plugin/bubble-shield/mcpb/server/vendor/`). Test scripts synced to `mcpb/server/scripts/`.
  MCPB re-packed.
- All suites green: `test_266_person_name_corporate` 57/57, `test_264_repeated_company` 31/31,
  `test_259_corporate_kyc` 20/20, `test_257_form_layout` 42/42, `test_bubble_shield_mcp` 18/18,
  `test_posttool_anonymize` 19/19, `test_256_daemon_path_fail_loud` 10/10.
  (test_guard/marker/tripwire/option_b require Python ≥10; pre-existing on this machine's 3.9.)

### Fidelity patch — doc-level person-name de-anon (Bug 1 + Bug 2 from reviewer, no leak)

**Bug 1 — round-trip name inversion** (`structured_ext.py`
`doc_level_person_repetition_matches`, ~L900):
- **Root cause:** stored `value=canonical_val` for every occurrence even when the doc
  had the inverted form ("TESTONI FAKENAME"). Vault restored all occurrences to the
  canonical form instead of the original surface text.
- **Fix:** `value=occ.group(0)` — actual matched text. vault._token_for_name groups by
  distinctive words: both forms share person-number 1 (\u27e6NOM_0001\u27e7 / \u27e6NOM_0001a\u27e7),
  each restores to its own surface form. Dead-code `nom_canonical` dict removed.
- **Test D26:** de-anon restores "TESTONI FAKENAME" unchanged (not inverted);
  all NOM tokens share one person number.

**Bug 2 — POSTE/_QUAL crosses newlines** (`recognizers.py` ~L141):
- **Root cause:** `_QUAL = r"(?:\s+...){0,3}"` used `\s+` which matches `\n`,
  swallowing the next line's "Signataire" label and breaking signataire_matches.
- **Fix:** `\s+` → `[^\S\n]+` in `_QUAL` and `_COMP`. Same-line whitespace only.
  POSTE still matches same-line qualifiers; next-line label is never swallowed.
- **Test D27:** POSTE match ends before newline; signataire name still masked;
  same-line qualifier regression-free.

## 1.16.0 — 2026-06-23 (feat #260: optional local OCR for scanned/image PDFs)

### Feature — OCR pack (optional, off by default, fail-open)
- **New: read scanned/image-only PDFs locally (board #260).** The plugin core
  continues to handle native/text PDFs with zero install. For scanned or
  image-only PDFs (no text layer), the optional OCR pack can now be installed
  once and used fully offline thereafter.
- **New MCP tool `bubble_shield_setup_ocr(action=start|status)`:** installs or
  checks the OCR pack from inside Cowork — no Terminal needed. `start` spawns
  the setup in the background; `status` reports progress. The setup creates a
  dedicated venv at `~/.bubble_shield/ocr-env/` with `docling` + `onnxruntime`
  (~150MB pip deps + ~506MB docling layout model from HuggingFace — one-time).
- **`bubble_shield_setup_ocr.py` — setup script (one-time, idempotent):**
  - Installs `docling` + `onnxruntime` into a persistent venv.
  - Downloads the docling layout model (docling-layout-heron, ~506MB) ONCE into
    the HuggingFace local cache (`ensure_layout_model_cached()`). A sentinel
    file (`layout_model_cached.flag`) marks the cache as warm.
  - Verifies the install by running a synthetic scanned-page OCR test WITH
    `HF_HUB_OFFLINE=1` enforced — proving the model loads from cache with ZERO
    network access.
  - Writes `~/.bubble_shield/ocr.json` with the venv python path.
- **Offline enforcement (PRIVACY GUARANTEE):**
  - `bubble_shield_extract._ocr_pack_python()` checks the sentinel before returning
    the venv path — if the model was not cached during setup, it returns `None`
    (fail-closed, never silently fetches at runtime).
  - `_ocr_pdf_if_pack_present()` sets `HF_HUB_OFFLINE=1` and
    `TRANSFORMERS_OFFLINE=1` in the subprocess env for every OCR invocation.
  - ZERO outbound connections after setup: the sentinel + offline flag together
    guarantee that no call to huggingface.co is ever made at OCR runtime.
  - Documentation updated: "models downloaded once at setup; OCR runs fully
    offline thereafter (HF_HUB_OFFLINE enforced)."
- **Fail-open on OCR error, fail-closed on pack absent (unchanged):** if the
  OCR pack is not installed, `extract_pdf_text` raises `ExtractionError` with
  an install hint — it never returns empty text. If the pack IS installed but
  OCR fails (e.g., illegible scan), the error falls through to `ExtractionError`
  (fail-open within the OCR path, fail-closed to the caller).
- **`[OCR]` quality note:** text extracted via OCR is prefixed with `[OCR]`;
  `bubble_shield_read` prepends a human-readable note recommending review of
  critical fields (names, dates, numbers) for OCR accuracy.
- **`test_260_ocr.py` — 3-test suite:**
  - Test 0: `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1` are verified in the
    subprocess env via mock — proves zero runtime network (no real OCR run
    needed for this assertion).
  - Test 1: pack absent → `ExtractionError` with install hint (fail-closed).
  - Test 2: pack present → OCR text + anonymise pipeline (skipped if not installed).
- **`test_bubble_shield_mcp.py` — updated:** expected tool count 8 → 9 (added
  `bubble_shield_setup_ocr`). New assertion: `setup_ocr status` returns a state.
  Suite: 19/19 passed.
- All 4 script copies synced (`scripts/` ↔ `mcpb/server/scripts/` identical).
  MCPB re-packed.
- All suites green: `test_bubble_shield_mcp` 19/19, `test_260_ocr` 3/3,
  `test_264_repeated_company` 37/37, `test_259_corporate_kyc` 24/24,
  `test_257_form_layout` 51/51, `test_guard` 21/21, `test_guard_marker` 11/11,
  `test_tripwire` 18/18, `test_posttool_anonymize` 19/19, `test_option_b_e2e` 9/9,
  `test_256_daemon_path_fail_loud` 16/16.
## 1.15.2 — 2026-06-23 (fix #264 ship-blockers: bare-type seed + two-token company)

### Bug A fix — degenerate seed corrupts the document (ship-blocker)
- **Root cause:** `doc_level_repetition_matches()` accepted any RAISON_SOCIALE seed
  whose canonical form was a bare forme-juridique type word (e.g. "SARL", "SELARL").
  Input `Raison sociale : SARL` → canonical "SARL" → the repetition pass matched
  every bare "SARL" in the document. Simultaneous over-mask ("Forme juridique : SARL"
  and "La SARL exerce…" → wrongly masked) AND under-mask ("SARL DUPONT" → only
  "SARL" masked, "DUPONT" leaked).
- **Fix:** Added `_FORME_JURIDIQUE_SET` (frozenset of all 14 type words).
  In `doc_level_repetition_matches`, skip any seed whose canonical form is a member
  of `_FORME_JURIDIQUE_SET`. Also skip the original (non-canonical) form if it is
  itself a bare type word. A bare-type seed contributes NO repetition matches.
- **Location:** `structured_ext.py` lines 419–425 (`_FORME_JURIDIQUE_SET`),
  `doc_level_repetition_matches()` (seed-collection loop, `_FORME_JURIDIQUE_SET` guards).

### Bug B fix — two vault tokens for the same company (ship-blocker)
- **Root cause:** `Dénomination de l'entreprise :` captured `SELARL SELARL DU DOCTEUR
  FAKENAME TESTONI` (doubled prefix, PDF extraction artifact) while `Raison sociale :`
  captured `SELARL DU DOCTEUR FAKENAME TESTONI`. Different `Match.value` strings →
  `vault.token_for()` (keyed by exact string) minted two different tokens
  (`⟦RAISON_SOCIALE_0001⟧` and `⟦RAISON_SOCIALE_0002⟧`) for ONE company. De-anon
  returned inconsistent surface forms.
- **Fix:** Apply `_canonical_company_name()` to `Match.value` before emitting in BOTH
  `form_raison_sociale_matches()` and `forme_juridique_anchored_matches()`. Both
  recognizers now emit `canonical_val` ("SELARL DU DOCTEUR FAKENAME TESTONI") as the
  vault key regardless of which surface form they observed. The vault sees one string →
  one token → consistent output.
- **Location:** `structured_ext.py` `form_raison_sociale_matches()` (emit block),
  `forme_juridique_anchored_matches()` (emit block).

### Test additions (D13–D20 in `test_264_repeated_company.py`)
- D13: bare SARL seed → 0 repetition matches (Bug A guard)
- D14: "Forme juridique : SARL" NOT masked as RAISON_SOCIALE (Bug A)
- D15: prose "La SARL exerce…" NOT masked as RAISON_SOCIALE (Bug A)
- D16: real multi-word canonical seed still produces repetition matches (Bug A, no regression)
- D17/D17b: doubled-prefix and clean form → same canonical value from form_raison_sociale_matches (Bug B)
- D18: vault emits different tokens for different raw strings (confirms fix must be in recognizer)
- D19: structured_ext emissions all share one value → single vault key (Bug B)
- D20: end-to-end simulation → both lines get the same ⟦RAISON_SOCIALE_0001⟧ token (Bug B)

### Other
- All 3 copies synced (root `bubble_shield/` → `plugin/bubble-shield/vendor/` → `plugin/bubble-shield/mcpb/server/vendor/`). Test scripts synced to `mcpb/server/scripts/`. MCPB re-packed.
- All suites green: `test_264_repeated_company` 37/37, `test_259_corporate_kyc` 24/24, `test_257_form_layout` 51/51, `test_bubble_shield_mcp` 18/18, `test_guard` 21/21, `test_guard_marker` 11/11, `test_tripwire` 18/18, `test_posttool_anonymize` 19/19, `test_option_b_e2e` 9/9, `test_256_daemon_path_fail_loud` 16/16.

## 1.15.1 — 2026-06-23 (fix #264: repeated company name leaks in liasse fiscale)

- **LEAK FIXED — corporate name repeated 14× in liasse fiscale.** Once #259 masked
  the labeled `Raison sociale :` line, the practitioner's company name (SELARL DU
  DOCTEUR …) still appeared unmasked as free-standing page/table headers throughout
  the liasse fiscale — the label-anchored recognizer naturally misses unlabeled
  repetitions. The company name leaked in clear in every table header, footer line,
  and section title.
- **Root cause:** PII detection is span-based — only the labeled occurrence was
  matched. Subsequent verbatim repetitions of the same string in unlabeled positions
  had no anchor to trigger a match.
- **Fix 1 — extend `_RAISON_SOCIALE_LABEL_RE`:** added the `de l'entreprise` label
  variant (`Dénomination de l'entreprise :`) which the #259 pattern missed. Liasse
  fiscale uses this exact label form.
- **Fix 2 — `forme_juridique_anchored_matches` (new, `structured_ext.py`):**
  Catches unlabeled company-name headers anchored by a forme-juridique type word
  (`SELARL|SARL|SAS|SCI|SCM|SCP|SASU|SNC|EURL|SCOP|…`), including the doubled-
  prefix extraction artifact `SELARL SELARL DU DOCTEUR …`. Emits RAISON_SOCIALE
  for the full span (type word + name) at score 0.82 (below labeled 0.90; overlap
  resolution keeps the higher-scoring labeled match when both fire on the same span).
  Precision guards: bare `SELARL` alone not matched; `Forme juridique : SELARL`
  label line not matched (line-prefix check); prose `La SELARL exerce…` not matched
  (ALL-CAPS name continuation required); `Type : SAS` not matched.
- **Fix 3 — `doc_level_repetition_matches` post-pass (new, `structured_ext.py`):**
  After all primary detectors run, collects every unique RAISON_SOCIALE value
  (canonicalised — doubled prefix stripped), then scans the full document for every
  verbatim occurrence and emits a RAISON_SOCIALE match for each span not already
  covered. This is the definitive belt-and-suspenders fix: even if the
  forme-juridique-anchored pattern can't fire (unusual layout), once the labeled
  occurrence is found the post-pass finds the other 13 repetitions. ADD-only,
  fail-open.
- **Canonical normalisation helper `_canonical_company_name`:** strips the doubled-
  prefix artifact (`SELARL SELARL DU DOCTEUR X` → `SELARL DU DOCTEUR X`) so the
  vault lookup is consistent and both forms resolve to the same canonical key for
  the repetition search.
- **New test `scripts/test_264_repeated_company.py`:** 4-part (daemon DOWN / UP /
  unlabeled-only / unit), 28 assertions. Full synthetic liasse block with all 5
  occurrence types (labeled variant, doubled-prefix, standalone header,
  prefixed header, date-prefixed header). Precision controls: `Forme juridique :
  SELARL` and `La SELARL exerce une activité` → NOT masked.
- All 3 copies re-synced (root `bubble_shield/` → `plugin/bubble-shield/vendor/`
  → `plugin/bubble-shield/mcpb/server/vendor/`). `scripts/` ↔
  `mcpb/server/scripts/` identical. MCPB re-packed.
- All suites green: `test_264_repeated_company` 28/28, `test_259_corporate_kyc`
  24/24, `test_257_form_layout` 51/51, `test_bubble_shield_mcp` 18/18,
  `test_guard` 21/21, `test_guard_marker` 11/11, `test_tripwire` 18/18,
  `test_posttool_anonymize` 19/19, `test_option_b_e2e` 9/9,
  `test_256_daemon_path_fail_loud` 16/16.

## 1.15.0 — 2026-06-23 (fix #259: corporate KYC PII leaks — raison sociale + SIRET NIC suffix)

- **LEAK 1 FIXED — Raison sociale leaks in clear.** Form line `Dénomination ou
  raison sociale : SELARL DU DOCTEUR <PERSON NAME>` returned the company name
  unmasked. For SELARL/SCM/SCI/SCP practices the company name EMBEDS the
  practitioner's personal name. No recognizer existed for `dénomination / raison
  sociale :` label lines.
- **Fix — `structured_ext.py` — new `form_raison_sociale_matches` recognizer:**
  Matches `Dénomination (ou raison sociale)? :` and `Raison sociale :` label
  lines → entity **RAISON_SOCIALE**, masks the whole company name value. ADD-only,
  fail-open, daemon-independent (deterministic, same contract as #257 recognizers).
  Precision guards: requires explicit label+colon; skips placeholder values
  (`néant`, `N/A`, etc.); value must contain at least one letter; capped at 120
  chars. Does NOT mask `Forme juridique : SELARL` — only labeled dénomination/
  raison-sociale lines. Wired into `make_structured_detector()`.
- **LEAK 2 FIXED — SIRET NIC suffix leaks.** Output showed
  `N° SIRET : ⟦SIREN_0001⟧-NNNNN` — the 9-digit SIREN masked but the 5-digit
  NIC suffix (`00011`) stayed in clear, making the full 14-digit SIRET
  reconstructable. Root cause: the SIRET regex `\d{3}[ ]?\d{3}[ ]?\d{3}[ ]?\d{5}`
  only tolerated spaces between groups. Real DCC forms use a hyphen between the
  SIREN and NIC (e.g. `123 456 789-00011`), which the old pattern missed —
  causing the SIRET to match only 9 digits (SIREN), leaving the NIC exposed.
- **Fix — `recognizers.py` — SIRET pattern updated:** Changed `[ ]?` separators
  to `[ -]?` so the SIRET recognizer (priority 93) now catches all variants:
  spaced (`123 456 789 00011`), compact (`12345678900011`), and hyphen-separated
  (`123 456 789-00011` / `123456789-00011`). Fix #259.
- **Regression test `scripts/test_259_corporate_kyc.py`:** 4-part test covering
  daemon DOWN and daemon UP paths, with:
  - Raison sociale masking for SELARL with embedded non-gazetteer name
  - Full 14-digit SIRET masking (hyphen-separated form)
  - Control: `Forme juridique : SELARL` type-word NOT masked
  - Prose control: no false positives on unlabeled text
  Uses synthetic-only PII (`FAKENAME TESTONI`, `123 456 789 00011`).
- All 3 copies re-synced (root `bubble_shield/` → `plugin/bubble-shield/vendor/`
  → `plugin/bubble-shield/mcpb/server/vendor/`). `scripts/` ↔
  `mcpb/server/scripts/` identical. MCPB re-packed.
- All suites green: `test_259_corporate_kyc` 8/8, `test_257_form_layout` 49/49,
  `test_bubble_shield_mcp` 18/18, `test_guard` 21/21, `test_guard_marker` 11/11,
  `test_tripwire` 18/18, `test_posttool_anonymize` 19/19, `test_option_b_e2e` 9/9,
  `test_256_daemon_path_fail_loud` 16/16.

## 1.14.1 — 2026-06-23 (fix #257-b: DOB leak via Né(e) le + placeholder guard)

- **BLOCKER (PR #3 reviewer): birthdate `03/05/1980` leaked in clear** in the
  form pattern `Né(e) le : DD/MM/YYYY`. Root cause: the core `DATE_NAISSANCE`
  recognizer regex `n[ée]e?\s+le` did NOT match `Né(e) le` because the literal
  parenthetical `(e)` breaks the `e?` alternation. The 1.14.0 release added
  Nom/Lieu/Pièce recognizers but left the DOB recognizer un-fixed, and
  `test_257_form_layout.py` had no assertion for birthdate masking — so the bug
  was missed by the green test run.
- **Fix — `recognizers.py` (Option A):** extended the `DATE_NAISSANCE` recognizer
  alternation from `n[ée]e?\s+le` to `n[eé](?:e|\(e\))?\s+le` so that `Né(e) le`,
  `né(e) le`, `née le`, `né le`, and `nee le` all match. The fix is in the core
  regex (daemon-independent) and fires in both daemon DOWN and UP paths. No
  existing DATE_NAISSANCE tests regress. (file: `bubble_shield/recognizers.py`,
  recognizer at line ~170 in the RECOGNIZERS list.)
- **Advisory fix — placeholder guard in `structured_ext.py`:** form label
  recognizers now skip placeholder/empty-marker values (`(vide)`, `N/A`, `néant`,
  `non renseigné`, etc.) that appear in unfilled template fields. Previously
  `Prénom : (vide)` produced a false NOM match that could corrupt the vault and
  mask template boilerplate. Guard is applied in `form_nom_matches` and
  `form_lieu_naissance_matches`. New `_PLACEHOLDER_VALUES` frozenset + `_is_placeholder()`
  helper added.
- **`test_257_form_layout.py` — new assertions (was missing, now explicit):**
  - Part A (daemon DOWN): `"03/05/1980" not in output` — the blocker assertion.
  - Part B (daemon UP): same assertion guarded behind daemon availability check.
  - Part D (unit): `DATE_NAISSANCE` recognizer tested for `Né(e) le` form directly;
    placeholder guard tested for `(vide)`, `N/A`, `néant` → no NOM emitted.
  - Total: 49 assertions (was 42).
- **CHANGELOG correction:** 1.14.0 entry falsely claimed DOB was among the fields
  fixed. Corrected — 1.14.0 fixed Nom/Prénom/Lieu/Passeport/CNI wiring; DOB via
  `Né(e) le` was missed and is fixed here in 1.14.1.
- All 3 copies of `structured_ext.py` and `recognizers.py` re-synced (root →
  `plugin/bubble-shield/vendor/` → `plugin/bubble-shield/mcpb/server/vendor/`).
  `scripts/` ↔ `mcpb/server/scripts/` identical. MCPB re-packed.
- All suites green: `test_257_form_layout` 49/49, `test_bubble_shield_mcp` 18/18,
  `test_guard` 21/21, `test_guard_marker` 11/11, `test_tripwire` 18/18,
  `test_posttool_anonymize` 19/19, `test_option_b_e2e` 9/9,
  `test_256_daemon_path_fail_loud` 16/16.

## 1.14.0 — 2026-06-23 (fix #257: FR état-civil FORM layout detection — wiring + Nom/Lieu/Pièce recognizers)

- **Fixed: GLiNER misses PII in FORM-LABEL layouts (board #257 — TOTAL LEAK).**
  In real client DCCs structured as `Nom : DUBOIS / Prénom : Marc / Né(e) le :
  03/05/1980 à : Lyon / Passeport n° 12AB34567`, the GLiNER NER daemon was blind
  to name, prénom, birthplace, and passport because the label-value layout has no
  prose context for NER to anchor on. Even with the daemon armed, those fields leaked.
  **Note: DOB via `Né(e) le` was NOT fixed in this release — see 1.14.1.**
- **Root cause — wiring bug confirmed:** `_engine()` in `bubble_shield_mcp.py`
  and the engine construction in `posttool_anonymize.py::main()` both built the
  engine with only `[daemon_detector]` as `extra_detectors`. `structured_ext`'s
  deterministic form-layout recognizers were NEVER wired in, so they never ran in
  the `bubble_shield_read` / `anonymize_text` / posttool path.
- **Fix 1 — wiring:** `structured_ext.make_structured_detector()` is now the
  FIRST entry in `extra_detectors` in both `bubble_shield_mcp.py::_engine()` and
  `posttool_anonymize.py` main engine build. It runs before the daemon and is
  daemon-independent (fail-open if import fails).
- **Fix 2 — new recognizers in `structured_ext.py`:**
  - `form_nom_matches`: matches `Nom : <VALUE>`, `Prénom : <VALUE>`,
    `Nom de naissance : <VALUE>`, `Nom d'usage : <VALUE>` → **NOM**.
  - `form_lieu_naissance_matches`: matches `Lieu de naissance : <VALUE>` and
    `Né(e) le : <DATE> à : <CITY>` (the "à :" city fragment) → **LIEU_NAISSANCE**.
  - `form_piece_identite_matches`: matches `Passeport n° <ID>`, `CNI n° <ID>`,
    `Pièce d'identité : <ID>`, `Titre de séjour n° <ID>` → **PIECE_IDENTITE**.
    Pattern: ID numbers are bounded to 30 chars starting with uppercase letter or digit.
  - All three are ADD-only, fail-open, recall-first.
- **Regression test `scripts/test_257_form_layout.py`:** 42 assertions covering
  daemon DOWN and daemon UP paths, `bubble_shield_read` file path, explicit lieu
  label, non-PII control text (no over-masking), and direct unit tests of each new
  recognizer. Uses `XANTHIPPE ZORVEC` — a name not in any FR first-name gazetteer —
  to prove the structured recognizer does the work, not the gazetteer.
  **Missing assertion: DOB masking — see 1.14.1.**
- Vendor re-synced. MCPB re-packed; `scripts/` ↔ `mcpb/server/scripts/` identical.
- All existing suites green: `test_bubble_shield_mcp` 18/18, `test_guard` 21/21,
  `test_guard_marker` 11/11, `test_tripwire` 18/18, `test_posttool_anonymize` 19/19,
  `test_option_b_e2e` 9/9, `test_256_daemon_path_fail_loud` 16/16.

## 1.13.1 — 2026-06-23 (dev: bump to align with #256 public release)

- Dev plugin.json version bump to 1.13.1 to align with public repo (no code changes).

## 1.13.0 — 2026-06-23 (MCPB-packaged MCP server — installable as a plugin)

- **Fixed plugin install failure.** The Claude app rejected the plugin with
  "MCP server 'bubble_shield' is a local/stdio server. Plugins may only declare
  remote (http/sse/ws) or MCPB servers." (same rejection caused the opaque GitHub
  `failed_content`). Plugins may not declare a bare stdio MCP server.
- **Converted the stdio server to MCPB.** The local stdio server is now packaged
  as `mcpb/bubble-shield.mcpb` (MCPB manifest v0.4, `server.type=python`) and the
  plugin declares it via `plugin.json` → `"mcpServers": "./mcpb/bubble-shield.mcpb"`.
  The old stdio `plugin/bubble-shield/.mcp.json` was removed (the loader merges it
  too, so leaving it would re-trigger the rejection). The host extracts the bundle
  and launches the wrapper `server/entry.py`, which puts the bundled pure-python
  engine + scripts on `sys.path` and runs the unchanged `bubble_shield_mcp.py`.
- **Bundle is pure-python.** Only the vendored `bubble_shield` engine and `pypdf`
  ship in the MCPB (1.7 MB unpacked). The ML accuracy pack (GLiNER/onnxruntime/
  numpy) stays a lazy, on-demand `bubble_shield_setup_ml` download into a separate
  runtime venv — NOT bundled.
- All 8 tools, fail-closed behaviour, and the vault round-trip verified through the
  packed-and-extracted bundle on synthetic PII. Existing suites stay green.
- **Re-pack on every release** (see RELEASING.md): the `.mcpb` is a built artifact;
  if you change `scripts/` or `vendor/`, re-sync into `mcpb/server/` and re-pack.

## 1.12.0 — 2026-06-23 (verified protection model + client docs)

- **Corrected the documented protection model.** Proven on Claude Code v2.1.186:
  PostToolUse `updatedToolOutput` is ignored by the harness for built-in tools
  (Read *and* Bash), every value shape — not just a Cowork limit
  ([anthropics/claude-code#32105](https://github.com/anthropics/claude-code/issues/32105),
  open). The plugin README now states the guarantee plainly: protection is the
  `PreToolUse` deny/steer + the first-party `bubble_shield_read` MCP tool (whose
  own output the harness *does* honor), NOT a post-hook scrub. The "anonymisé"
  notice is context, never proof of substitution.
- **Verified the Option B path end-to-end.** `bubble_shield_read` returns `⟦…⟧`
  tokens, fails closed on error, the guard denies a built-in Read of a marked
  folder and steers to the MCP tool, vault round-trips. Added
  `scripts/test_option_b_e2e.py` (9 cases). No regressions across the suites.
- **Documented root-marker coverage.** One `.bubble-shield.json` at a folder root
  protects the whole tree (sub-folders inherit); files outside marked folders are
  not covered — now explicit in the README.
- **Client onboarding docs** (non-technical, FR): added a Cowork user tutorial and
  corrected the governance email (both surfaces install the same plugin; the
  difference is config scope, not install-vs-no-install).

## 1.11.1 — 2026-06-14 (finding + honest docs)

- **PROVEN: Cowork does not run PostToolUse on third-party MCP connectors.** A
  live probe (logged every PostToolUse invocation) across 19 real Gmail calls in
  Cowork recorded ZERO firings on the mail connector (only Bash fired). So mail
  containment — correct + fail-safe in unit tests and in plain Claude Code CLI —
  NEVER ENGAGES in Cowork. It is effectively CLI-only.
- Consequence for mail in Cowork (now documented honestly, no overclaim): raw
  mail reaches context; the PreToolUse mail-guard STEER is the only mechanism that
  fires, and it is best-effort (observed: agent complied once, summarised raw mail
  twice). The RELIABLE protection for sensitive mail is to route it through a
  protected folder and read via bubble_shield_read (PreToolUse-enforced). Onboarding
  skill rewritten to say exactly this.
- mail_containment left ON BY DEFAULT (harmless fail-safe; protects in CLI). Probe
  removed. No behaviour change beyond docs + the recorded finding.

## 1.11.0 — 2026-06-14

- **Mail containment is now ON BY DEFAULT.** `mail_containment` defaults true and
  runs INDEPENDENTLY of `posttool_enabled`. Mail PII reaches context the instant
  the connector returns and is high-risk, so protection-on is the honest default
  for a privacy product. The fail-safe (pass-through on any shape mismatch, never
  crashes the connector) is what makes default-on safe despite the
  undocumented-shape dependency. Set `mail_containment:false` to fall back to the
  PreToolUse steer only. Tests: posttool 19/19, guard 21/21.

## 1.10.0 — 2026-06-14

- **Mail containment (opt-in `mail_containment`) — true protection with a safety
  net.** The 1.9.1 allow+steer improved output behaviour but live-tested as
  unreliable: the agent self-censored yet did NOT call bubble_shield_anonymize_text, so
  raw mail still reached context. This adds real containment: a PostToolUse
  handler intercepts a mail connector's result and anonymises its string values
  IN PLACE, PRESERVING the exact data shape (no flat-string clobber → no
  H.reduce), so raw PII never enters context. SAFETY NET: it only rewrites a
  shape it can cleanly reproduce; on ANY mismatch/error it emits nothing
  (pass-through → the 1.9.1 steer still applies), so it can never crash the
  connector — turning the brittle (undocumented Anthropic-owned shapes)
  dependency from silent-brick into silent-degrade. Off by default; enable per
  client. PostToolUse matcher widened to mcp__.* — SAFE because non-mail
  structured results bail via the simple-text safe-gate (verified: notion-style
  result untouched). Tests: posttool 19/19, guard 21/21. NOTE: still depends on
  undocumented connector/hook shapes; the fail-safe is why it's shippable. Needs
  live Cowork confirmation that the preserved shape is accepted (no H.reduce).

## 1.9.1 — 2026-06-14 (fix)

- **Fix: mail-guard no longer creates a catch-22.** 1.9.0 DENIED raw mail reads
  and told the agent to "fetch then anonymise" — but a PreToolUse deny means the
  fetch never runs, so the agent retried the same blocked call forever and mail
  became UNUSABLE (live-caught: the agent correctly refused to route around it).
  The fetch IS the only way to get the mail text to anonymise, so blocking it is
  self-contradictory. Now the guard ALLOWS the mail read but returns
  `additionalContext` (PreToolUse supports allow + context) forcefully instructing
  the model to run the fetched text through `bubble_shield_anonymize_text` before using
  it. Mail flows; strong steer; still honest-scope (a steer, not hard
  containment — raw mail transits the result once). guard 21/21.

## 1.9.0 — 2026-06-14

- **Mail-guard — enforced anonymisation for e-mail (PreToolUse).** Live test
  showed that, given a neutral "read my emails", the agent read raw mail and
  leaked a real e-mail address — it did NOT anonymise on its own. Judgment-based
  protection is unreliable for a privacy product. Now the guard BLOCKS raw mail-
  connector reads (Gmail `search_threads`/`get_thread`/`list_messages`… — detected
  by mail-specific actions since the connector id is an opaque UUID) with a
  forceful instruction to pipe the fetched text through `bubble_shield_anonymize_text`
  first. The guard's PreToolUse matcher widened to `mcp__.*` (safe — the guard
  only emits allow/deny, never updatedToolOutput, so it can't hit the #H.reduce
  rewrite-shape bug). Non-mail mcp tools (incl. our own bubble_shield_read, workspace
  bash, notion) are not caught. Opt-out: `mail_guard:false`; extend detection via
  `mail_tool_patterns`. Tests: guard 21/21 (+7 mail-guard). HONEST SCOPE: this is
  a strong STEER, not the hard containment the folder guard gives — raw mail still
  transits the tool result once before the agent anonymises it (Bubble Shield has no mail
  creds, can't fetch+anonymise mail server-side).

## 1.8.3 — 2026-06-14 (fix)

- **Fix: PostToolUse no longer rewrites arbitrary MCP connector output (the real
  Gmail `H.reduce` cause).** 1.8.2 skipped structured results by shape, but the
  hook still MATCHED `mcp__.*`, and rewriting any MCP connector result breaks the
  Cowork harness — it measures the result as a content-block array, so a rewrite
  throws `H.reduce is not a function` (live-diagnosed: Gmail failed every call in
  Cowork-with-Bubble Shield, worked in a plain chat). The PostToolUse matcher is now
  `Read|Bash|mcp__workspace__bash` only — built-in tools + Cowork's own shell,
  whose text rewrite is proven safe. Arbitrary `mcp__.*` connectors (Gmail etc.)
  are no longer touched; PII in their results goes through the explicit
  `bubble_shield_anonymize_text` / `bubble_shield_read` tools. The 1.8.2 shape-gate stays as a
  second guard. Regression green.

## 1.8.2 — 2026-06-14 (fix)

- **Fix: PostToolUse hook no longer breaks MCP connectors (Gmail).** The hook
  matches `mcp__.*` and rewrote any result containing PII via a flat
  `updatedToolOutput {type:text,text}`. For connectors returning STRUCTURED
  results (e.g. Gmail `{threads:[...]}`) that replaced the array/object shape
  with a string → the connector's own handler threw `H.reduce is not a function`
  on every call (reproduced + root-caused from a live Cowork session; Gmail
  worked in a plain chat with no Bubble Shield). Now the hook only rewrites SIMPLE text
  results (bare string / `{type,text}` / pure text-block list) and leaves any
  structured result UNTOUCHED. PII inside structured MCP results is handled by
  the explicit `bubble_shield_anonymize_text` / `bubble_shield_read` tools, not the ambient
  rewrite. Regression guards added (structured + mixed). posttool 13/13.

## 1.8.1 — 2026-06-14

- **`bubble_shield_enable_global` MCP tool — truly-global "anonymise everywhere", one
  click, no Terminal.** Sets `posttool_enabled` in the host config
  (~/.config/bubble_shield/bubble-shield.json) from inside Cowork — the host-side MCP
  server reaches the path the agent's VM shell can't. on/off/status; MERGES into
  the existing config (preserves protected_folders etc., never clobbers). Closes
  the last "needs you / needs Terminal" gap for machine-wide coverage. Tests:
  bubble_shield_mcp 18/18 (incl. merge-preserves-keys).

## 1.8.0 — 2026-06-14

- **Three new MCP tools — "PII from anywhere" + write-back, no Terminal.**
  - `bubble_shield_anonymize_text(text)` — anonymise any text that isn't a file (an
    e-mail body, a message, an API result). The mail path: fetch → anonymise the
    body → reason over tokens. Closes the gap that `bubble_shield_read` (files only)
    left.
  - `bubble_shield_write(path, content)` — the write-back direction: the agent drafts a
    document using ⟦…⟧ tokens, calls this with the output path; Bubble Shield restores
    the real values LOCALLY and writes the file, returning only a success line —
    NEVER the de-anonymised content. So a finished client document with real PII
    is produced without the agent ever seeing the real values. Fail-closed
    (errors if no vault; never writes raw on failure).
  - `bubble_shield_setup_ml(action=start|status)` — installs/checks the ML accuracy pack
    from INSIDE Cowork with no Terminal: the host-side MCP server spawns the
    bootstrap detached and reports progress, since the agent's own shell is
    VM-only. start is idempotent (no reinstall if present).
- Onboarding skill updated: the four tools, the no-Terminal ML setup flow, and
  the read-in → tokens → write-out workflow.
- Tests: bubble_shield_mcp 12/12 (incl. write hides real PII from the response while the
  file gets it; fail-closed without a vault); guard 14/14, marker 11/11,
  tripwire 18/18, posttool 11/11, extract OK. plugin validate ✔.

## 1.7.0 — 2026-06-14

- **`bubble_shield_read` MCP tool — the Cowork workaround for ambient anonymisation.**
  Live testing showed Cowork RUNS our PostToolUse hook but ignores
  `updatedToolOutput` for built-in tools (Read/Bash) — output rewrite only takes
  effect for MCP tools (anthropics/claude-code#32105). So the v1.6 ambient hook
  can't cloak a built-in Read in Cowork.
  - New pure-stdlib stdio MCP server (`.mcp.json` → `scripts/bubble_shield_mcp.py`)
    exposing `bubble_shield_read(path)`: the agent reads client files THROUGH it and the
    tool's OWN returned content is already anonymised (⟦…⟧), so no rewrite is
    needed — output is controlled at the source, which Cowork honours for MCP.
  - Reuses the engine + extractor + policy panel + warm NER daemon + session
    vault (reversible, consistent tokens with the folder path). FAIL-CLOSED:
    returns an error, never raw text (opposite of the ambient hook's fail-open).
  - The folder guard still blocks the bare Read of protected files, steering the
    agent to `bubble_shield_read`.
  - Verified locally (CLI --plugin-dir): agent discovers + calls the tool and
    receives cloaked name/IBAN/email. Cowork-surfacing is the next live test.

## 1.6.0 — 2026-06-14

- **ML accuracy pack — "protect PII anywhere" (opt-in, off by default).** A new
  PostToolUse hook anonymises sensitive data in ANY tool result before Claude
  sees it (a fetched email, a script's stdout, an opened Excel) — not just inside
  marked folders. On-device GLiNER NER via a warm localhost daemon (~40ms warm),
  ONNX runtime (~71MB, no PyTorch), nothing leaves the machine.
  - `bubble_shield_setup_ml.py` — one-time day-one bootstrap: persistent venv + model
    download + a login LaunchAgent so the daemon is always warm.
  - `bubble_shield_nerd.py` — warm NER daemon (127.0.0.1 only, idle-shutdown).
  - `posttool_anonymize.py` — the hook: regex core + daemon NER, opt-in
    (`posttool_enabled`), FAIL-OPEN, PII-presence gate, honours the policy panel
    + session vault (reversible, consistent tokens with the folder path).
  - Self-installer + hooks.json register PostToolUse; cowork-only gate unchanged.
  - onboarding skill: plain-FR "protect everywhere" branch + accuracy-pack.md.
- Limit (by design): a doc pasted/dragged straight into the chat is in context
  before any hook — cannot be auto-anonymised. Onboarding warns about this; it is
  the user's responsibility. The fail-closed guarantee stays the folder guard.

## 1.5.0 — 2026-06-14

- **Cowork-only gate on the self-installer — no more host-Mac spill.** The
  SessionStart `install_user_hooks.py` previously wrote the PreToolUse guard +
  UserPromptSubmit tripwire into `$HOME/.claude/settings.json` unconditionally.
  On a real Mac that shared file is read by every CLI session, cron, and the
  Desktop app — so the guard spilled off-Cowork and could block unrelated
  Bash/Read calls (it broke a scheduled task on 2026-06-07). It also survived a
  plugin uninstall, so it kept firing after removal.
- The installer now runs ONLY inside the Cowork sandbox VM, detected by
  `HOME` starting with `/sessions/` (the confirmed Cowork VM home), or
  `CLAUDE_CODE_IS_COWORK=1`, or `CLAUDE_CODE_ENTRYPOINT=local-agent`. On the host
  Mac none of these hold → the installer no-ops and writes NOTHING (not even the
  stable script dir). Verified: live Cowork probe (HOME=/sessions/<name>) +
  anthropics/claude-code#40495. Fail-safe direction: if Cowork can't be
  positively confirmed, it does not install.
- Guard enforcement itself is unchanged and still fires in Cowork (live-verified
  2026-06-14: a marked Dropbox folder blocked a raw Read with the 🔒 message).

## 1.4.0 — 2026-06-03

- **Visual tool for Cowork — the before/after as an artifact.** The local webapp
  (FastAPI on 127.0.0.1) can't run in Cowork's sandbox (its localhost is the VM,
  not the user's Mac; FastAPI deps aren't vendorable). Added
  `scripts/make_artifact.py`: runs the same engine and emits one self-contained
  HTML file (inline CSS, identical view + styling to the webapp) with the
  highlighted before/after, the verdict, and the masquer/conserver toggle table.
  The anonymise + onboarding skills now present this artifact as the visual tool.
  Pure-stdlib + vendored engine — runs in Cowork, zero install.
- Onboarding no longer sends Cowork users to the dead-end webapp; the standalone
  webapp is reframed as a power-user/own-machine tool.

## 1.3.0 — 2026-06-03

- **Cowork enforcement fix.** In Cowork the agent runs in a VM spawned with
  `--setting-sources=user`, so plugin-bundled hooks (`hooks/hooks.json`) are
  silently ignored — only the VM's user `settings.json` is honoured (Anthropic
  issue #16288). Added a **SessionStart** hook (`scripts/install_user_hooks.py`)
  which DOES fire from a plugin and writes the guard (PreToolUse) + tripwire
  (UserPromptSubmit) into the VM's `~/.claude/settings.json` at session start.
  Idempotent; preserves other hooks; harmless no-op on the CLI.
- The guard now also matches Cowork's shell tool `mcp__workspace__bash` (not just
  `Bash`), and reads the command from `command`/`script`/`code`.

## 1.2.1 — 2026-06-02

- **Encrypted vaults now work with zero install too.** Re-implemented vault
  encryption in pure Python stdlib (PBKDF2-HMAC-SHA256 key derivation + an
  HMAC-SHA256 counter-mode cipher + encrypt-then-MAC authentication), dropping
  the `cryptography` dependency. So saving/loading a passphrase-protected vault
  runs on any Mac's built-in python3 — no `pip install`, fully offline. Wrong
  passphrase or a tampered file fails loudly (constant-time MAC check). Legacy
  v1 (scrypt+Fernet) vault files still load when `cryptography` is present.

## 1.2.0 — 2026-06-02

- **Fully self-contained — works as a complete product with zero install.** The
  plugin now bundles its dependencies under `vendor/` (the Bubble Shield engine + a
  pure-python `pypdf`), so the anonymiser runs from a GitHub install or a Cowork
  zip with **no `pip install`, no engine on the user's machine, no network**.
  Same approach as Bubble Sentinel.
- `.docx` is now read with the Python standard library (zipfile + ElementTree) —
  no `python-docx`/`lxml` needed. PDF via the vendored pypdf. Plain text native.
- Scripts + the `bubble-shield-anonymize` skill load the bundled engine via
  `${CLAUDE_PLUGIN_ROOT}/vendor`. The firm-identity config is never vendored.

## 1.1.0 — 2026-06-02

- **In-folder marker protection (Cowork-native).** Drop a `.bubble-shield.json`
  inside a folder to protect it + its subtree; the guard walks up from each
  accessed file to the nearest marker. Works inside Cowork's sandbox (which can't
  write to `~/.config`). Opt-in: only marked folders are guarded.
- **Chat-box tripwire** (`UserPromptSubmit` hook): nudges/blocks when raw PII is
  pasted or a document is uploaded directly in the conversation.
- **Client identity removed from source.** The firm allowlist now loads from a
  gitignored deployment config; source ships only generic public third parties.
- Onboarding skill + docs rewritten to the Cowork flow (request the client
  folder, write the marker into it — no Terminal, no `~/.config`).

## 1.0.0 — 2026-06-01

- Initial release: fail-closed `PreToolUse` guard blocking reads of
  `protected_folders` (global config) + bundled `bubble-shield-anonymize` and
  `bubble-shield-onboarding` skills.
