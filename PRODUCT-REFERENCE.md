# Bubble Shield — Product Reference

> **Purpose of this document.** A single authoritative reference for how Bubble Shield
> works, what it covers, what it does *not* cover, and its RGPD posture. Written to be
> accurate to the shipped product (not aspirational), and to serve as the factual base for
> commercial / client-facing material. Last updated 2026-07-02 (reflects v1.18.18 + the
> in-flight detection & mail work).
>
> ⚠️ **Two audiences.** Sections marked **[TECH]** are engineering-accurate. Sections marked
> **[COMMERCIAL-BASE]** are written so they can be lifted, softened, and reframed into sales
> copy — but every claim here is factual and bounded. Do not add claims to the commercial
> version that aren't supported here.

---

## 1. What Bubble Shield is (in one paragraph) [COMMERCIAL-BASE]

Bubble Shield is a local, reversible privacy layer for AI assistants used on client data.
It sits between a financial advisor's documents/emails and the AI model (Claude, via Cowork),
and ensures that **identifying client data — names, IBANs, addresses, national-ID numbers,
company registration numbers, dates of birth — is replaced with opaque tokens before it ever
reaches the model.** The mapping between tokens and real values (the "vault") **never leaves
the advisor's machine.** The AI works on the tokenised text; the final document is restored
locally. The result: the advisor gets AI assistance on real dossiers, while the raw personal
data of their clients is not transmitted to the AI provider in clear.

It is built specifically for **French wealth-management advisors (CGP)** and their regulatory
reality (RGPD, AMF), on the Cowork / Claude Code platform.

---

## 2. How it works — the mechanism [TECH]

### The core guarantee: the tool OWNS the read
Bubble Shield does not try to "scrub" data after the AI has seen it. It intercepts the read
*before* raw data reaches the model, and reroutes it through an anonymising path that returns
**only tokenised text as its own output**:

- **File reads.** A `PreToolUse` hook (`guard.py`) DENIES any attempt by the AI to read a file
  inside a **protected folder** (marked with a `.bubble-shield.json` file) via the normal
  tools (`Read`/`Grep`/`Glob`/`Edit`/`Write`/`Bash`, and — as of v1.18.18 — generic MCP file
  tools and glob-expanded shell commands). Instead, the AI is told to call the MCP tool
  **`bubble_shield_read(path)`**, which returns the file **already tokenised** (`⟦NOM_0001⟧`,
  `⟦IBAN_0002⟧`…). The real values never enter the model's context.
- **Email reads** *(disabled in V1 — kept in reserve).* An IMAP mail-triage path
  (`bubble_shield_mail_read` / `bubble_shield_mail_apply`) exists in the codebase but is
  **not enabled in the shipped product** — the tools are gated off (`_mail_enabled()` is
  false unless `BUBBLE_SHIELD_ENABLE_MAIL=1`) and are not exposed to the assistant. It can
  be re-enabled without a re-deploy. Documented in §9 for reference; it is not a live V1
  feature.
- **Restoring the answer.** When the AI produces a document containing tokens, **`bubble_shield_write`**
  de-tokenises it locally from the vault. The advisor gets the real, finished document; the AI
  never saw the real values.
- **Pasted-into-chat data** is covered by a second, advisory hook (`tripwire.py`) that nudges the
  user to work from the protected folder instead (see §5 — this is a guard-rail, not containment).

### The vault (the reversibility, kept strictly local) [TECH]
Each dossier has a **vault** mapping token↔real-value. It is **local to the machine** and is
**never transmitted** to any model or server (verified: the anonymising daemon and webapp bind
to `127.0.0.1` only; no telemetry, no external POST of vault contents). The same client gets the
**same token across all their files (and email)** — so a name masked in a PDF appears as the same
token in a related email. *(Encryption-at-rest: available today via `save_encrypted` (HMAC-SHA256,
encrypt-then-MAC); making it the default is in progress — see §7.)*

### Fail-closed by design [TECH]
The controls fail **safe**, not open:
- If the guard's decision logic errors for any reason (malformed input, missing config), it
  **DENIES** the read rather than letting data through (hardened v1.18.18).
- If the strong detection engine (the on-device ML model) is not running, `bubble_shield_read`
  and `bubble_shield_mail_read` **REFUSE to return a document** rather than return a
  weakly-scrubbed one — no partial result that could carry a missed name.

---

## 3. What it detects — coverage [TECH] / [COMMERCIAL-BASE]

Bubble Shield detects and tokenises these categories of French client data:

| Category | Examples |
|---|---|
| **NOM** — person names | surnames, forenames, full names, incl. particles/hyphenated/civility-prefixed |
| **ADRESSE / VILLE** | full addresses, streets, communes + postcode |
| **IBAN** | bank account numbers (mod-97 validated; spaced/unspaced/separated) |
| **SECU** | numéro de sécurité sociale (NIR) |
| **SIREN / SIRET** | company registration numbers |
| **NUM_FISCAL** | fiscal reference numbers |
| **PIECE_IDENTITE** | ID / passport numbers |
| **DATE_NAISSANCE** | dates of birth |
| **TEL** | phone numbers (0X, +33, various formats) |
| **EMAIL** | email addresses |
| **NUM_CLIENT** | client/dossier reference numbers |
| **ISIN** | security identifiers *(kept by default — a fund identifier, not personal data)* |
| **MONTANT** | amounts *(policy-configurable — kept or masked per the advisor's choice)* |
| **POSTE / SOCIETE** | job titles, company names |

**Detection is layered:** a deterministic core (regex + checksum validation for structured
identifiers like IBAN/NIR/SIREN) **plus an on-device machine-learning model** (GLiNER, a
multilingual named-entity recogniser) for free-text names, addresses, and organisations that
have no fixed pattern. Both run **100% locally** — no data leaves the machine for detection.

### Measured accuracy [TECH — use carefully in commercial copy]
On an adversarial French wealth-advisory test battery (synthetic data), with the on-device ML
engine active — **the configuration a properly-onboarded advisor runs**:
- **Overall recall: 94%** of identifying values detected.
- **Names: 93%** (vs 28% with the deterministic-only fallback — the ML model is what closes the
  free-text-name gap).
- Structured identifiers (IBAN, NIR, SIREN/SIRET, phones, emails, dates): **~90–100%**.

**Honest bounds (must be stated, never omitted in commercial use):**
- It is **not 100%.** On the battery, 2 synthetic values still leaked. Bubble Shield is a
  **strong risk-reduction measure with human review**, not a guarantee of zero leakage.
- The **94% figure is for the ML-active configuration.** With the ML model unavailable, the
  read path **refuses** (fail-closed) rather than under-protecting — so an advisor is never
  silently served weak protection, but the feature is unavailable until the model is running.
- **Human review remains required** on sensitive dossiers. The tool's "safe to send" verdict
  now explicitly says that *no detection found* does **not** guarantee *no PII present*
  (v1.18.19) — it is a decision aid, not a certification.

---

## 3.5. Gazetteer de-pollution — reducing over-masking [TECH] / [COMMERCIAL-BASE]

The cross-session name gazetteer (§3's persistent "known PII" deny-list, which lets Bubble
Shield keep masking a name it has seen once even outside the document where it first
appeared) is deliberately biased toward **over-collection**: it is cheap to add an entry, and
a missed real name is the risk that matters. Over time this accumulates **false positives** —
form-label words and common capitalised nouns that get swept in alongside real names, and then
get masked unnecessarily every time they recur, hurting readability.

**De-pollution is a background process that removes exactly those false positives — it never
touches masking recall going forward.** Nothing about what gets *detected* changes; de-pollution
only re-evaluates entries already sitting in the gazetteer.

**The decision cascade, in two stages:**
- **Stage 1 — frequency + structure (statistical, no model).** A lowercase, high-frequency
  common word is confidently dropped by pure word-frequency statistics alone — instant, no
  inference cost.
- **Stage 2 — on-device model adjudication (the ambiguous remainder).** Capitalised entries
  are genuinely hard to call with statistics alone (many real French surnames are themselves
  common words), so they are handed to a small **local language model** running as a
  background daemon bound to `127.0.0.1` — **no network egress, nothing leaves the machine.**
  The model answers one narrow question per candidate ("common word, or a surname?") and only
  an unambiguous "common word" verdict is accepted. Any ambiguity, any daemon error, or the
  daemon being offline all resolve identically: **the entry stays masked.**

This is fail-toward-masking end to end: de-pollution can only ever make output *more readable*
(fewer unnecessary maskings of ordinary words), and structurally cannot weaken protection —
worst case, an entry that should have been dropped simply stays masked, which is the same
safe-by-default posture as before de-pollution existed.

**Self-correcting, with a human backstop.** Removing an entry from the gazetteer is never
permanent-by-omission: if the value later reappears as a plausible name, ordinary detection
re-adds it. Every de-pollution decision is logged to a review queue for audit, and a human
reviewer can mark an entry **sticky** — a manual override that pins the decision (kept or
dropped) and that de-pollution will not revisit automatically.

**Commercial framing:** this is a readability/quality improvement, not a new protection
claim — do **not** describe de-pollution as improving recall or accuracy of detection. It
addresses a specific, honest side-effect of the deliberately-aggressive gazetteer design
(over-masking of common words over time), while leaving the fail-closed detection guarantee
of §2–§3 completely unchanged.

---

## 4. Where it runs — surfaces [TECH]

- **Cowork (Claude Desktop)** — the target platform for the CGP clients. Fully covered.
- **Claude Code CLI and the IDE extension** — covered (the hook engine runs there too).
- **A companion desktop app** (macOS) — the human control surface: review queue for uncertain
  detections, vault management (RGPD rectification/erasure), and the known-PII list.
- **Not covered: plain `claude.ai` web chat** — there is no hook engine there. Advisors on the
  CGP stack use Cowork, which is covered.

---

## 5. What it does NOT cover — honest limits [TECH]

State these plainly; they protect the client and us:
- **Not anonymisation in the legal sense.** It is **reversible pseudonymisation** (the vault
  makes it reversible). See §6.
- **Not 100% recall.** ~94% measured with ML active; human review required on sensitive dossiers.
- **Pasted / uploaded chat content** is only *nudged*, not intercepted — data pasted directly
  into the chat box reaches the model before any tool runs (a platform limit). Advisors are
  guided to work from the protected folder, where interception is real.
- **The advisor's own machine is trusted.** Bubble Shield protects what goes to the *AI provider*;
  it does not defend against malware on the advisor's own computer or a compromised OS.
- **It does not replace** a DPA with the AI provider, a DPIA, a data-transfer analysis, or the
  advisor's own responsibility (see §6).

---

## 6. RGPD posture [COMMERCIAL-BASE — the compliance story]

> Full analysis in `COMPLIANCE_RGPD.md`. This is the summary. It is a design analysis, not
> legal advice; a DPO/lawyer must validate before deployment on real client data.

**Bubble Shield implements *pseudonymisation*, explicitly named in RGPD art. 32 as an
appropriate security measure — not *anonymisation*.** The original document and the vault
remain personal data for the advisor (the data controller). What changes is what reaches the
AI provider.

**The core legal argument (grounded in current case law):** identifiability is assessed
*relatively*, per recipient. The CJEU confirmed (EDPS v CRU / SRB, C-413/23 P, 4 Sept 2025)
that **the same data can be personal for the original controller yet anonymous for a recipient
who has no reasonable means to re-identify it.** Because the vault never leaves the advisor's
machine, the AI provider receives opaque tokens it **cannot reverse** — so the transmitted text
can be argued **anonymous in the provider's hands**, while remaining pseudonymised (personal)
for the advisor. This is precisely the architecture Bubble Shield implements.

**What Bubble Shield supports (✓), supports-but-doesn't-replace (◐), or leaves to the org (✗):**
- ✓ **art. 5-1-c minimisation** — only non-identifying tokens reach the AI provider.
- ✓ **art. 25 & 32 security / privacy-by-design** — local-first, offline detection, fail-closed,
  pseudonymisation by default.
- ✓ **Reduces the cross-border transfer** of *personal* data to the AI provider (the SRB argument).
- ◐ **Vault confidentiality (art. 5-1-f)** — local + access-restricted; encryption-at-rest is
  being made the default.
- ✗ **DPA with the AI provider (art. 28)**, **DPIA (art. 35)**, **transfer analysis (Chap. V)** —
  organisational; Bubble Shield is a *documented mitigating measure* within them, not a substitute.

**One-line positioning:** *Bubble Shield is a strong RGPD security measure (pseudonymisation
+ privacy-by-design) that minimises the personal data sent to the AI — not an exemption from
the advisor's broader obligations.* That honesty is itself a selling point to a DPO.

---

## 7. Roadmap / known gaps being closed [TECH]
- **Email protection** (`bubble_shield_mail_read` + `bubble_shield_mail_apply`, IMAP) —
  **built but disabled in V1** (kept in reserve behind `_mail_enabled()`, not exposed to
  the assistant). Reference in §9; re-enable without re-deploy when it becomes a V1 feature.
- **Gazetteer de-pollution** (A+D→Gemma cascade, self-correcting) — **shipped** (v1.22.0). Full
  reference in §3.5.
- **Detection tuning** — closing the two measured leak shapes (bare title-case names in prose;
  hyphen-grouped IBANs).
- **Vault encryption-at-rest as default** (currently available, being made default).
- **Compliance pack** (DPA template, DPIA template, transfer-analysis note) — proposed as a
  packaged client deliverable, turning "a tool" into "a compliant workflow."

### 7.1 The architecture roadmap — two phases (the strategic direction) [TECH]

Bubble Shield masks at the **tool layer** today: the `bubble_shield_read` / `bubble_shield_list`
MCP tools return already-anonymised text, so the agent only ever holds `⟦tokens⟧`. This exists for
one concrete reason: **Cowork runs remotely on Anthropic's servers** (not on the client's Mac), so
there is no local network hop to intercept — tool-layer masking is the *only* viable approach for
Cowork. (Verified 2026-07-07: Cowork execution is remote; its own in-VM egress proxy has documented
allowlist bugs; a local `127.0.0.1` proxy is physically outside Cowork's path.)

The tool-layer design has a structural cost: **the agent never sees real data, so anything it must
reference *by name* can break** — e.g. masked folder names in a listing make it impossible for the
user to tell the agent which client folder to open (the 2026-07-07 folder-navigation bug). These are
handled today by surgical per-surface fixes (a folder/file *name* is a navigation label the user owns
and already sees on their machine → returned in clear; file *contents* stay masked).

**Phase 2 — egress-proxy masking (once clients migrate Cowork → Claude Code CLI).** A local reverse
proxy on `127.0.0.1` masks PII **only on the last hop to Anthropic's API**. The agent keeps the
**real data in its context** (folder navigation, file references, natural reasoning all work) while
nothing identifying crosses to the AI subprocessor. This **dissolves the entire "masking breaks
reference" bug class** and is an even cleaner RGPD art.46 story (pseudonymisation literally *at the
transfer boundary*). Crucially, it **reuses Shield's whole engine** (recognisers, vault, Gemma
de-pollution, gazetteer) — only the *integration point* changes (proxy body-rewrite instead of MCP
tool output), so it is a re-plumbing, not a rewrite.

**Why Phase 2 is gated on the migration (verified 2026-07-07):** Claude Code CLI **can** be pointed
at a local proxy — it honours `ANTHROPIC_BASE_URL` + `HTTPS_PROXY`, has **no hard cert-pinning**, and
Anthropic explicitly documents support for local TLS-inspection proxies (`NODE_EXTRA_CA_CERTS`). So
the egress-proxy architecture becomes fully viable the moment a client is on Claude Code CLI. Until
then, Cowork clients stay on tool-layer masking. Reference implementation shape: klovys99
(github.com/Korbicorp/klovys99) — same idea, with Shield's superior FR/finance engine. Tracked as
board card **#585** (design-first when the migration lands).

**Net roadmap:** *tool-layer masking + surgical per-bug fixes today (Cowork era)* → *egress-proxy
masking tomorrow (Claude Code era)*. Both phases share one engine; the choice is purely *where* the
masking boundary sits, driven by *where the client's session runs*.

---

## 8. For the commercial doc — what to lift, and the guardrails [COMMERCIAL-BASE]
- **Lead with the guarantee** (§1) and the local-vault story — that's the emotional core for a
  CGP ("your clients' data doesn't leave your machine in clear").
- **Use the 94% recall** — but *always* with the "not 100%, human review required" bound (§3).
  A trust product that over-claims accuracy loses more than it gains the first time a name slips.
- **Use the RGPD story** (§6) as a DPO-facing differentiator — the SRB-based relative-anonymity
  argument is current and strong, and few competitors will have it.
- **Do NOT say "anonymisation"** as the capability claim — say "pseudonymisation réversible et
  locale." (It's both legally correct and, counter-intuitively, more credible to a DPO.)
- **Sell the workflow, not the regex** — the moat is the compliant, French-CGP-tailored workflow
  (tool + compliance posture + human review + onboarding), not the detection engine alone.

---

## 9. Mail triage — feature reference (DISABLED in V1, kept in reserve) [TECH] / [COMMERCIAL-BASE]

> **Status: not enabled in the shipped product.** The mail tools are gated off
> (`_mail_enabled()` returns false unless `BUBBLE_SHIELD_ENABLE_MAIL=1`) and the
> `bubble-shield-mail-triage` skill lives in `reserve/`. This section describes the
> reserved design; it is NOT a live V1 capability.

Bubble Shield can triage a whole Gmail inbox (several times a day, or on a morning scheduled
task) **without the model ever seeing real client PII.** Built as the
`bubble-shield-mail-triage` skill plus two MCP tools. Each message is read *already
pseudonymised*, classified into a 5-tier taxonomy (**Clients / Important / Newsletters /
Structurés / CV / Transition**), labelled and archived; a reply/transfer *draft* can be
prepared in the user's voice.

### The skill and the two tools [TECH]

| Component | Type | What it does |
|---|---|---|
| `bubble-shield-mail-triage` | skill | Reads mail pseudonymised, classifies into the 5-tier taxonomy (first-match-wins, order matters), applies labels + archives, prepares reply/transfer drafts. Golden rules: doubt → archive, never delete; never auto-send (drafts only). |
| `bubble_shield_mail_read` | MCP tool | Fetches Gmail over **IMAP host-side**; returns each message **pseudonymised** (`De: ⟦EMAIL_7⟧`, `Bonjour ⟦NOM_1⟧`) with a `UID:` line (a mailbox integer, not PII — never anonymised) that the assistant passes straight to apply. |
| `bubble_shield_mail_apply` | MCP tool | Applies triage decisions host-side: add/remove labels, archive/unarchive, create reply **drafts** restored from tokens via the vault. Batched per pass. |

### Structural guarantees [TECH — these are the honesty invariants]

- **Never sends.** `bubble_shield_mail_apply` has no SMTP — it can only APPEND to the drafts
  folder. Drafts only; a human sends. A draft with an unresolved token is **skipped**, not sent
  with visible markers.
- **Never deletes.** No `\Deleted`/expunge/Trash/Spam path. "Archive" = removing `\Inbox` only,
  and it is reversible (`unarchive`). The worst case of a mistake is a **mislabelled or archived
  mail, recoverable** from *All Mail* — never a deletion, never an unwanted send.
- **Fail-closed.** If the NER detector is down, `bubble_shield_mail_read` **refuses** rather than
  return raw mail — triage suspends until it is back; it never falls back to reading PII in the clear.
- **Capped + journalled.** Mutations are capped per pass (60) and each action is logged (chmod
  600, without custom label names that could be PII). stderr is redacted (exception *type* only,
  never the message/body) for both tools that restore PII.
- **Correction is first-class** (v1.21.5): `remove_labels`, change-category (remove old + add new
  in one decision), `unarchive`. Removing a label only un-tags — it never deletes.

### Why host-side IMAP [TECH]

In an unattended scheduled task, Cowork **greys out its own Gmail-mutation tools** (it won't let
an AI mutate a mailbox without human validation), and a native Gmail connector would return raw
mail to the model. Both mail tools run **host-side over IMAP**, so the skill works from a
scheduled task with **no manual validation** — it reads, judges, applies, and posts a report.

### Client-list dependency — token-to-token match [TECH]

The advisor's client list lives in the protected folder (`clients/clients_routing.csv`) and is
read **pseudonymised** like any protected file. Thanks to the shared vault, a client's email
carries the **same token** in the list and in the mail, so classification runs **token-to-token**,
never on real addresses. Re-exporting the list from a CRM/O2S refreshes the routing the next pass —
**no code change, no redeploy**. If the list is absent, the assistant classifies only on non-list
signals (human-named handwritten mail, `List-Unsubscribe`, subject keywords) and flags that the
`🔴 Clients` sort will be partial until a list is exported.

### Honest bounds (state, never omit) [TECH — use carefully in commercial copy]

- Triage leans mostly on **non-PII signals** (headers/domains); the fine judgement reads the
  **pseudonymised** body — the model still never sees real PII.
- Same recall bound as §3: pseudonymisation is **best-effort**, not perfect. A name in an odd
  position (e.g. a URL slug) can slip; the remedy is the known-PII gazetteer / `bubble_shield_add_known_pii`.
  Do **not** claim perfect masking of mail.
- Nothing is ever sent or deleted — that is the load-bearing safety guarantee for running it
  unattended.
