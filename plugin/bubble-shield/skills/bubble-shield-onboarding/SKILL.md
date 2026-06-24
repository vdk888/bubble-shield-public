---
name: bubble-shield-onboarding
description: Help a non-technical user (a CGP / financial advisor) understand, configure, and use the Bubble Shield Guard plugin — what it does, how to protect a client folder, how to show the before/after visually, how the masquer/conserver settings work, and how to set up the optional accuracy pack (better DETECTION, not magic everywhere-masking). Use this skill whenever the user asks "how does Bubble Shield work", "how do I set this up / configure it", "which folders are protected", "how do I anonymise a dossier", "how do I see the before/after", "what's the masquer/conserver table", "protect my data everywhere / not just one folder", "catch PII in my emails / everywhere", "turn on the smart/accurate detection", "install the AI detection", or seems unsure how to operate the tool — even if they don't name it. Lead with plain language, never jargon, because the user is not technical. CRITICAL HONESTY — do NOT tell a Cowork user that Bubble Shield anonymises everywhere automatically or that e-mail is auto-protected, because neither is true in Cowork (PostToolUse does not fire on built-in Read or connectors). The reliable protection is the marked FOLDER plus bubble_shield_read; for e-mail, SAVE the message into the protected folder first. The accuracy pack improves DETECTION on what is read through the folder, it does not add everywhere-coverage in Cowork.
---

# Bubble Shield — onboarding & operation (for a non-technical advisor)

Your user is a **financial advisor (CGP/CIF)**, not an engineer. They installed
Bubble Shield Guard to safely use an AI assistant on real client files without sending
identifying data to a model. Your job is to make the tool feel obvious and
trustworthy. Explain in plain French, with concrete analogies, and *do* the
setup steps for them rather than handing over commands to run.

The golden rule to convey: **the client's real name, address, account numbers
never leave this computer.** Everything else follows from that.

## How Bubble Shield works — the one-paragraph version (say this first)

"Avant de parler à l'IA, Bubble Shield remplace les informations qui identifient votre
client (nom, adresse, IBAN, e-mail…) par des étiquettes anonymes — un peu comme
un vestiaire de théâtre où chaque manteau reçoit un numéro. L'IA travaille sur la
version anonymisée, sans jamais savoir de qui il s'agit. Quand elle a fini, on
remet les vrais noms dans sa réponse. La table de correspondance (vos noms ↔ les
numéros) reste dans un tiroir fermé, sur votre ordinateur."

Then, if they want more, the two pieces:

1. **The guard (le verrou).** While it's on, the assistant is *physically blocked*
   from opening files in your protected client folders. If it tries, it's
   stopped and told to anonymise first. So even a mistake can't leak a raw file.
2. **The anonymiser (le coffre).** Turns a file into an anonymised copy you (and
   the assistant) can safely work on. Reversible: the answer gets de-anonymised
   at the end.

There's a fuller plain-language script in `references/explain-to-client.md` —
read it when the user wants the "how does this actually work / is it really
safe?" conversation, or before a demo.

## The three things they'll want to do

### 1. Protect a client folder — drop a marker INSIDE it (Cowork-native)

The whole tool hinges on one thing: **marking which folders hold client data**.
Until a folder is marked, the guard leaves it alone (it fails *safe* by staying
inert on unmarked folders, not by blocking everything).

**In Cowork, you protect a folder by putting a tiny marker file inside it** —
`.bubble-shield.json`. The guard then blocks every read/edit of anything in that
folder (and its sub-folders). This is the ONLY method that works in Cowork,
because Cowork is sandboxed: it can write into a folder the user has connected to
the session, but it CANNOT write to `~/.config` or other hidden system folders
(it will refuse — "overlaps a protected host location"). So the config lives
*with the data*, like a `.gitignore`.

**Do this for the user (don't hand them Terminal commands):**

1. **Ask where their client files live** and have them point you at that folder.
   Common answers: a `Clients` sub-folder in Dropbox, a `Souscriptions` folder in
   Downloads. (Use `AskUserQuestion` if helpful, but accept a free-text path —
   they may name a specific dossier like `Downloads/Souscription X`.)
2. **Get access to that folder.** If it isn't already connected to the session,
   request it with the `request_cowork_directory` tool (path = the client
   folder). The user approves once. ⚠️ Request the **client folder itself**, never
   `~/.config`, `~`, or a system path — those are rejected.
3. **Write the marker** into that folder: create `<client-folder>/.bubble-shield.json`.
   Minimal contents that also exempt the anonymised-output sub-folder:
   ```json
   {
     "allow_paths": ["clean"],
     "allow_extensions": [".anon.txt"],
     "block_bash": true,
     "tripwire_enabled": true
   }
   ```
   An empty `{}` works too (just protects the folder). `allow_paths` entries may
   be relative to the marker's folder. The marker file itself is never blocked.
4. **Confirm it's live.** Tell the user the folder is now protected — anything
   inside it is the *coffre*, and you'll anonymise before reading. (No
   `/reload-plugins` needed for a new marker: the guard re-reads markers on every
   file access. `/reload-plugins` is only needed once, right after the plugin is
   first installed/enabled in Cowork.)

To protect **another** folder later: same thing — drop a `.bubble-shield.json`
into it. To **stop** protecting a folder: delete its marker.

> CLI fallback (only if the user runs Claude Code in a terminal, not Cowork): a
> global `~/.config/bubble_shield/bubble-shield.json` with a `protected_folders` list
> also works and composes with markers. Most clients use Cowork — prefer the
> marker. Full field reference for both in `references/configure.md`.

### 2. Read / anonymise a dossier

When the user wants the assistant to work on a real client file:

- **One file (the quick path):** read it through the **`bubble_shield_read`** tool —
  it returns the file's contents already anonymised (`⟦…⟧` tokens), so the real
  data never reaches the model. This is the default way to open a single
  protected file, and the path that works in Cowork (a plain read of a protected
  file is blocked by design). Then work on what it returns; de-anonymise the
  final answer locally.
- **A whole dossier (batch):** use the companion skill **`bubble-shield-anonymize`** —
  it handles PDFs and Word docs automatically and writes anonymised copies into a
  `clean/` sub-folder. Invoke that skill and work on the cloaked copies.

Either way the real values stay in a local vault and the answer is restored at
the end. If they prefer a visual, show them the **before/after artifact** (next).

### 3. Show the visual (before/after + masquer/conserver)

Some advisors like to *see* the before/after rather than trust a black box. In
Cowork, the way to do that is a **Bubble Shield artifact** — a rich panel that renders
right on their screen with the highlighted before/after, the verdict, and the
masquer/conserver toggle table. Generate it with the `bubble-shield-anonymize` skill's
"Show the before/after visually" step (`scripts/make_artifact.py`) and present
it. For a demo, use a fictional sample — never real client data.

> **Why not "a web app"?** Bubble Shield also ships a local webapp, but it's a
> *run-on-your-own-computer-in-a-terminal* tool: it starts a small server on
> `127.0.0.1`. That can't work inside Cowork (Cowork runs in a sandbox, so its
> localhost isn't the user's machine). The artifact above is the Cowork-native
> equivalent and shows the same view. Only mention the standalone webapp to a
> technical user who runs Bubble Shield on their own machine (see `references/run-webapp.md`).

## The masquer / conserver table — the one setting advisors actually tune

This is where the advisor decides, per type of data, what to **hide** vs **keep
in clear**. Explain the *why*, because it's the crux:

- **Keep € amounts (montants).** The advisor often wants to ask the assistant
  "is this allocation coherent with the client's risk profile?" — which needs the
  numbers. So amounts default to *kept*.
- **Hide the job title (poste).** "Directeur marketing chez TotalEnergies"
  identifies the person almost as surely as their name — so it's *hidden* by
  default.

They can flip any toggle and save; it sticks and applies to the next
anonymisation. Frame it as *their* risk call, and gently warn before they keep an
"identifiant" item in clear.

## "I want to hide a NEW kind of field" — add a custom field (Cowork-native, works now)

When the user says *"this isn't being masked"*, *"hide my dossier numbers too"*,
*"also mask the contract reference"*, *"add a field"*, or you notice a recurring
identifier the default detection misses — you can add it **yourself, in Cowork**,
via the **`bubble_shield_add_field`** MCP tool. (This is the real, working path —
unlike the masquer/conserver artifact, whose checkboxes don't persist in Cowork.)

**Do it FOR them — don't make them write a regex. Your job is to turn their
plain-language example into the right rule.**

1. **Ask for an EXAMPLE of the field, not a definition.** Say:
   > « Donnez-moi un exemple de la donnée à masquer, tel qu'elle apparaît dans vos
   > documents (vous pouvez maquiller les chiffres) — par ex. un numéro de dossier
   > "DOS-2024-0481", une référence contrat, un identifiant interne. »
   Get the **shape**: prefix letters? how many digits? separators (`-`, `/`, space)?
   fixed or variable length? Where it appears (a form label like "N° dossier :").

2. **Derive a REGEX TEMPLATE from the shape — NEVER the literal value.** The tool's
   guard-rail REFUSES a concrete PII instance; you must pass category descriptors
   (`\d`, `[A-Z]`, `{4}`). Examples:
   - "DOS-2024-0481" (DOS- + year + 4 digits) → `DOS-\d{4}-\d{4}`
   - "FR + 11 digits SIREN-style" → `\d{9}` with `validator: "luhn"` if it checksums
   - A contract ref "C/2026/00123" → `C/\d{4}/\d{5}`

3. **Call `bubble_shield_add_field`:**
   - `kind: "regex"` + `entity_type` (UPPER_SNAKE, e.g. `DOSSIER_CODE`) + `label`
     (FR human label, e.g. "Numéro de dossier") + `pattern` (the TEMPLATE) +
     `validator` (`none` unless it has a real checksum: `luhn`/`iban`/`isin`/`mod97`).
   - For a NAME-like field regex can't capture (job title, a product name) → use
     `kind: "gliner_label"` + `gliner_label` as a category PHRASE (e.g.
     `"employer name"`, `"internal project codename"`). Needs the accuracy pack on.
   - To KEEP something in clear (the firm's OWN identifier, never a client's) →
     `kind: "keep"` + `keep_kind` (`phrase`/`email_domain`/`phone`) + `keep_value`
     + `confirm: true`.

4. **Verify it took** with `bubble_shield_list_fields`, then **re-read the document**
   through `bubble_shield_read` and confirm the field is now `⟦…⟧`. Show the user
   the before/after on that field so they SEE it worked.

5. **To remove one:** `bubble_shield_remove_field`.

**Guard-rail to respect (it protects them):** the tool rejects a pattern that is a
real PII value rather than a descriptor — if it refuses, you gave it a literal;
re-derive the template from the *shape*. Never store a real client value as a
"field".

> Plain-language framing for the user: « Je vais apprendre à Bubble Shield à
> reconnaître ce type d'information à partir de sa *forme* (pas de la vraie valeur),
> pour qu'il le masque automatiquement la prochaine fois. »

## The accuracy pack — better DETECTION (optional, opt-in) — NOT "anonymise everywhere" in Cowork

⚠️ **READ THIS FIRST — what the accuracy pack does and does NOT do.** The accuracy
pack is an on-device AI detector (GLiNER) that **improves detection quality** — it
catches names/addresses the simple regex rules miss (e.g. a bare name with no
"Nom :" label). **That is its real value.** But it does **NOT** make Bubble Shield
"anonymise everything you read, everywhere, automatically." Be precise with the
client, because the obvious-sounding promise is false on their setup:

- **The pack improves detection ON THE CONTENT BUBBLE SHIELD ALREADY SEES** — i.e.
  files you read through `bubble_shield_read` / a marked protected folder / the
  `bubble-shield-anonymize` skill. There, GLiNER makes the masking *more accurate*.
- **It does NOT silently anonymise "anything the assistant reads" in Cowork.** The
  "ambient / machine-wide" mechanism relies on a Claude Code **PostToolUse** hook
  rewriting tool output — and **that hook does NOT fire on Cowork's built-in Read
  or on third-party connectors** (proven: 0 of 19 real Gmail calls fired it;
  built-in `Read` output is not rewritten either, any harness version). So in
  **Cowork**, turning on the "global" switch does **not** give you transparent
  everywhere-anonymisation. The reliable protection in Cowork remains: **work from
  a marked folder / read via `bubble_shield_read`** — and the pack makes THAT
  detection better.
- **The "global/ambient" switch is effectively a CLI-only feature.** On the Claude
  Code **CLI** (terminal), the ambient PostToolUse path can engage on `Read`/`Bash`;
  in **Cowork** it cannot. Do not promise a Cowork client machine-wide auto-masking.

So when a user asks "protège mes données partout / anonymise mes e-mails / active
la détection intelligente":

1. **Set the honest expectation FIRST:**
   > « Le "pack précision" est une petite IA locale (rien n'envoyé sur internet)
   > qui rend la détection **plus fine** — elle repère des noms/adresses que les
   > règles simples ratent. Mais soyons clairs : il ne rend PAS Bubble Shield
   > capable d'anonymiser "tout, partout, automatiquement". La protection fiable
   > reste : **travailler depuis un dossier protégé** (je lis le fichier via le
   > coffre, et là le pack améliore le masquage). Pour un e-mail ou un fichier
   > reçu : **enregistrez-le d'abord dans le dossier protégé**, puis demandez-moi. »
2. **Install it FOR them — no Terminal.** Call **`bubble_shield_setup_ml`** with
   `action: "start"`, then poll `action: "status"` every ~20s until `ready`, keeping
   them company ("J'installe la détection avancée, ça prend quelques minutes…").
   100% local, nothing sent anywhere. (CLI alternative: run
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/bubble_shield_setup_ml.py"` directly.)
3. **Where it takes effect:**
   - **The accurate detection applies wherever Bubble Shield reads** — `bubble_shield_read`,
     marked folders, the anonymise skill. No flag needed for those explicit reads.
   - **`bubble_shield_enable_global` (`action:"on"`)** writes the host config for the
     ambient PostToolUse path. **Honest scope: this is meaningful on the CLI, but in
     Cowork PostToolUse does not fire on Read/connectors, so it does NOT deliver
     everywhere-masking there.** Don't sell it as "everywhere" to a Cowork user —
     it's a CLI capability + a no-op-but-harmless flag in Cowork.
   - **Per-folder `"posttool_enabled": true`** in a marker has the same Cowork caveat.
4. **Confirm it's live** in plain words:
   > « C'est prêt. Désormais, quand l'assistant lit un document, un e-mail, un
   > tableur, il anonymise automatiquement les informations sensibles avant de les
   > traiter — où qu'elles se trouvent. Les vraies valeurs restent sur votre
   > ordinateur, comme toujours. »

**Honest framing to keep** (don't oversell — it builds trust):
- It runs **100 % local** — the model is on their Mac, nothing is sent anywhere.
- It's a **broad safety net**, not the hard lock. The *fail-closed* guarantee
  stays the folder guard on marked folders; this layer fails *open* (if anything
  goes wrong it lets the original through rather than freeze the session).
- **The copy-paste / drag-into-chat path is the user's responsibility** — warn
  about it explicitly. No software can anonymise what's pasted straight into the
  conversation, because it's already in front of the assistant.
- The masquer/conserver table (above) governs this layer too — same toggles.

Troubleshooting + the full mechanics are in `references/accuracy-pack.md`.

## The Bubble Shield tools you call (read in / write out)

Four tools ship with the plugin. Reach for them instead of doing PII-handling by
hand — they keep the real values out of your context and in the local vault:

- **`bubble_shield_read`(path)** — read a client *file* anonymised (the default for any
  file that may hold PII; the guard blocks the raw Read).
- **`bubble_shield_anonymize_text`(text)** — anonymise a *block of text that isn't a
  file*: a message, an API result, pasted content. ⚠️ For e-mail in Cowork this is a
  MANUAL fallback, NOT automatic protection: nothing forces the assistant to call it,
  so raw mail can still reach context if it doesn't. **The reliable workflow for mail
  is to save the message into the protected folder and use `bubble_shield_read`** (see
  the E-mails section). Use `bubble_shield_anonymize_text` only as a stopgap when a file
  isn't available, and call it BEFORE reasoning over the text.
- **`bubble_shield_write`(path, content)** — produce a **finished client document with
  the REAL names** without ever seeing them. Draft the letter/summary/note using
  the `⟦…⟧` tokens, then call `bubble_shield_write` with your token draft + the output
  path. Bubble Shield restores the real values locally and writes the file; it returns
  only a success confirmation, **never the real content** — so you build a
  complete, real document blind to the client's identity.
- **`bubble_shield_setup_ml`(action)** — install/check the accuracy pack (above).
- **`bubble_shield_enable_global`(action)** — toggles the ambient PostToolUse path
  (host config). ⚠️ Meaningful on the Claude Code CLI only; in **Cowork it does NOT
  deliver everywhere-masking** (PostToolUse doesn't fire on Read/connectors there).
  Don't describe it to a Cowork client as "anonymise everywhere".

Typical full workflow: `bubble_shield_read` (or `bubble_shield_anonymize_text`) the input →
reason and draft using tokens → `bubble_shield_write` the final document. The client
gets real, usable output; you never touched a real name.

## E-mails — ⚠️ NO automatic protection in Cowork. Save to the protected folder first.

**Say this to a Cowork client, plainly, and do NOT soften it:** Bubble Shield does
**NOT** automatically anonymise e-mail in Cowork. The "mail-guard" is only a *nudge*
to the assistant, and the in-place "mail containment" mechanism **does not run at
all in Cowork** — so e-mail is NOT protected by the tool. **The only reliable way
to use the assistant on an e-mail is: SAVE the message/attachment into your
protected folder FIRST, then ask the assistant to work on it** (it reads it via
`bubble_shield_read`, anonymised). Until you do that, do not paste or have the
assistant read raw e-mail content.

Why (the proven facts, don't overclaim):
- Mail lives behind a third-party connector. When the agent fetches mail, the
  **mail-containment** PostToolUse rewrite is supposed to anonymise it in place —
  but ⚠️ **proven by live test (2026-06-14): Cowork does NOT fire PostToolUse on
  third-party connectors** (0 of 19 real Gmail calls fired it). So in Cowork that
  mechanism **never engages — e-mail auto-protection simply does not work.**
- The **PreToolUse mail-guard** can only *steer* the assistant ("anonymise this
  first") — and that depends on the assistant complying (observed live: sometimes
  it does, sometimes it summarises raw mail). **Treat it as a nudge, never a lock.**
- So in Cowork there is **no enforced e-mail protection**. The enforced path is the
  folder: **save the e-mail/attachment into a marked folder → `bubble_shield_read`.**
  That's the same hard guarantee files get.

### The honest one-liner for the client
> « En Cowork, Bubble Shield ne protège PAS automatiquement vos e-mails. Pour
> travailler sur un e-mail en sécurité : **enregistrez-le d'abord dans votre
> dossier protégé**, puis demandez-moi — je le lirai anonymisé. Sans ça, le contenu
> de l'e-mail n'est pas protégé. »

### CLI distinction (only mention to a technical user running Claude Code in a terminal)
On the Claude Code **CLI** (not Cowork), `mail_containment` (on by default) CAN fire
the PostToolUse rewrite and anonymise a fetched mail result in place. So mail
auto-protection is a **CLI-only** capability. In Cowork it's a no-op (fail-safe:
pass-through, never crashes the connector — harmless, but it does nothing). **Never
tell a Cowork client their mail is auto-protected.** The save-to-folder workflow is
the right answer on BOTH surfaces; it's the *only* reliable one in Cowork.

## How to talk to the client — tone

- **Plain words, no acronyms.** Say "les informations qui identifient votre
  client", not "PII". Say "étiquette anonyme", not "token".
- **Lead with the reassurance**, then the mechanism: the real data never leaves
  the computer; the AI only sees anonymised copies; it's reversible.
- **Use the vestiaire (cloakroom) analogy** — it lands instantly with
  non-technical people.
- **Be honest about limits.** It's a strong safety measure, not a magic shield:
  a human should still glance at anything flagged "à relire", and it doesn't
  replace the firm's RGPD paperwork (DPA/AIPD). Saying this *builds* trust.
- **Never paste raw client data into the chat to demonstrate.** Use the webapp's
  built-in fictional sample, or anonymise first.

See `references/explain-to-client.md` for ready-to-say scripts (the 30-second
pitch, the "is it really safe?" answer, the demo walk-through).

## When something looks wrong

- "The assistant says it can't read my file" → that's the guard working. Read it
  through the **`bubble_shield_read`** tool (returns it anonymised), or run
  `bubble-shield-anonymize` on the whole folder and work on the `clean/` copies.
- "Nothing is being blocked" → the folder probably has no `.bubble-shield.json`
  marker (step 1), or the plugin was just installed and the session still needs a
  one-time `/reload-plugins`. Check that the marker file exists inside the client
  folder.
- "A PDF won't anonymise" → it may be a scanned image (no text). Bubble Shield fails
  *closed* there on purpose; tell the user to paste the text manually rather than
  risk missing PII.
- An amount/job-title is hidden or kept against their wish → it's the
  masquer/conserver table; adjust it (step 3) and re-run.
- "I turned on protect-everywhere but it's not catching names in e-mails" →
  **In Cowork this is expected — there is no everywhere/e-mail auto-masking** (the
  ambient PostToolUse path doesn't fire on Cowork's Read or on the mail connector;
  see the E-mails section). The fix is the workflow, not a setting: **save the
  e-mail/attachment into the protected folder, then read it via `bubble_shield_read`.**
  Don't tell the client to "check the pack" for this — the pack improves detection
  on what's read through the folder; it does not add e-mail coverage in Cowork.
  (On the CLI only, `bubble_shield_setup_ml.py --check-only` + `posttool_enabled: true`
  is relevant to the ambient path.)
