---
name: bubble-shield-onboarding
description: "Help a non-technical CGP / financial advisor understand, set up, and use Bubble Shield — protecting a client folder, the before/after, the masquer/conserver settings, the optional accuracy pack (better DETECTION, not everywhere-masking). Triggers when the user asks how it works, how to set up / configure it, which folders are protected, how to anonymise a dossier, see the before/after, the masquer/conserver table, protect data everywhere or catch PII in emails, turn on the smart detection — or seems unsure how to operate it, even without naming it. Also 'démarrer', 'onboarding', 'montre-moi', 'première fois' launch the guided first-run demo. Plain language, no jargon. CRITICAL HONESTY: do NOT claim it anonymises everywhere or that e-mail is auto-protected in Cowork (PostToolUse does not fire on built-in Read/connectors). Reliable protection = the marked FOLDER + bubble_shield_read; for e-mail, save the message into the folder first."
---

# Bubble Shield — onboarding & operation (for a non-technical advisor)

Your user is a **financial advisor (CGP/CIF)**, not an engineer. They installed
Bubble Shield Guard to safely use an AI assistant on real client files without sending
identifying data to a model. Your job is to make the tool feel obvious and
trustworthy. Explain in plain French, with concrete analogies, and *do* the
setup steps for them rather than handing over commands to run.

The golden rule to convey: **the client's real name, address, account numbers
never leave this computer.** Everything else follows from that.

---

## Première prise en main — flux guidé (guided demo flow)

**Trigger words:** "démarrer", "onboarding", "montre-moi", "première fois",
"prends-moi par la main", "guide-moi", "configure Bubble Shield", or any
first-session signal. When triggered, run the six steps below in order.

Use **elicitation** for every decision gate — it renders as choice buttons in
Cowork. Do NOT skip steps or merge them; each step is one confirm/enum elicit +
a brief progress line.

**Honesty invariant for the whole flow:** never claim "anonymise everywhere" or
that e-mail is auto-protected. The flow teaches folder-first. The accuracy pack
improves *detection quality* on what Bubble Shield already reads — nothing more.

---

### Étape 1 — Bienvenue et consentement

Say this welcome message verbatim (adapt language to theirs if needed):

> « Bienvenue dans Bubble Shield. Je vais vous installer en 5 minutes.
> À la fin, on le fera ensemble sur un de VOS vrais fichiers : je ferai
> une vraie tâche dessus sans jamais voir le nom du client — puis votre
> Mac produira le document final avec le vrai nom, sans que je l'aie vu.
> Prêt ? »

Then **elicit** (renders as buttons):

```
[Démarrer la configuration]   [Plus tard]
```

- If `[Plus tard]` → stop the flow here. Say: « Pas de problème — dites
  "première prise en main" quand vous voulez recommencer. »
- If `[Démarrer la configuration]` → continue to Étape 2.

---

### Étape 2 — Installer TOUS les modèles en une seule passe (détection + OCR)

Progress line first:
> « **Étape 1/3 — Installation des modèles.** J'installe en une seule fois
> tout ce dont Bubble Shield a besoin, 100 % en local sur votre Mac :
> • **GLiNER** — détection fine des noms/adresses/identifiants (NER multilingue) ;
> • **OpenAI Privacy Filter** — détection PII renforcée ;
> • **OCR** — lecture des PDF scannés (Docling + RapidOCR).
> ~900 Mo au total, une seule fois, rien n'est envoyé sur internet. Les
> modèles déjà présents sont ignorés (pas de re-téléchargement). »

Call **`bubble_shield_setup_ml`** with `action: "start"`. This single call
pulls **GLiNER + OpenAI Privacy Filter + OCR** in one pass — there is no
separate "voulez-vous aussi installer X ?" step afterwards.

Then poll **`bubble_shield_setup_ml`** with `action: "status"` every ~20 s
until status is `ready` (or timeout after ~10 min → error message). The status
returns a **per-model line** naming each model and its state — relay it to the
user verbatim. Examples:

> « 📦 GLiNER ↓ téléchargement · OpenAI-PF ↓ téléchargement · OCR ↓ téléchargement »

then later:

> « 📦 GLiNER ✓ déjà présent · OpenAI-PF ✓ prêt · OCR ✓ prêt »

Keep company during the wait, naming what's downloading from the status line:
> « J'installe les modèles… (ceux déjà présents sur votre Mac sont ignorés). »

When `ready`:
> « ✓ Tout est prêt : GLiNER + OpenAI Privacy Filter + OCR. La détection est à
> son maximum et les PDF scannés seront lus et anonymisés. Vous ne serez plus
> jamais sollicité pour installer un modèle. »

> Note : `bubble_shield_setup_ocr` existe encore (compatibilité), mais l'onboarding
> n'en a PAS besoin — `bubble_shield_setup_ml(action='start')` installe déjà l'OCR
> dans la même passe.

**Elicit** to confirm readiness before continuing:

```
[Continuer — marquer un dossier]   [Annuler le guidage]
```

- `[Annuler le guidage]` → stop gracefully.
- `[Continuer — marquer un dossier]` → Étape 4.

---

### Étape 4 — Marquer le dossier protégé (folder-first)

Progress line:
> « **Étape 2/3 — Dossier protégé.** La protection de Bubble Shield ne
> s'active que sur les dossiers que vous lui montrez. On va marquer un
> dossier de démonstration maintenant. »

Say:
> « Cliquez et choisissez le dossier client à protéger — une fenêtre du
> Finder va s'ouvrir. »

Then **open a native Finder folder picker** so the user CLICKS the folder
instead of typing a path. Call Bash with:

```bash
osascript -e 'POSIX path of (choose folder with prompt "Choisissez le dossier client à protéger")'
```

- On success it prints the chosen folder's POSIX path (e.g.
  `/Users/vous/Documents/Clients/DUPONT/`) — use that as the path.
- If the user clicks Cancel, osascript exits non-zero with
  `User canceled` on stderr → say « Pas de souci » and re-offer the picker
  (or `[Annuler le guidage]`). Never crash.
- **Fallback only if osascript is unavailable** (rare, e.g. headless): ask
  for the path as free text, example `/Users/vous/Documents/Clients/DUPONT`.

If the user is following the demo exactly, they pick the DUPONT client folder.

Once you have the path:
1. Request directory access with `request_cowork_directory` (the client
   folder, never `~/.config` or `~`).
2. Write the marker file `<dossier>/.bubble-shield.json`:
   ```json
   {
     "allow_paths": ["clean"],
     "allow_extensions": [".anon.txt"],
     "block_bash": true,
     "tripwire_enabled": true
   }
   ```
3. Confirm out loud:
   > « ✓ Dossier marqué. Tout fichier à l'intérieur est maintenant dans
   > le coffre — l'assistant ne peut plus l'ouvrir directement. »

**Elicit:**

```
[Lancer la démo sur le DCC]   [Annuler le guidage]
```

---

### Étape 5 — Démo : une VRAIE tâche sur un VRAI fichier (deux temps)

> « **Étape 3/3 — La démo.** Au lieu d'un exemple tout fait, faisons-le
> sur un de VOS vrais fichiers, avec une vraie tâche. Vous allez voir
> deux versions du résultat côte à côte : celle que MOI j'ai vue
> (anonymisée, avec des étiquettes) — et la vraie (avec les noms
> réels), produite sur votre Mac sans jamais passer par moi. »

**Elicit — laissez le client choisir SA tâche et SON fichier :**

```
Sur quel fichier de ce dossier, et pour quelle tâche ? Par exemple :
« résume-moi le DCC de M. X », « rédige la lettre de synthèse à partir
de ce profil », « extrais les points clés de cet avis d'imposition ».
```

- Le client nomme **un fichier réel** (dans le dossier protégé marqué à
  l'Étape 4) et **une tâche réelle** (résumé, lettre, extraction, note…).
- S'il ne sait pas quoi choisir, proposez : « résumez-moi ce document et
  rédigez-en une courte note de synthèse » sur le fichier de son choix.
- Notez le chemin du fichier = `<FICHIER>`, la tâche = `<TÂCHE>`.

#### Temps A — « L'IA fait la tâche en aveugle » (résultat ANONYMISÉ, montré en session)

**Get the file's content as GUARANTEED-masked tokens — two calls, in order:**

1. Call **`bubble_shield_read`** on `<FICHIER>` to get the file's text.
   ⚠️ On a folder you JUST marked (Étape 4), the background sweep hasn't
   indexed this file yet, so `bubble_shield_read` may return the **raw**
   extracted text this first time (that is the shadow-index design — a
   fresh file is masked by the sweep afterwards, not at read time). So do
   **NOT** show or use whatever `bubble_shield_read` returns directly in
   the demo — it could still contain the real name.
2. Pass that returned text through **`bubble_shield_anonymize_text`**. THIS
   is the call that guarantees masking: it runs the detector on-device and
   **fails closed** (it returns tokens, or an error — never raw PII). Its
   output is the tokenised version you will work on and show.

The result of step 2 is the document content with all PII replaced by tokens
(`⟦NOM_0001⟧`, `⟦ADRESSE_0001⟧`, `⟦IBAN_0001⟧`, `⟦DATE_NAISSANCE_0001⟧`…).

**Work ONLY on the `bubble_shield_anonymize_text` output.** Do NOT use the
`bubble_shield_read` raw output, do NOT use the filename as a source of
identity, do NOT paste or infer any real personal value. You see only tokens.

Now **actually perform `<TÂCHE>`** using only the tokenised version —
a real result, not a canned one. Keep every PII in token form. Then
**show that token-form result to the client in the session**, framed:

> « Voici le résultat de votre tâche, tel que MOI je l'ai produit —
> regardez, je n'ai jamais vu un seul nom réel : partout où il y avait
> une identité, je n'ai eu que des étiquettes (⟦NOM_0001⟧, ⟦IBAN_0001⟧…).
> **C'est la version anonymisée — la preuve que ça a marché sans exposer
> vos données.** »

**Elicit** before Temps B:

```
[Voir la vraie version — restaurée sur mon Mac]   [Arrêter ici]
```

- `[Arrêter ici]` → skip to Étape 6.
- `[Voir la vraie version…]` → continue.

#### Temps B — « Votre Mac produit la vraie version » (résultat EN CLAIR, dans un fichier)

Take the **exact token-form result from Temps A** (same text, PII still
in `⟦…⟧` form — do NOT re-draft, do NOT try to fill in real values
yourself; you don't have them and never will).

Call **`bubble_shield_write`** with:
- `path` = a file at the **root of the marked folder** (a GUARDED path),
  e.g. `<dossier>/resultat-demo.txt` (or `.pdf`).
  ⚠️ **NE PAS** écrire dans `clean/` : ce sous-dossier est en `allow_paths`
  (lecture autorisée, réservé aux copies **anonymisées** de la skill
  anonymize) — un fichier RESTAURÉ (vraies valeurs) y serait relisible par
  l'IA. `bubble_shield_write` **refusera** un chemin non protégé (hors
  dossier marqué, sous `clean/`, ou extension exemptée) : c'est voulu.
- `content` = the token-form result from Temps A, verbatim.

This is the "script that reuses the tokens to decrypt": Bubble Shield
replaces every `⟦…⟧` token with its real value **from the local vault
on the Mac**, writes the finished file to disk (à un emplacement
**protégé**), and returns ONLY a success confirmation + a count of
restored values — **never the clear content**. The real names never enter
this conversation.

Then say (make the side-by-side explicit — this is the punchline):

> « ✓ Fait. **Ouvrez vous-même ce fichier sur votre Mac** (dans le Finder,
> ou via la visionneuse locale Bubble Shield) :
> `<dossier>/resultat-demo.txt`
>
> Je ne le rouvrirai PAS, moi — et je ne le peux pas : ce fichier est dans
> le dossier protégé, donc si j'essayais de le lire, Bubble Shield me
> **bloquerait** (sinon les vrais noms reviendraient dans notre
> conversation). **Ce blocage, c'est justement la protection qui
> fonctionne.** C'est à vous, humain, de l'ouvrir.
>
> Comparez les deux :
> • **Ce que j'ai vu, moi** (ci-dessus, en session) → des étiquettes.
> • **Ce que votre Mac a produit** (le fichier, que VOUS ouvrez) → le vrai
>   résultat, avec les vrais noms.
>
> **Même tâche, même résultat — mais l'identité de votre client n'a
> jamais quitté votre ordinateur.** L'IA a travaillé sur des étiquettes ;
> c'est votre Mac, à la toute fin et en local, qui a remis les vraies
> valeurs. Rien en clair n'est passé par l'IA ni par Cowork. »

> Signature (si la tâche produit une lettre) : « Votre conseiller » ou le
> nom du cabinet du client depuis `deployment_allowlist.json`. Ne signez
> JAMAIS « Bubble Invest » dans une démo client — c'est notre société.

> 💡 **Pour AFFICHER le vrai document à l'écran du client** (au lieu de « ouvrez-le
> dans le Finder »), enchaînez avec une visionneuse Cowork (`present_files` /
> `create_artifact`) sur le fichier restauré — voir la section
> **« Afficher un VRAI document au client »** juste ci-dessous. Le clair va à
> l'écran du client, jamais dans votre contexte.

⚠️ **Ne trichez pas la démo.** Ne devinez pas, ne re-tapez pas, ne
« complétez » jamais une vraie valeur à la place d'un token, même si le
nom semble évident d'après le nom de fichier ou le contexte. Toute la
preuve repose sur le fait que le résultat en session est 100 % en
étiquettes et que seul `bubble_shield_write` (côté Mac) connaît le clair.

---

## Afficher un VRAI document au client — le circuit artefact / visionneuse (Cowork)

Souvent le client veut **voir le résultat final avec les vrais noms**, pas juste
« ouvrez le fichier dans le Finder ». En Cowork, vous pouvez le lui afficher
DIRECTEMENT à l'écran — en toute sécurité — avec un **outil visionneuse Cowork**.
La règle qui rend ça sûr : le clair va à l'ÉCRAN du client, jamais dans votre
contexte à vous (l'assistant).

### Le circuit en 4 temps (à suivre dans cet ordre)

1. **Lire en jetons** — `bubble_shield_read(path=<fichier client>)`. Sur un
   fichier déjà indexé, vous ne voyez que des étiquettes (`⟦NOM_0001⟧`,
   `⟦IBAN_0001⟧`…). ⚠️ Sur un fichier **tout neuf** que le balayage n'a pas
   encore indexé, cette lecture peut renvoyer le texte **en clair** une
   première fois — si vous n'êtes pas sûr que le fichier est déjà indexé,
   repassez ce que vous avez lu par **`bubble_shield_anonymize_text`** (qui
   masque toujours, ou échoue — jamais de clair) avant de vous en servir.
2. **Rédiger EN JETONS** — écrivez le document final (HTML, lettre, note…) en
   n'utilisant QUE les étiquettes. Vous n'avez pas les vraies valeurs, et c'est
   le principe.
3. **Restaurer sur un chemin PROTÉGÉ** — `bubble_shield_write(path=…, content=…)`
   avec votre brouillon-jetons. Bubble Shield remet les vraies valeurs **depuis
   le coffre local** et écrit le fichier restauré sur le disque, **à la racine
   du dossier protégé** (un chemin gardé) — **JAMAIS dans `clean/`** (refusé, et
   relisible par l'IA). L'outil ne renvoie qu'une confirmation + un compte, jamais
   le contenu en clair.
4. **Afficher à l'humain** — appelez un **outil visionneuse Cowork** sur le
   fichier RESTAURÉ pour le rendre à l'écran du client :
   - **`present_files`** (argument `files: [{ file_path: <fichier restauré> }]`) —
     affiche le/les fichier(s) tels quels dans le panneau Cowork ;
   - **`create_artifact`** / **`update_artifact`** (argument `html_path: <fichier
     HTML restauré>`) — publie un artefact riche (mise en page, avant/après…).

   Le clair apparaît sur l'écran du client ; **vous, l'assistant, n'avez toujours
   vu que des jetons.**

### L'invariant à ne JAMAIS enfreindre

> **N'écrivez JAMAIS vous-même une vraie valeur dans un artefact ou un fichier.**
> Vous ne les avez pas — et si vous en tapiez une, c'est que vous l'auriez vue =
> une fuite. Les vraies valeurs n'atteignent le client QUE via le fichier restauré
> sur disque (produit par `bubble_shield_write`), affiché par une visionneuse.

Autrement dit : votre brouillon d'artefact ne contient que des `⟦…⟧`. C'est
`bubble_shield_write` (côté Mac) qui produit la version en clair, et la
visionneuse ne fait que **la montrer** au client.

### Exemple concret (le flux exact, testé)

Le client demande : « montre-moi la situation patrimoniale de M. X, avec les
vrais noms pour moi ». Vous enchaînez :

1. `bubble_shield_read(path=".../clients/dupont/DCC.pdf")`
   → vous recevez le contenu en jetons : `⟦NOM_0001⟧`, `⟦IBAN_0001⟧`, montants réels…
2. Vous écrivez un HTML de synthèse **en jetons** (tableau État civil, patrimoine…),
   avec `Write` vers un fichier de travail, p. ex.
   `.../clients/dupont/situation.html` — **le HTML ne contient QUE des `⟦…⟧`**
   pour l'identité (les montants réels, eux, sont autorisés en clair par choix).
3. `bubble_shield_write(path=".../clients/dupont/situation-reelle.html", content=<votre HTML-jetons>)`
   → renvoie « ✅ 15 valeur(s) réelle(s) restaurée(s) » + le chemin. **Pas le contenu.**
   Le fichier `situation-reelle.html` sur disque contient maintenant les vrais noms.
4. `create_artifact(html_path=".../clients/dupont/situation-reelle.html")`
   (ou `present_files(files=[{file_path: ".../situation-reelle.html"}])`)
   → l'artefact s'affiche à l'écran du client **avec les vrais noms**. Vous, vous
   n'avez toujours vu que `⟦NOM_0001⟧`.

Résultat : le client voit une fiche complète et réelle ; votre transcript ne
contient aucun vrai nom. Dites-le-lui — c'est la démonstration la plus parlante
du produit.

### Pourquoi le garde ne bloque PAS ces visionneuses (alors qu'il bloque `Read`)

Le garde Bubble Shield bloque `Read`/`cat`/les autres outils fichier sur un
chemin protégé — sinon les vrais noms reviendraient dans votre contexte. Mais il
**laisse passer** `present_files` / `create_artifact` / `update_artifact`, même
sur un fichier restauré protégé, pour une raison précise et vérifiée : **ces
outils ne renvoient à l'assistant qu'un chemin ou une confirmation, jamais le
contenu du fichier.** Ils affichent à l'humain, pas à vous. C'est exactement la
même garantie que `bubble_shield_read`/`write` : le client voit, l'IA ne voit pas.

> ⚠️ Cette exemption ne vaut QUE tant que ces outils ne renvoient pas le corps du
> fichier. Elle ne s'étend à AUCUN autre outil : tout autre `mcp__*` fichier, et
> `Read`/`cat`, restent bloqués sur un chemin protégé.

### En une phrase pour le client
> « Je vous affiche le vrai document à l'écran sans jamais l'avoir lu moi-même :
> votre Mac remet les vrais noms dans le fichier, et l'affichage vous le montre —
> moi, je n'ai travaillé que sur des étiquettes. »

---

### Étape 6 — Clôture et suite

> « La configuration est terminée. Voici ce qui est en place :
> ✓ GLiNER + OpenAI Privacy Filter (détection avancée) installés
> ✓ OCR (PDF scannés) installé
> ✓ Dossier protégé marqué
> ✓ Démo complète — vraie tâche sur un vrai fichier : résultat anonymisé (vu par l'IA) vs vrai résultat (produit par votre Mac)
>
> Que voulez-vous faire maintenant ? »

**Elicit** (renders as three buttons):

```
[Protéger un autre dossier]   [Régler masquer/conserver]   [Terminé]
```

- `[Protéger un autre dossier]` → return to Étape 4 logic (new path,
  same marker-drop procedure).
- `[Régler masquer/conserver]` → continue to the masquer/conserver
  section below.
- `[Terminé]` → say:
  > « Parfait. Bubble Shield est opérationnel. Pour refaire cette visite
  > guidée : dites "première prise en main". Pour protéger un nouveau
  > dossier client : dites "protège ce dossier". »

---

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
  it returns the file's contents with client data replaced by `⟦…⟧` tokens. This
  is the default way to open a single protected file, and the path that works in
  Cowork (a plain read of a protected file is blocked by design). Then work on
  what it returns; de-anonymise the final answer locally.
  - **One caveat (v1.23.0):** the read is fast because it serves a masked copy
    prepared in the background for files the tool has already seen. A **brand-new
    file** the tool hasn't processed yet can come back **not yet masked** on its
    first read (it gets queued and masked right after). So for a new dossier,
    prefer the whole-dossier batch below the first time — it masks everything up
    front — rather than relying on the very first single-file read.
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

> **In-chat view vs. the desktop app.** The artifact above is the quick
> Cowork-native view (the in-chat HTML can't be a live server — Cowork's sandbox
> localhost isn't the user's machine). For ongoing **review / vault / gazetteer
> management**, the user has a separate **Bubble Shield desktop app** (see below).

## The Bubble Shield desktop app — the human review surface (on their Mac)

**Bubble Shield is TWO pieces:** (1) this plugin inside Cowork = the *protection*
(it anonymises), and (2) a small **"Bubble Shield" app on the user's Mac** = the
*human control surface*. They are separate because the Cowork sandbox can't open
a window on the host — the app runs natively on the Mac.

**The app has three screens the user will need:**
- **File de révision** — the inbox of **low-confidence candidates** Bubble Shield
  wasn't sure about (e.g. a bare first name, a partial address that scored under
  the threshold). The user clicks **Confirmer** (→ it's masked everywhere after)
  or **Ignorer** (→ never masked). **This is the human safety net for the
  near-misses** — without it, sub-threshold data the model wasn't sure about
  stays in clear. ALWAYS tell the user this app exists and that they should open
  it periodically to clear the review queue.
- **Coffre** — the token↔value table per dossier (masked, reveal-on-click);
  correct or forget a mapping (RGPD).
- **Liste connue** — the gazetteer of confirmed PII; remove a wrong entry.

**Installing the app (once, on the user's Mac)** — one command in Terminal:
```
curl -fsSL https://raw.githubusercontent.com/vdk888/bubble-shield-public/main/install-app.sh | bash
```
It drops a **"Bubble Shield" app** on their Desktop (real icon, no terminal).
First launch: **clic droit → Ouvrir**, once (it's unsigned). Re-running the
command updates it. Tell the user this is a one-time setup, and that you (their
Bubble Invest contact) can do it with them if they prefer.

> When to point the user to the app: after they process documents, say
> « Pensez à ouvrir l'app Bubble Shield → File de révision pour confirmer les
> données que je n'étais pas sûr de masquer. » The plugin protects; the app is
> where the human has the final say on the uncertain cases.

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

## "Tu as oublié CE mot précis" — add a client-flagged miss (Cowork-native)

Different from the section above. `add_field` teaches a **pattern/category**
(«tous les numéros de dossier»). This is for the other case: the client points at
**one specific word** that appeared **in clair** and shouldn't have — «tu as oublié
DELMARRE», «tu as laissé passer ce nom». The detector isn't perfect; bare proper
nouns with no context score below threshold and slip through.

**Add it to the liste connue (gazetteer) so it's masked EVERYWHERE from now on:**
```
bubble_shield_add_known_pii(value="Delmarre", confirm=true)   # entity_type="NOM" par défaut
```
It's then masked **deterministically** in every later document — no NER score needed.

⚠️ **Warn the client BEFORE adding.** This word will be masked **partout où il
apparaît**, dans tous les documents. If it's a **common word** (a very common first
name, a dictionary word), it can **over-mask** legitimate text — confirm with the
client that it's specific enough. `confirm=true` is **required** (poka-yoke: the
tool refuses without it, to force you to have raised this). It rejects a
pattern-looking value (`\d{5}`, `[A-Z]…`) and steers you to `add_field` — because a
category is that other tool's job, not this one.

**Division of labour:** high-confidence detections already enter the liste connue
**automatically** — you do nothing. `bubble_shield_add_known_pii` is for the
**misses only the client catches**.

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

---

## En cas de fuite ou de problème — escalader à Bubble Invest

Si une donnée identifiante apparaît **en clair** dans un résultat (fuite), si le
masquage semble désactivé, ou pour **tout problème, doute ou bug**, dites-le
clairement à l'utilisateur et invitez-le à nous le signaler **immédiatement** :

> 📧 **bubbleinvest.ai@gmail.com**

C'est notre canal de support. Mieux vaut sur-signaler que laisser passer une
exposition. N'essayez pas de masquer ou minimiser un problème : signalez-le,
arrêtez le traitement de la donnée concernée, et orientez vers cet e-mail.
