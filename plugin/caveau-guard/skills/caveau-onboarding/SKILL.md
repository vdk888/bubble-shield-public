---
name: caveau-onboarding
description: Help a non-technical user (a CGP / financial advisor) understand, configure, and use the Caveau Guard plugin — what it does, how to protect a client folder, how to show the before/after visually, how the masquer/conserver settings work, and how to set up the optional "protect PII everywhere" accuracy pack. Use this skill whenever the user asks "how does Caveau work", "how do I set this up / configure it", "which folders are protected", "how do I anonymise a dossier", "how do I see the before/after", "what's the masquer/conserver table", "protect my data everywhere / not just one folder", "catch PII in my emails / everywhere", "turn on the smart/accurate detection", "install the AI detection", or seems unsure how to operate the tool — even if they don't name it. Lead with plain language, never jargon, because the user is not technical.
---

# Caveau — onboarding & operation (for a non-technical advisor)

Your user is a **financial advisor (CGP/CIF)**, not an engineer. They installed
Caveau Guard to safely use an AI assistant on real client files without sending
identifying data to a model. Your job is to make the tool feel obvious and
trustworthy. Explain in plain French, with concrete analogies, and *do* the
setup steps for them rather than handing over commands to run.

The golden rule to convey: **the client's real name, address, account numbers
never leave this computer.** Everything else follows from that.

## How Caveau works — the one-paragraph version (say this first)

"Avant de parler à l'IA, Caveau remplace les informations qui identifient votre
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
`.caveau-guard.json`. The guard then blocks every read/edit of anything in that
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
3. **Write the marker** into that folder: create `<client-folder>/.caveau-guard.json`.
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

To protect **another** folder later: same thing — drop a `.caveau-guard.json`
into it. To **stop** protecting a folder: delete its marker.

> CLI fallback (only if the user runs Claude Code in a terminal, not Cowork): a
> global `~/.config/caveau/caveau-guard.json` with a `protected_folders` list
> also works and composes with markers. Most clients use Cowork — prefer the
> marker. Full field reference for both in `references/configure.md`.

### 2. Read / anonymise a dossier

When the user wants the assistant to work on a real client file:

- **One file (the quick path):** read it through the **`caveau_read`** tool —
  it returns the file's contents already anonymised (`⟦…⟧` tokens), so the real
  data never reaches the model. This is the default way to open a single
  protected file, and the path that works in Cowork (a plain read of a protected
  file is blocked by design). Then work on what it returns; de-anonymise the
  final answer locally.
- **A whole dossier (batch):** use the companion skill **`caveau-anonymize`** —
  it handles PDFs and Word docs automatically and writes anonymised copies into a
  `clean/` sub-folder. Invoke that skill and work on the cloaked copies.

Either way the real values stay in a local vault and the answer is restored at
the end. If they prefer a visual, show them the **before/after artifact** (next).

### 3. Show the visual (before/after + masquer/conserver)

Some advisors like to *see* the before/after rather than trust a black box. In
Cowork, the way to do that is a **Caveau artifact** — a rich panel that renders
right on their screen with the highlighted before/after, the verdict, and the
masquer/conserver toggle table. Generate it with the `caveau-anonymize` skill's
"Show the before/after visually" step (`scripts/make_artifact.py`) and present
it. For a demo, use a fictional sample — never real client data.

> **Why not "a web app"?** Caveau also ships a local webapp, but it's a
> *run-on-your-own-computer-in-a-terminal* tool: it starts a small server on
> `127.0.0.1`. That can't work inside Cowork (Cowork runs in a sandbox, so its
> localhost isn't the user's machine). The artifact above is the Cowork-native
> equivalent and shows the same view. Only mention the standalone webapp to a
> technical user who runs Caveau on their own machine (see `references/run-webapp.md`).

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

## The accuracy pack — "protéger les données PARTOUT, pas seulement un dossier" (optional, opt-in)

By default Caveau protects the **folders you mark**. Some advisors want more: have
the assistant anonymise sensitive data **wherever it appears** — e.g. a client
e-mails an Excel of their holdings and the assistant reads it during the morning
mail check. That's the **accuracy pack**: an on-device AI detector that catches
names/addresses the simple rules miss, and anonymises sensitive data in *anything
the assistant reads*, not just marked folders.

It's **off by default** and set up **once, ideally with the person who installed
Caveau for you** (it downloads an AI model — a few hundred MB — the first time).

**When the user asks for this** ("protège mes données partout", "anonymise mes
e-mails", "active la détection intelligente"):

1. **Explain plainly what it adds and its honest limit** (say both):
   > « Par défaut, Caveau protège les dossiers que vous marquez. L'option "partout"
   > ajoute une détection plus fine — une petite IA qui tourne *sur votre
   > ordinateur* (rien n'est envoyé sur internet) — pour repérer et masquer les
   > données sensibles dans tout ce que l'assistant lit, pas seulement vos dossiers
   > clients. Une limite à connaître : si vous *collez ou glissez vous-même* un
   > document directement dans la conversation, il est déjà sous les yeux de
   > l'assistant avant que Caveau n'agisse — travaillez toujours depuis vos
   > fichiers, jamais par copier-coller. »
2. **Set it up FOR them** — run the bootstrap (don't hand them a command):
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/caveau_setup_ml.py"
   ```
   It creates a small dedicated environment, downloads the on-device model, and
   installs an auto-start so the detector is ready at every login. Watch for the
   final `✅ ML pack ready` line. First run needs a network connection and a few
   minutes; after that it's automatic and offline.
3. **Turn the feature on** — add `"posttool_enabled": true` to the client's
   Caveau config (the global `~/.config/caveau/caveau-guard.json`, or — in Cowork
   — to the relevant folder marker). Without this flag the pack stays dormant even
   once installed (opt-in by design).
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
  through the **`caveau_read`** tool (returns it anonymised), or run
  `caveau-anonymize` on the whole folder and work on the `clean/` copies.
- "Nothing is being blocked" → the folder probably has no `.caveau-guard.json`
  marker (step 1), or the plugin was just installed and the session still needs a
  one-time `/reload-plugins`. Check that the marker file exists inside the client
  folder.
- "A PDF won't anonymise" → it may be a scanned image (no text). Caveau fails
  *closed* there on purpose; tell the user to paste the text manually rather than
  risk missing PII.
- An amount/job-title is hidden or kept against their wish → it's the
  masquer/conserver table; adjust it (step 3) and re-run.
- "I turned on protect-everywhere but it's not catching names in e-mails" →
  check the accuracy pack: run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/caveau_setup_ml.py" --check-only`
  (it should print `OK`), confirm `posttool_enabled: true` is set, and remember
  the first detection after a reboot warms the model (the very next read is
  covered). Names in dense text near the confidence threshold can still slip —
  it lifts recall, it isn't perfect. See `references/accuracy-pack.md`.
