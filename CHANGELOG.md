# Changelog

## 1.23.0 — FEATURE: shadow-index runtime redesign

Reworks the read path from "anonymise-on-every-read" to a **shadow index**: a pre-computed store of already-anonymised document versions, so the hot path carries no ML cost and the heavy masking runs out-of-band.

- **feat(runtime) — zero-model read path.** `bubble_shield_read` → `_read_with_shadow` is now ML-free: it computes the file's content hash, and on a shadow HIT serves the pre-anonymised shadow directly (no GLiNER, no Gemma, no daemon). On a MISS it serves the raw *extracted* text once and QUEUES the file (`shadow_store.mark_pending`) for the background sweep — the read never blocks on model inference. The masking guarantee moves off the read path onto the sweep.
- **feat(runtime) — background sweep does the heavy masking.** A launchd-scheduled sweep (`bubble_shield_sweep.py` → `shadow_index.run_sweep`) walks each protected root, runs the FULL anonymisation pipeline (GLiNER + Gemma structured-form second pass, all layers ON — the same `_anonymise_file` path the old read used) on new/changed files, and writes the resulting shadow into the store. Singleton-locked and resumable (a `pending` table + content-hash keys make rename-free and edit-reindexed, so a stale shadow is impossible).
- **feat(store) — one encrypted SQLite shadow store.** Shadows + the harvested gazetteer live in a single SQLite database, encrypted at rest with a machine-local passphrase (`shadow_store`). The store fails **closed** if the passphrase is unavailable (refuse-plaintext), never writing shadows in the clear. Store location is config-indirected via `BUBBLE_SHIELD_HOME` (v2-shared layout).
- **feat(gazetteer) — read-time net.** Confirmed PII names harvested by the engine during the sweep populate the gazetteer, which a lightweight read-time pass consults so a name already known to the store is masked even on the zero-model read path.
- **change(mail) — mail path disabled, kept in reserve.** The mail tools (`bubble_shield_mail_read` + its mutation counterpart) are no longer registered in the default tool surface; they are gated behind `BUBBLE_SHIELD_ENABLE_MAIL=1` and thus invisible/unreachable via `tools/list` in a shipped build. The code is retained in reserve, not deleted.
- **fix(venv) — venvs standardised on Python 3.12.** The ML / Gemma / OCR runtime venvs are pinned to Python 3.12 (not the launching interpreter), fixing a wrong-ABI install failure on bare client Macs.
- **fix(gemmad) — dedicated MLX worker thread.** The offline Gemma daemon now runs MLX inference on one dedicated worker thread (`InferenceWorker`), stopping an all-NOM degradation seen when MLX was driven concurrently under `ThreadingHTTPServer`.

Fail-closed containment from 1.22.x is preserved end-to-end: on the read path a shadow MISS never fabricates a masked body, and the sweep's masking runs the same fail-closed envelope as before.

## 1.22.4 — SECURITY (P0 #589-B)

Closes a leak class that survived 1.22.3: a degraded French tax form (liasse fiscale) masked ~114 entities (`verdict_state == "masked_ok"`) but still returned several clear entity classes (a SIRET written digit-by-digit, a forename left clear after a masked surname, an associate's date + place of birth, the accounting firm's name/address/phone, a siège postal code + city). The 1.22.3 text-quality gate only runs on `zero_detection`, never on `masked_ok`, so a degraded form that masks *some* entities bypassed it entirely — carrying everything the columnar extraction hid from the fast regex/GLiNER pass.

- **fix(security) (#589-B, P0) — structured-form → Gemma second-pass masker.** `_anonymise_text` now detects the document CLASS (liasse / CERFA / bilan) via form-number fingerprints + fiscal label markers (`_is_structured_form`). On a structured form, it escalates to a local Gemma second pass (`/extract_pii` on the offline Gemma daemon) that finds the entities the fast pass missed and masks them into the SAME reversible vault. The escalation is **FAIL-CLOSED**: `StructuredFormUnverifiedError` → `isError:true`, NO body, on any Gemma failure (daemon down / timeout / non-200 / `ok:false` / malformed JSON) OR when the second pass applied ZERO masking to a triggered form (empty, non-matching, or stale spans) at any length — the tool escalated *because* the fast pass is untrusted on this doc, so a failed escalation never falls back to it. The trigger is glue-tolerant (degraded extraction that fuses tokens — the incident's failure mode — still fingerprints) and requires ≥3 distinct markers so ordinary prose never escalates. **Clean / non-form documents are byte-identical to before** (no Gemma cost, no behavior change). The user is warned up front that a structured form takes longer to process. **Known residual:** Gemma's own recall is not perfect — on a triggered form where Gemma masks most entities but misses one, that one still returns (the envelope can only fail closed on *zero* applied masking); this narrows but does not eliminate the recall gap inherent to any second-pass masker.

## 1.22.3 — SECURITY (P0 #589)

Closes a real client-PII leak: in a live session `bubble_shield_read` returned two RAW documents (a 43KB liasse-fiscale PDF + a 10KB .docx) with ZERO masking tokens and `isError:false` — the raw PII went straight into the model's context — because `_anonymise_text` failed closed ONLY when the NER daemon was down, and had two other fail-open paths. The fix closes them with three layers, all guaranteeing: **a body is returned only when masking provably COMPLETED on text the recognizers could actually read.**

- **fix(security) (#589, P0) — layer 1, zero-detection base:** when the engine finds ZERO detections on a SUBSTANTIAL document (`verdict_state == "zero_detection"`), `_anonymise_text` no longer returns `res.anonymized` (which on a zero-detection result IS the raw input) decorated with a soft "please review" note. A note is not containment. Now raises `ZeroDetectionError` → `isError:true`, FIXED French refusal message, NO body. Gated on engine.py's `substantial_text` boundary (>=8 words AND >=40 chars) so a genuinely tiny/empty input (`nothing_to_do`) is never refused.

- **fix(security) (#589, P0) — layer 2, masking-incomplete tripwire:** a read now returns a text body ONLY if it holds a VALID completed `AnonymizationResult` (a computed `verdict_state`). Any path where `engine.anonymize()` did not run to completion — swallowed exception, daemon reported up but returning malformed/empty, any early-return that skips masking — raises `MaskingIncompleteError` → `isError:true`, NO body. This closes the exact class that leaked the client's data (raw text returned with no completed masking run). The daemon-down `NERDownError` path is unchanged.

- **fix(security) (#589, P0) — layer 3, text-quality gate:** refines layer 1 so it no longer blindly refuses every clean document. On a substantial `zero_detection`, a text-quality score (`real_word_ratio`, `avg_word_len`, `nonword_pct`) distinguishes genuinely-clean prose from garbled/degraded extraction (scanned/OCR PDFs — the actual incident shape). CLEAN prose (the recognizers had real text and confidently found nothing) → RETURNS, with the mandatory `relecture humaine requise` note. GARBLED / low-quality extraction → HARD FAIL-CLOSED (`ZeroDetectionError`, no body). Named, tunable constants (`_QUALITY_MIN_REAL_WORD_RATIO = 0.40`, `_QUALITY_MIN_AVG_WORD_LEN = 3.5`, `_QUALITY_MAX_NONWORD_PCT = 8.0`), calibrated with wide margin (clean 0.84/6.4/0.7 vs garbage 0.08/2.4/13.3).

**Net behavior:** the ONLY documents refused are (a) masking didn't complete, or (b) the extracted text was garbage the recognizers couldn't read. Every genuinely-clean document — long or short — still returns (with a human-review note when nothing identifying was found). The leak/low_confidence review-note cases and normal masking are unchanged. Residual risk is the pre-existing NER recall gap (a name the recognizers miss in otherwise-clean prose returns unmasked with the review note) — not widened by this fix; thresholds warrant a real-CGP-doc sanity pass as a fast follow.

## 1.22.2

- fix(installer): `install-app.sh` no longer blindly reuses an existing `.venv` on update. It now compares the existing venv's Python ABI (major.minor) against the freshly-selected interpreter and rebuilds the venv (`rm -rf .venv`, French log message) on mismatch — fixes a live bug where a client whose `.venv` was built against a wrong-ABI Python (e.g. Homebrew python3.12 shadowing stock python3.9 on PATH, the #396b case) hit `ResolutionImpossible / Cannot install pywebview==3.4` on every subsequent update, because the cp39-only offline wheels can't install into a 3.12 venv holding old unpinned deps. A matching-ABI venv is still reused untouched (fast path); only the app's own `.venv` is ever deleted, never the app dir or user data.
- fix(launcher): the generated `.app` launcher (`make_app_bundle.sh`) now detects the real hardware arch via `sysctl -n hw.optional.arm64` (never `uname -m`, which lies under Rosetta translation) and re-execs itself under that arch — plus forces the same arch on the `python -m launcher` exec — before running. Fixes an intermittent live client crash ("Bubble Shield — Erreur de démarrage" / `incompatible architecture (have 'arm64', need 'x86_64')`) that occurred whenever Finder/LaunchServices happened to launch the `.app` under Rosetta, causing the universal venv Python to load its x86_64 slice against arm64-only compiled wheels (`pydantic_core`). Verified live on Apple Silicon under both a Rosetta-launch context and a native-arch launch context.
- fix(dashboard) (#587): the risk-control dashboard no longer counts `vault_reveal` (document RESTORE) events as anonymisation runs. `summarize()` now scopes `runs`/`unsafe_runs`/the "anonymisations" headline to `event == "anonymize"` only; restores are surfaced separately and honestly as `reveal_runs` ("restaurations"), never merged into the risk numbers. Fixes a live false "35 à relire of 38" (should have read "0 à relire of 3") caused by fail-closed `_is_unsafe()` flagging every reveal (no `safe_to_send` key) as residual-PII risk. Relabeled the "à relire" stat and the detail-table verdict badge to distinguish restores from genuine risk flags.

## 1.22.1

- fix(list): `bubble_shield_list` returns folder/file NAMES in clear (they are navigation labels the user owns and already sees on their machine) — masking them broke the user's ability to tell the agent which client folder to open. File CONTENTS stay fully masked (`bubble_shield_read` unchanged). The listing no longer depends on the NER daemon.
- docs: two-phase architecture roadmap in PRODUCT-REFERENCE §7.1 (tool-layer masking now / egress-proxy after Cowork→Claude Code migration).


## 1.22.0

### Added
- **Gazetteer de-pollution pipeline (#568).** The always-mask gazetteer
  self-pollutes (over-masked common words get seeded in as permanent
  false-positive entries); a new triage→Gemma-classify→remove-PII→audit-log
  pipeline (`depollute.py` + `gemma_classifier.py`, backed by a warm local
  Gemma daemon `bubble_shield_gemmad.py`) can now filter and self-correct the
  gazetteer, on demand ("Clean now" button + audit view in the review queue)
  or automatically (async trigger right after a new value is seeded).
  Fail-safe by design: any classification ambiguity defaults to keep-masking.

### Fixed
- **`add_candidate` honors the caller's `gaz_path`** instead of silently
  defaulting to the main gazetteer — root cause of a latent audit-log gap
  affecting any caller (including de-pollution itself) operating on a
  non-default gazetteer.
- **MCPB bundle mirror re-synced.** The shipped `.mcpb` server copy had
  fallen behind the plugin copy on `known_pii_store.py` / `review_queue.py`
  (including the `gaz_path` fix above) and was missing the new de-pollution
  files entirely; both trees are byte-identical again
  (`tests/test_mirror_copies_identical.py`).

## 1.21.5

### Added
- **Correct a triage — remove or change a label, un-archive a mail.** If the client
  points out a mistake or changes their mind, the assistant can now fix a mail without
  re-doing everything: `bubble_shield_mail_apply` accepts `remove_labels` (drop a wrong
  tag), a change-category flow (remove the old label + add the right one in one
  decision), and `unarchive` (bring an archived mail back into the inbox). Removing a
  label only un-tags — it never deletes the message, and nothing is ever sent.

### Fixed
- **Emoji variation selector stripped so add/remove stay symmetric.** Gmail strips the
  U+FE0F variation selector from labels like `🏗️`/`↪️`/`✍️` on storage; Bubble Shield now
  strips it before encoding too, so a later label removal matches what Gmail actually
  stored instead of relying on lenient server matching.

## 1.21.4

### Fixed
- **Mail triage can now apply emoji labels and prepare drafts on any-locale Gmail.**
  Two bugs surfaced by a live triage test, both fixed and verified against a real
  account:
  - **Emoji/accented label names no longer crash the apply.** Applying a label like
    `🔴 Clients` or `Système` failed with `UnicodeEncodeError` because IMAP label
    names carrying non-ASCII must be modified-UTF-7 encoded; the label argument is
    now encoded correctly (`🔴 Clients` → `&2D3dNA- Clients`).
  - **Draft creation works on non-English Gmail.** The drafts folder was hardcoded to
    the English `[Gmail]/Drafts`; a French account uses `[Gmail]/Brouillons`, so
    drafts failed with "folder doesn't exist". Bubble Shield now discovers the drafts
    folder via its IMAP `\Drafts` special-use flag (locale-proof), with a fallback.

## 1.21.3

### Fixed
- **A failed mail-triage action now says WHY (the exception type), so it's
  diagnosable.** `bubble_shield_mail_apply` used to report only `1 échec (UID: n)`
  with no reason — the cause was redacted to avoid leaking PII, but that made a real
  failure impossible to diagnose from the assistant side. It now surfaces the
  exception **type name** (a class identifier like `MailConfigError`, `IMAP4.error`,
  `PermissionError` — never PII, never the message/args) in the returned summary and
  stderr, e.g. `1 échec (UID:type — 3785:MailConfigError)`. The restored draft body
  and any exception message are still never surfaced.

## 1.21.2

### Fixed
- **Mail triage can now actually apply its decisions.** `bubble_shield_mail_apply`
  targets each message by UID, but `bubble_shield_mail_read` did not return one — and
  read used unstable sequence numbers while apply used UIDs, a mismatched identifier
  space. So an assistant could classify mail but could not label/archive/draft without
  guessing an identifier (risking action on the wrong message), and correctly refused.
  Read now uses UID SEARCH/FETCH and starts each message block with a `UID:` line (a
  mailbox integer, not PII — never anonymised) that the assistant passes straight to
  apply. The read→apply hand-off is verified byte-identical. Also added
  `bubble_shield_mail.py` to the plugin↔bundle mirror-integrity tripwire.

## 1.21.1

### Fixed
- **NER daemon no longer drops after a plugin update (#561, the real fix).** The
  LaunchAgent that keeps the detection daemon warm was pointed at the daemon script
  inside the ephemeral per-session plugin cache. Cowork garbage-collects that cache on
  every plugin update, so after each update launchd tried to start a deleted file, crash-
  looped, and the daemon went "down" — making reads fail-closed until a session lazily
  respawned it. The daemon is now installed to a STABLE location (`~/.bubble_shield/
  daemon/`) and the LaunchAgent points there, so a plugin update can no longer orphan it.
  A re-run of the ML setup refreshes the stable copy to the new code. Only the daemon +
  its setup script are copied (guard/hooks/tests are explicitly excluded).

## 1.21.0

### Added
- **Mail triage — trie une boîte Gmail entière sans jamais voir de PII.** Nouveau skill
  `bubble-shield-mail-triage` + nouvel outil `bubble_shield_mail_apply`. L'assistant lit
  chaque mail anonymisé (`bubble_shield_mail_read`), le classe dans une taxonomie 5 niveaux
  (Clients / Important / Newsletters / Structurés / CV / Transition), puis pose les libellés
  Gmail, archive, et prépare des brouillons de réponse — via un chemin d'écriture IMAP
  host-side, donc utilisable dans une tâche planifiée sans validation manuelle. La liste des
  clients est lue anonymisée depuis le dossier protégé et matchée jeton-à-jeton, donc elle
  reste à jour à chaque export sans changer le code.

### Security
- **Chemin d'écriture mail à garanties structurelles.** `bubble_shield_mail_apply` ne peut
  PAS envoyer (aucun SMTP — uniquement APPEND vers les brouillons), ne peut PAS supprimer
  (aucun `\Deleted`/expunge/Trash/Spam — archiver = retirer `\Inbox`), est plafonné à 60
  mutations par passage, et journalise chaque action (chmod 600, sans noms de libellés custom
  potentiellement PII). Les brouillons sont restaurés en mémoire via le vault : le vrai nom va
  dans le brouillon Gmail, jamais dans le contexte du modèle ni sur disque. Un brouillon dont
  un jeton reste non résolu est SAUTÉ plutôt qu'envoyé avec des marqueurs visibles.
- **stderr redigé** pour les deux outils qui restaurent du PII (`write` + `mail_apply`) : le
  type d'exception est loggé, jamais le message brut, pour qu'une erreur de librairie ne puisse
  pas déposer du PII restauré dans un log host persistant.

## 1.20.6

### Fixed
- **NER daemon stays warm across a working session.** The idle-shutdown timeout
  was 15 minutes, but the idle exit is clean (exit 0) and the LaunchAgent only
  auto-restarts on a failure exit — so the daemon dropped mid-session and reads
  refused (fail-closed) until a ~20-37s cold re-spawn. The idle timeout is now 4
  hours (set `BUBBLE_SHIELD_NERD_IDLE=0` for an always-warm install), so a normal
  working session no longer hits intermittent NER-down refusals.

## 1.20.5

### Fixed
- **`bubble_shield_list` no longer self-blocks.** The sanctioned folder-listing
  tool (added in 1.20.2) was not in the guard's own-tool allow-list, so the guard
  denied it on every protected folder — making file discovery impossible and
  forcing the operator to paste paths by hand. It is now exempted like
  `bubble_shield_read`/`write` (it returns masked filenames, fail-closed if NER is
  down). The exemption stays narrow — non-sanctioned MCP file tools remain blocked.

## 1.20.4

Security hardening of the Cowork sandbox-mount guard, surfaced by a live red-team.

### Security
- **`cd`-compound mount-alias bypass closed (#553).** A shell command that does its
  own `cd` into a sandbox mount and then reads via a relative path
  (`cd .../mnt/outputs && cat "../Dropbox/clients/x"`) bypassed the guard, because
  the guard saw the session-root cwd, not the post-`cd` directory. The guard now
  resolves the effective cwd from `cd` chains before classifying.
- **Whole cwd-hiding class fail-closed (#553-B/C/D).** Constructs that hide the
  effective directory from the guard — subshells `(...)`, `bash -c`, `pushd`,
  `eval`, `cd $VAR`/`cd $(...)`, opaque `eval "$(...)"`, and mount paths assembled
  from simple shell variables — now fail closed when combined with a read that
  could reach a protected mount. Infra mounts (`outputs`/`uploads`/`.claude`/
  `.remote-plugins`) and non-mount work are unaffected.

### Fixed
- **No more spurious "internal error" blocks.** A tool event with an unexpected
  shape (command passed as a list, cwd as a number) made the guard fail closed
  with a scary "internal error" on a perfectly legitimate command. The guard now
  normalizes these shapes and reaches a correct decision instead of crashing.

## 1.20.3

Security + feature release. Fixes surfaced by a live red-team pass against the
guard, plus a new client-facing gazetteer tool.

### Security
- **Relative `..`-traversal exfil closed (#19).** A path that reached a protected
  file by walking up out of a sandbox mount alias (e.g. `…/mnt/<X>/../../secret`)
  slipped past the marker walk-up. The guard now resolves and re-checks traversal
  tokens fail-closed so the escape hatch is gone.
- **Bare-name symlink bypass closed (#20).** A symlink named as a bare word (no
  path separators) pointing at a protected file let a read dodge the guard. The
  guard now extracts and resolves bare-word tokens before deciding, so a symlink
  target inside a protected folder is blocked like the real path.
- **Write-back can no longer read PII back into context (#40).** `bubble_shield_write`
  now refuses any target that is not itself guarded or explicitly allow-listed, so
  a restored in-clear file can't be written to an unguarded path and then re-read
  by the agent.
- **Guard block-decision is now single-sourced (#40 structural).** The write gate
  and the read guard both call one shared `decide_block_for_path` decision, so the
  two can no longer drift apart as the code evolves — a whole class of "the write
  path allowed what the read path blocked" bugs is designed out.

### Added
- **Cowork human-viewer tools exempted.** The viewer tools that render a file
  straight to the human (`present_files`, `create_artifact`, `update_artifact`)
  are exempted from the protected-path block, so the agent can show a restored
  file to the operator without the raw body ever entering the model's context.
- **`bubble_shield_add_known_pii` tool.** Lets a client flag a word the anonymiser
  missed and add it to the always-mask gazetteer, so that term is masked on every
  subsequent document without a code change.

## 1.20.1

- **P0 SECURITY — Cowork sandbox-mount-alias Bash exfil closed.** In a Cowork
  session the guard runs host-side and matches real Mac paths, but the sandbox
  mounts each connected folder under a dynamic alias `/sessions/<name>/mnt/<X>/…`.
  A shell command using that alias path bypassed the marker walk-up and could read
  a protected file in clear. The guard now DENIES any `/sessions/*/mnt/<X>` token
  fail-closed (except the known infra mounts `outputs`/`uploads`/`.claude`/
  `.remote-plugins`), regardless of config/marker state — because the host cannot
  see a marker on the sandbox filesystem.
- **Manifest completeness** — the MCPB manifest now advertises `bubble_shield_mail_read`
  and `bubble_shield_status` (both were served but not listed).
- **Onboarding demo** — the first-run demo now runs a real, client-chosen task on a
  real file, showing the anonymized result in-session and the in-clear result written
  locally to disk (never re-entering the session).

## 1.18.20

> Assumes PR #24 (1.18.19, `verdict_state` honesty fix) merges first. If #24
> does not land before this, the version needs reconciling (this would become
> 1.18.19).

### Fixed
- **Recall LEAK 1 — hyphen/dot-grouped IBAN was not detected.** The IBAN
  recognizer's separator class only allowed a space between groups, so real forms
  written `FR76-1027-…` or `FR76.1027.…` never matched the regex and leaked in the
  daemon-DOWN (regex-only) config while still being certified "safe to send".
  Extended the separator class to accept space, `-` or `.` between groups, and the
  mod-97 validator now strips the same separators before checksum validation. All
  three groupings (spaced / unspaced / hyphen / dot) now match and mod-97-validate
  identically. No change to spaced/unspaced behaviour (regression-tested).
- **Recall LEAK 2 — bare Title-case "Prénom Nom" mid-sentence leaked.** In running
  prose with no structured label (`Titulaire:` / `Nom:`), a bare Title-case name
  such as "Frédérique Marchand" leaked because GLiNER scores that span *below* the
  0.30 accept threshold (measured 0.21) and the forename gazetteer — the anchor
  that catches untitled `Prénom Nom` — was missing the forename. Root cause was
  investigated by running the real GLiNER detector on the exact span and inspecting
  raw scores (0.05–0.21 mid-sentence vs 0.74 in isolation), confirming the signal
  is genuinely below threshold and that lowering the global threshold would tank
  precision. Fix: expand the forename gazetteer (`gazetteer.py`) with the missing
  common French forenames. This anchors strictly on the forename list — the
  untitled-NOM recognizer only fires when the FIRST token is a known forename — so
  precision on ordinary capitalized French terms ("Plan Épargne Retraite",
  "Assurance Vie", "Crédit Agricole") is unchanged (measured 0 false positives on a
  20-sample financial-prose corpus). ALL-CAPS and labeled forms were already caught;
  this closes the bare Title-case gap without touching the detection thresholds.

## 1.18.17

### Fixed
- **Installer online fallback pulled unpinned dependencies.** `install-app.sh`
  has two dependency-install paths: the default offline path (vendored wheels in
  `vendor/wheels/`, versions pinned by the exact wheel files) and an online
  fallback taken on the rare Mac with no ABI-matching interpreter. The online
  fallback ran `pip install fastapi uvicorn pywebview jinja2 pypdf
  python-multipart` with no version constraints, so it fetched the LATEST
  releases from PyPI — e.g. pywebview 6.x against launcher code written and
  tested for pywebview 3.4. That is the same pywebview 3.x/4.x API-drift crash
  class (`window.events` AttributeError) fixed in 1.18.16; the unpinned fallback
  simply relocated it. Added `constraints.txt` at the repo root pinning the exact
  versions vendored in `vendor/wheels/`, and applied `-c constraints.txt` to
  BOTH the offline and online pip installs so they resolve to identical versions.
  pywebview now stays 3.4 on every install path.
