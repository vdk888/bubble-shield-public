<div align="center">

# ⟦ Bubble Shield ⟧

**Local, reversible PII pseudonymisation for LLM workflows.**

Put your sensitive data in the vault before you talk to a model — pseudonymise on
the way *in*, restore the answer on the way *out*, and the real values
never leave the machine. (Reversible pseudonymisation, a GDPR art. 32 security
measure — not irreversible "anonymisation" in the RGPD sense; the vault keeps it
reversible and stays local.)

</div>

---

Bubble Shield replaces identifying data (names, IBANs, e-mails, tax IDs, amounts…)
with **reversible tokens** like `⟦NOM_0001⟧`, shows you a before/after diff, and
**refuses to certify a document as safe-to-send while any PII still survives**
(fail-closed). The mapping between tokens and real values lives in a local
*vault* that is never part of what you send to an LLM.

It specialises in **French / finance** documents (the entity set and the demo
are FR-first) but the engine is generic and the entity list is easy to extend.

> Built deliberately *on top of* existing prior art (Presidio, PII-Shield,
> DontFeedTheAI, anonLLM) rather than reinventing it — see
> **[PRIOR_ART.md](PRIOR_ART.md)** for exactly what is borrowed and where Bubble Shield
> diverges.

## Why it's safe by design

- **100 % local.** The core is pure-stdlib Python — no network, no API calls,
  no telemetry. The demo webapp binds `127.0.0.1`.
- **Reversible without a parallel database.** Opaque math-bracket sentinels
  `⟦TYPE_NNNN⟧` survive copy/paste and LLM reformatting, and restore by exact
  replace. The vault is the only place real values live once a doc is cloaked.
- **Fail-closed.** Bubble Shield re-scans its own output; if a PII-shaped string
  survives, or a detection landed below the confidence threshold, the document
  is flagged **unsafe to send**. A missed PII is the real risk, so recall is
  what the bench optimises.
- **Layered, optional ML.** A zero-dependency regex + checksum core (IBAN
  mod-97, ISIN/SIREN Luhn) that runs anywhere, plus two **optional** layers that
  *only ever add recall* and fail open to the core when their backend is absent.
- **Encryption at rest (art. 32).** The vault concentrates all the mission's PII,
  so it is the highest-value file on the machine. `Vault.save_encrypted()` /
  `load_encrypted()` give authenticated, pure-stdlib encryption (PBKDF2 + HMAC-CTR,
  encrypt-then-MAC — no `pip install`). The default `save()` still writes chmod-600
  cleartext, so it now **warns loudly** when it does, and a one-command migration
  encrypts existing vaults in place:
  `python3 -m bubble_shield.vault encrypt <vault-dir>` (`status` to audit first).
  *Encrypt-by-default with machine-local key management is the tracked follow-up.*

## Detection layers

| # | Layer | Backend | Default | Covers |
|---|-------|---------|---------|--------|
| 1 | Regex + checksums | pure stdlib | **on** | IBAN, ISIN, SIRET/SIREN, e-mail, NIR/sécu, n° fiscal, FR phone, amounts, dates, titled names |
| 2 | NER | Microsoft Presidio + spaCy | off | person names, locations in prose |
| 3 | Local LLM | Ollama (your machine) | off | names, orgs, places the regex misses |

## Mail triage

Bubble Shield can triage a whole Gmail inbox — several times a day, or on a
morning scheduled task — **without the assistant ever seeing real client PII.**
Each message is read *already pseudonymised* (`⟦tokens⟧`), classified into a
5-tier taxonomy (**Clients / Important / Newsletters / Structurés / CV /
Transition**), then labelled and archived; a reply/transfer *draft* can be
prepared in the user's voice. Shipped as the `bubble-shield-mail-triage` skill
plus two MCP tools.

**Two tools, one host-side write path:**

| Tool | What it does | What the model sees |
|---|---|---|
| `bubble_shield_mail_read` | Fetches Gmail over IMAP, returns each message **pseudonymised** | `De: ⟦EMAIL_7⟧` / `Bonjour ⟦NOM_1⟧, …` + a `UID:` line (a mailbox integer, not PII) |
| `bubble_shield_mail_apply` | Applies triage decisions host-side | only a success/failure summary — never the body |

The raw mail never enters the model context, and a reply draft is restored to
the real names **inside Gmail** via the local vault — so the assistant writes
`Bonjour ⟦NOM_1⟧` and Gmail ends up with the real name, without the model ever
seeing it.

**Structural guarantees (the reason it's safe unattended):**

- **Never sends.** `bubble_shield_mail_apply` has no SMTP — it can only APPEND to
  the drafts folder. Drafts only; a human sends.
- **Never deletes.** No `\Deleted`/expunge/Trash/Spam path. "Archive" = removing
  `\Inbox` only, which is fully reversible (`unarchive` restores it). A
  mis-classified mail is always recoverable from *All Mail*.
- **Fail-closed.** If the NER detector is down, `bubble_shield_mail_read`
  **refuses** rather than return raw mail — triage suspends until it's back,
  it never falls back to reading PII in the clear.
- **Capped and journalled.** Mutations are capped per pass and each action is
  logged (chmod 600, without custom label names that could be PII). A draft with
  an unresolved token is **skipped**, never sent with visible markers.
- **A restored draft doesn't re-enter the model's context:** the real name is
  restored *in-memory* into the Gmail draft — it is never returned to the assistant
  nor written to disk, and stderr is redacted (exception *type* only, never the
  message/body).

**Correction is first-class.** If a mail is mis-tagged, the assistant fixes it
with the same tool — `remove_labels` (drop a wrong tag), a change-category flow
(remove the old label + add the right one in one decision), or `unarchive`
(bring it back into the inbox). Removing a label only un-tags — it never deletes.

**Why host-side IMAP.** Cowork greys out its own Gmail-mutation tools in an
unattended run, and a native Gmail connector would return raw mail to the model.
Because both mail tools run host-side, the skill works from a scheduled task with
**no manual validation** — it reads, judges, applies, and posts a report.

**The client list stays current with no code change.** The advisor's client
list is read *pseudonymised* from the protected folder (`clients/clients_routing.csv`)
and matched **token-to-token** against the mails: thanks to the shared vault, a
client's email carries the *same* token in the list and in the mail, so
classification runs on consistent tokens, never real addresses. Re-exporting the
list from a CRM/O2S refreshes the routing the next day — no redeploy.

## Quickstart

```bash
git clone <this-repo> bubble_shield && cd bubble_shield
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt          # demo webapp deps; the engine needs none

# 1) Try the demo webapp (binds 127.0.0.1 only — own-machine / screen-share tool):
uvicorn webapp.app:app --host 127.0.0.1 --port 8765
#    → open http://127.0.0.1:8765
#
#    Demo flow: paste text → see PII cloaked with ⟦tokens⟧, before/after side-by-side,
#    vault table (token↔value, local only), then toggle masquer/conserver per entity
#    type and re-anonymise to see the policy take effect.
#
#    What the "Contrôle & réglages" dashboard exposes:
#      • Risk stats — runs / unsafe / errors / safe-rate across all sessions.
#      • Policy table — masquer/conserver toggle for ALL entity types, including
#        any custom fields you've added via MCP (uses extended_policy_view()).
#      • Champs personnalisés — list current regex/GLiNER/keep-list entries and add
#        new ones; every add is validated by pii_guard.check_input() (same guard as MCP,
#        single source of truth — a real IBAN or proper noun is refused identically).
#      • Détecteur — select gliner / openai / both; the OpenAI mode badge shows its
#        true availability (requires onnxruntime < 1.27) so the UI is never dishonest.
#
#    pypdf is resolved automatically: system install first, then the vendored copy at
#    plugin/bubble-shield/vendor/pypdf — so PDF upload works without extra pip installs.
#    Scanned/image PDFs are not supported (no OCR); the page shows a clear notice.
#
#    NOTE: this webapp is an own-machine dev/demo tool only. It binds 127.0.0.1 and
#    is NOT part of the shipped plugin payload (not vendored into plugin/).

# 2) Run the reliability bench (recall/precision on FR finance fixtures):
python bench/run_bench.py

# 3) Tests:
pytest
```

### Use it as a library

```python
from bubble_shield import AnonymizationEngine, Vault

engine = AnonymizationEngine(vault=Vault(mission="dossier-dupont-2026"))
res = engine.anonymize("Monsieur Jean Dupont, IBAN FR76 3000 6000 0112 3456 7890 189.")

res.anonymized      # 'Monsieur ⟦NOM_0001⟧, IBAN ⟦IBAN_0002⟧.'
res.safe_to_send    # True  (entities found & masked, no residual, no sub-threshold)
res.verdict_state   # 'masked_ok' | 'leak' | 'low_confidence' | 'zero_detection' | 'nothing_to_do'
res.verdict_fr      # human-facing FR verdict for the state above
engine.deanonymize(res.anonymized)   # round-trips back to the original

# NB: a SUBSTANTIAL document where the engine finds ZERO entities is NOT reported
# as safe — res.safe_to_send is False and verdict_state == 'zero_detection'.
# "Found nothing" is not "safe"; on free text it often means a name was MISSED.
```

## Turning on the optional layers

```bash
# Layer 2 — Presidio NER (names/locations):
pip install presidio-analyzer spacy && python -m spacy download fr_core_news_lg
```
```python
AnonymizationEngine(use_ner=True)
```

```bash
# Layer 3 — local LLM via Ollama (run it on YOUR machine, never a server):
#   install Ollama → https://ollama.com  then:  ollama pull llama3.1
export BUBBLE_SHIELD_OLLAMA_URL=http://localhost:11434   # defaults shown
export BUBBLE_SHIELD_OLLAMA_MODEL=llama3.1
```
```python
AnonymizationEngine(use_llm=True)
```

Both default to **off** and add no dependency to the core. With Ollama
unreachable (e.g. on a server), `use_llm=True` behaves exactly like the
pure-regex build — it never breaks, it only ever *adds* recall where a local
model is available.

You can also plug in any custom detector:

```python
AnonymizationEngine(extra_detectors=[my_gazetteer])   # any text -> list[Match]
```

## Layout

```
bubble_shield/
├── bubble_shield/
│   ├── engine.py        # detect → anonymise / de-anonymise + fail-closed scan
│   ├── recognizers.py   # FR/finance regex + checksum recognizers
│   ├── vault.py         # the reversible token ↔ value store (per mission)
│   ├── gazetteer.py     # French first-name list (untitled-name recall)
│   ├── presidio_ext.py  # optional Presidio/spaCy NER layer
│   └── llm_ext.py       # optional local-LLM (Ollama) prose layer
├── webapp/              # FastAPI + Jinja demo (before/after, vault, verdict)
├── bench/               # reliability bench + FR finance fixtures
├── tests/               # 38 tests
└── PRIOR_ART.md         # what Bubble Shield borrows and where it diverges
```

## Status

Reliable anonymiser + demo webapp + reliability bench (100 % recall on the
fixture set). The next building block — wiring Bubble Shield into an LLM client
(Claude Code hooks / a proxy) so anonymisation happens transparently — is a
separate, downstream concern and intentionally out of scope here.

## License

MIT — see [LICENSE](LICENSE).
