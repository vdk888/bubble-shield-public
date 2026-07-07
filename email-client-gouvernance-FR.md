# Email client — Bubble Shield & gouvernance des données (FR)

**Objet :** Protéger les données de vos clients quand vous utilisez l'IA — une approche par niveaux

---

Bonjour [Prénom],

Vous souhaitez utiliser l'assistant IA sur vos dossiers clients sans exposer leurs
données personnelles. C'est exactement le problème que Bubble Shield résout. Voici, en
clair, comment il vous protège — et pourquoi la protection se construit **en
plusieurs couches qui s'additionnent**, plutôt qu'en une seule barrière.

Un mot de cadrage d'abord, pour être précis et honnête : Bubble Shield fait de la
**pseudonymisation locale**. Concrètement, il remplace les informations qui
identifient votre client (nom, IBAN, e-mail…) par des étiquettes neutres
*avant* que l'IA ne les voie, et garde la table de correspondance **sur votre
machine**. Au sens du RGPD, ce n'est pas de l'« anonymisation » totale (les
données restent des données personnelles puisque vous, vous pouvez ré-identifier)
— mais la pseudonymisation est **une mesure de sécurité que le RGPD recommande
explicitement** (articles 25 et 32). Elle **réduit la quantité et la sensibilité**
des données personnelles qui atteignent un tiers, ce qui sert directement la
**minimisation des données** (art. 5) et votre **devoir de confidentialité**
envers le client.

## Vos obligations, en deux phrases

En tant que CGP/CIF, vous êtes responsable du traitement des données de vos
clients. Les envoyer à un prestataire d'IA est un transfert à un sous-traitant :
le RGPD s'applique pleinement (la CNIL le rappelle — utiliser l'IA *n'exonère pas*
du RGPD). Vos principales obligations : **minimiser** les données envoyées
(art. 5), les **sécuriser** (art. 32), intégrer la protection **dès la conception**
(art. 25), encadrer le sous-traitant par un **contrat / DPA** (art. 28), et — si
les données sortent de l'UE — disposer d'un **mécanisme de transfert** valide.

Bubble Shield agit sur les premières (minimisation, sécurité, privacy by design). Il ne
remplace pas le contrat avec le fournisseur ni le mécanisme de transfert : il
vient **par-dessus**, en défense en profondeur.

## La protection par niveaux

**Le cœur de la protection — vos dossiers clients marqués (Cowork *et* Claude Code).**
Sur un **dossier client marqué**, la protection est **garantie** : l'assistant ne
peut techniquement pas ouvrir un fichier protégé en clair. Toute tentative de
lecture est **bloquée**, et l'assistant est redirigé vers le coffre, qui lui
renvoie le contenu **déjà pseudonymisé** (les vraies valeurs ne touchent jamais
son contexte). Un seul marqueur à la **racine** d'un dossier synchronisé (par
ex. votre dossier Dropbox client) protège **tout ce qu'il contient, à toute
profondeur** — les sous-dossiers héritent automatiquement. C'est la couche la plus
solide, et elle couvre le cœur de votre activité, **à l'identique sur Cowork et
sur Claude Code**.

La contrepartie est la même sur les deux : ce qui n'est **pas** dans un dossier
marqué — typiquement un **e-mail entrant**, ou un fichier posé ailleurs — n'est
pas couvert. Cela demande **un réflexe simple de la part de l'employé** :
*enregistrer le document dans le dossier protégé avant de demander à l'assistant de
travailler dessus.* Le coffre s'applique alors normalement. C'est une contrainte
organisationnelle légère, mais réelle — et c'est pourquoi l'**emplacement du
marqueur** (idéalement à la racine du dossier de travail) est l'étape clé de la
mise en place.

**Cowork vs Claude Code — la vraie différence.**
Dans les deux cas, vous installez le **même plug-in Bubble Shield** et la
protection repose sur le **même mécanisme** : dossier marqué → lecture bloquée →
redirection vers le coffre. La différence n'est **pas** « manuel vs automatique »,
ni « installer vs ne rien installer » — les deux reposent sur le même réflexe
« travailler depuis le dossier protégé ». Claude Code (en ligne de commande / dans
l'éditeur, sur le poste) offre une **portée de configuration plus large** — par
exemple une liste de dossiers protégés **globale** au poste, que l'environnement
plus cloisonné de Cowork ne permet pas (dans Cowork, on protège via le **marqueur**
déposé dans le dossier). Le choix se fait surtout sur **l'outil que vos équipes
utilisent déjà**, pas sur le niveau de protection du cœur d'activité, qui est le
même.

> *Précision technique honnête :* il n'existe pas, à ce jour, de couche qui
> pseudonymiserait « automatiquement, partout, sans rien faire » — y compris les
> e-mails — sur l'une ou l'autre version. La garantie repose sur le blocage des
> lectures dans les dossiers marqués, pas sur un filtre invisible appliqué après
> coup. C'est un choix de conception : une protection qu'on peut **prouver** plutôt
> qu'une promesse qu'on ne peut pas tenir.

**Niveau 3 — La rétention courte chez le fournisseur : ferme la fenêtre
d'exposition.**
Indépendamment de Bubble Shield, votre compte Anthropic (Team/Entreprise) **peut être
configuré** pour limiter la durée de conservation : suppression des données après
résiliation, durées de rétention courtes côté API, et même un accord « zéro
rétention » pour les comptes éligibles. *Ceinture et bretelles :* même dans le cas
improbable où une donnée passerait, son exposition reste **limitée dans le temps**.
À confirmer/activer selon votre contrat — c'est une couche qui s'**ajoute** au
coffre, jamais qui le remplace.

## Une précision sur la lisibilité (ce n'est pas une couche de protection en plus)

Un point de confort, à ne pas confondre avec les couches de protection ci-dessus :
la liste des noms déjà confirmés — qui permet à Bubble Shield de continuer à
masquer un nom vu une fois, même en dehors du document d'origine — est
volontairement **large** par prudence, et accumule donc, avec le temps, quelques
mots courants masqués à tort. Un nettoyage automatique en arrière-plan (« la
gazetteer de-pollution ») retire ces faux positifs pour rendre les documents
plus lisibles, en s'appuyant en partie sur un petit modèle d'IA **local, sur
votre machine, sans connexion réseau**. Ce nettoyage ne modifie **jamais** ce qui
est détecté ni masqué par ailleurs — en cas de doute, l'entrée reste masquée,
exactement comme avant. C'est une amélioration de confort, pas une nouvelle
garantie de protection : le cadrage juridique ci-dessus (pseudonymisation
locale, art. 25 & 32) est inchangé.

## Le cas des e-mails : lecture anonymisée côté machine (la minimisation appliquée à la boîte)

La précision technique ci-dessus reste vraie : il n'existe pas de filtre invisible
qui pseudonymiserait « tout, partout, sans rien faire ». Mais pour un flux précis —
le **tri de la boîte Gmail** — Bubble Shield étend la garantie du dossier marqué à
l'e-mail, **sans jamais renvoyer le mail en clair au modèle**. C'est la même
logique de minimisation, appliquée à la boîte de réception.

Concrètement, quand l'assistant trie la boîte, il ne se connecte **pas** à Gmail
par un connecteur natif (qui exposerait le message brut). Il lit chaque mail par un
**outil côté machine** qui le renvoie **déjà pseudonymisé** : là où le message dit
« Marc Dubois, IBAN FR76… », l'assistant lit « ⟦NOM_1⟧, IBAN ⟦IBAN_2⟧ ». La PII
n'atteint donc **jamais** le modèle — exactement le principe du coffre, étendu au
canal e-mail. Le classement (libellé, archivage) et la préparation d'un brouillon
se font ensuite par un **chemin d'écriture côté machine** ; quand un brouillon doit
porter le vrai nom, il est **restauré localement** dans Gmail, le vrai nom allant
dans le brouillon et **jamais dans le contexte du modèle**.

Deux points comptent pour la gouvernance :

- **Minimisation (art. 5) servie directement.** Le tri s'appuie majoritairement sur
  des signaux **non-personnels** (en-têtes techniques, `List-Unsubscribe`,
  domaines) ; le jugement fin lit le corps **anonymisé**. La quantité de données
  personnelles qui atteint le modèle est réduite au strict nécessaire — et souvent à
  rien pour le bruit (newsletters, transactionnel).
- **Garanties structurelles, pas déclaratives.** L'outil de mutation **ne peut pas
  envoyer** (aucun SMTP — brouillons uniquement, un humain envoie) ni **supprimer**
  (aucune corbeille ni `\Deleted` — « archiver » retire seulement le mail de la
  boîte, et c'est réversible). Le pire cas d'une erreur est donc un mail **mal
  étiqueté ou archivé, récupérable** — jamais une donnée perdue ni un envoi non
  voulu. Enfin, **fail-closed** : si le détecteur est indisponible, la lecture
  **refuse** plutôt que de renvoyer du brut. La liste des clients qui alimente le
  tri est elle-même lue **anonymisée** depuis le dossier protégé et rapprochée
  jeton-à-jeton — aucune vraie adresse ne transite.

Cette brique **s'ajoute** aux couches précédentes sans en modifier le cadrage
juridique : c'est toujours de la **pseudonymisation locale** (art. 25 & 32), au
service de votre minimisation et de votre confidentialité — et elle ne remplace ni
le DPA, ni le mécanisme de transfert, ni votre politique de conservation.

## Le point le plus important : ça dépend de vos workflows

Le bon dosage entre ces niveaux **dépend surtout de la façon dont votre équipe
travaille réellement au quotidien** : d'où arrivent les documents clients (dossiers
partagés, e-mails, scans…), qui les manipule, à quelle fréquence, et quel niveau
de discipline est réaliste à demander aux employés.

- Si vos dossiers vivent déjà dans des dossiers structurés et que l'essentiel du
  travail se fait *à partir de ces dossiers* → **Cowork couvre déjà très bien** (rien
  à installer), le réflexe « document → dossier protégé » suffit.
- Si vous voulez des **réglages plus fins** (dossiers protégés globaux, intégration
  au poste) et que l'installation sur les postes ne pose pas de problème →
  **Claude Code** est probablement le meilleur choix. Dans les deux cas, la
  protection du cœur d'activité repose sur le **dossier marqué**, pas sur un filtre
  automatique invisible.

La meilleure recommandation, je peux vous la faire **une fois qu'on a regardé
ensemble vos flux internes** — c'est là que se décide l'approche la plus adaptée
*et* la plus simple à tenir pour vos équipes.

## En résumé

Bubble Shield **pseudonymise localement** les données avant qu'elles n'atteignent l'IA et
garde la clé de ré-identification chez vous — une mesure de réduction du risque
que le RGPD encourage (art. 25 & 32), qui sert votre minimisation des données
(art. 5) et votre confidentialité client. Cette protection se **superpose** au
contrat fournisseur (DPA, art. 28), au mécanisme de transfert et à la rétention
courte côté serveur. Le tout forme une défense en profondeur — et le réglage
optimal dépend de vos workflows internes.

Dites-moi quand vous voulez qu'on passe en revue vos flux ; je vous propose
ensuite la configuration la plus adaptée.

Bien à vous,
[Signature]

---

> *Ceci ne constitue pas un conseil juridique. La mise en conformité d'un cabinet
> doit être validée par un avocat spécialisé (données personnelles / réglementation
> financière) et, le cas échéant, par votre DPO. Bubble Shield est une mesure technique de
> réduction du risque : elle ne remplace ni le contrat de sous-traitance (DPA,
> art. 28 RGPD), ni le mécanisme de transfert hors UE, ni votre politique de
> conservation.*

---

## Notes internes (NE PAS envoyer au client — pour Joris)

- **Précision juridique tenue exprès :** on dit « pseudonymisation locale », jamais
  « anonymisation » ni « exonère du RGPD ». La donnée envoyée reste une donnée
  personnelle (vault local = ré-identification possible de votre côté). Source :
  RGPD art. 4(5) + considérant 26.
- **Ce que Bubble Shield NE fait PAS** (à ne pas laisser croire au client) : il ne
  remplace pas le DPA Anthropic (art. 28), ni le mécanisme de transfert
  (adéquation EU-US Data Privacy Framework si Anthropic certifié, sinon CCT/SCC),
  ni la tenue du registre (art. 30), ni la politique de conservation interne.
- **À vérifier avant d'affirmer au client** (le brief de recherche le signale) :
  (1) la certification DPF actuelle d'Anthropic + les termes exacts du DPA au
  moment de la signature ; (2) la nature précise du « secret professionnel » du
  cabinet — pour un CIF, la confidentialité repose sur la déontologie AMF + RGPD +
  contrat, pas sur un secret pénal type avocat/médecin ; et ça varie selon les
  statuts (IOBSP, courtage assurance…). Faire valider par un avocat.
- **Sources** (brief complet dans `research/` si besoin) : CNIL fiches IA
  (avr. 2024 → 2025), RGPD art. 4/5/25/28/32/33, CMF L541-4, décision d'adéquation
  EU-US DPF (10 juil. 2023), pages Anthropic DPA/SCC/ZDR.
