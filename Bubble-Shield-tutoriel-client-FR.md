# Bubble Shield — Guide de démarrage

### Protéger les données de vos clients quand vous travaillez avec l'IA

*Un guide simple, sans jargon. Vous serez opérationnel en quelques minutes.*

---

## En une phrase

Vous déposez vos dossiers clients dans un dossier protégé. Quand vous demandez à
l'assistant de travailler dessus, **Bubble Shield remplace automatiquement les
noms, IBAN et e-mails par des étiquettes anonymes avant que l'IA ne les voie.**
Vous, vous récupérez à la fin un document complet avec les vrais noms. L'IA, elle,
n'a jamais su de qui il s'agissait.

> **L'image à retenir : le vestiaire de théâtre.** Chaque manteau (chaque donnée
> identifiante) reçoit un numéro à l'entrée. L'IA ne voit que les numéros. Le
> registre qui relie les numéros aux vrais noms reste dans un tiroir fermé, **sur
> votre ordinateur** — il ne part jamais ailleurs.

---

## Ce dont vous avez besoin

- Cowork (l'application Claude) installée et connectée à votre compte.
- Vos dossiers clients quelque part sur votre ordinateur — typiquement un dossier
  **Dropbox** (par ex. `Dropbox/Clients`).

C'est tout. Pas de serveur, pas d'abonnement supplémentaire, rien à coder.

---

## Étape 1 — Installer Bubble Shield (une seule fois)

Dans Cowork, ouvrez l'onglet **« Customize »** (Personnaliser), puis ajoutez et
installez le plug-in **Bubble Shield** depuis la place de marché. C'est la même
manipulation que pour n'importe quel plug-in Cowork — quelques clics.

> Si vous avez un doute, votre interlocuteur Bubble Invest le fait avec vous en
> deux minutes lors de la mise en place. Une fois installé, c'est fait pour de bon.

Après l'installation, **redémarrez la session** (ou lancez `/reload-plugins`) une
fois, pour que la protection s'active. Vous n'aurez plus à le refaire.

---

## Étape 2 — Protéger votre dossier client (le geste clé)

C'est **l'étape la plus importante**, et elle est très simple.

Vous indiquez à l'assistant **où vivent vos dossiers clients**, et il y dépose un
petit fichier-repère appelé `.bubble-shield.json`. À partir de là, **tout ce que
contient ce dossier est protégé.**

**Dites simplement à l'assistant, en français :**

> *« Protège mon dossier client : c'est `Dropbox/Clients`. »*

Il s'occupe du reste (il vous demandera juste d'autoriser l'accès au dossier une
fois). Vous n'avez aucune commande technique à taper.

> **Le point qui change tout : un seul repère à la racine protège TOUT.**
> Si vous placez le repère à la racine de votre dossier `Dropbox/Clients`, alors
> **tous les sous-dossiers, à n'importe quelle profondeur, sont protégés
> automatiquement.** Un seul geste couvre l'ensemble de vos dossiers clients.
>
> ⚠️ **À l'inverse : un fichier rangé EN DEHORS d'un dossier protégé n'est pas
> couvert.** D'où la règle d'or ci-dessous.

### La règle d'or

> **Travaillez toujours depuis votre dossier protégé.**
> Un e-mail, un scan, une pièce jointe ? **Enregistrez-le d'abord dans le dossier
> protégé**, puis demandez à l'assistant de travailler dessus. Ne collez jamais de
> données client brutes directement dans la conversation (voir « À ne pas faire »).

---

## Étape 3 — Travailler au quotidien

Une fois le dossier protégé, vous travaillez **normalement**. Vous demandez à
l'assistant ce que vous voulez :

> *« Fais-moi un résumé du dossier de M. Dupont. »*
> *« Rédige une lettre de suivi pour ce client. »*
> *« Cette allocation est-elle cohérente avec son profil de risque ? »*

En coulisses, l'assistant lit le fichier **via le coffre** : il ne reçoit que la
version anonymisée. Concrètement, là où le document dit *« Marc Dubois, IBAN
FR76… »*, l'assistant lit *« ⟦NOM_0001⟧, IBAN ⟦IBAN_0001⟧ »*. Il raisonne sur ces
étiquettes, sans jamais connaître l'identité réelle.

---

## Étape 4 — Récupérer le document final (avec les vrais noms)

Quand l'assistant a fini sa lettre, son résumé ou sa note, il l'a rédigé**e** avec
les étiquettes (`⟦NOM_0001⟧`). Bubble Shield **remet automatiquement les vrais
noms** au moment de produire le fichier final, **sur votre ordinateur**.

Résultat : vous obtenez un document **complet et utilisable**, avec « Marc Dubois »
et son vrai IBAN — alors que l'IA, elle, n'a jamais vu ces informations. Le tour
est joué.

---

## Bonus — Faire trier votre boîte mail le matin

Au-delà des dossiers, Bubble Shield sait aussi **trier votre boîte Gmail** — et
c'est peut-être le gain de temps le plus concret au quotidien.

**Ce que ça fait pour vous.** Vous demandez à l'assistant de trier votre boîte
(ou vous le laissez faire tout seul, le matin). Il **range chaque e-mail** dans la
bonne case — un client, un partenaire important, une newsletter, une candidature…
— en posant une **étiquette de couleur** dans Gmail et en **archivant** ce qui
n'a pas besoin de rester sous vos yeux. Vous arrivez le matin devant une boîte
**déjà classée**, avec en tête ce qui compte vraiment (les clients, les
échéances), et le bruit rangé de côté.

> **Le point rassurant : l'assistant ne voit jamais vos vrais clients.** Comme
> pour vos dossiers, il lit chaque mail **déjà anonymisé** — là où le message dit
> « Marc Dubois », il lit « ⟦NOM_1⟧ ». Il décide où le classer sans jamais
> connaître l'identité réelle. Le tri est précis, mais votre confidentialité
> reste intacte.

**Comment se passe une passe du matin.** Vous pouvez simplement demander :

> *« Trie ma boîte mail. »* — ou : *« Range mes e-mails et prépare-moi les
> brouillons de réponse. »*

L'assistant parcourt les nouveaux messages, les classe, archive ce qui doit
l'être, et vous fait un **compte-rendu** : « 34 mails triés, 3 brouillons de
réponse prêts à valider. » Vous pouvez aussi le programmer pour qu'il tourne
**tout seul chaque matin** — votre interlocuteur Bubble Invest met ça en place
avec vous en quelques minutes.

**Les brouillons : c'est vous qui envoyez, toujours.** Quand une réponse est
attendue, l'assistant **prépare un brouillon** dans votre style — mais il ne
l'envoie **jamais**. Le brouillon vous attend dans Gmail, avec les vrais noms
déjà remis en place ; vous le relisez et vous cliquez sur « Envoyer » vous-même.

> **Rien n'est jamais envoyé, rien n'est jamais supprimé.** C'est la garantie de
> fond. L'assistant peut poser une étiquette, archiver, ou préparer un brouillon —
> mais il n'a **techniquement pas** la capacité d'envoyer un e-mail ni d'en
> supprimer un. « Archiver » veut simplement dire « sortir de la boîte de
> réception » : le message reste dans « Tous les messages », toujours
> retrouvable.

**Si l'assistant se trompe de case.** Aucun problème — dites-le-lui :

> *« Ce mail n'est pas un client, c'est une newsletter. »*
> *« Remets celui-là dans ma boîte de réception. »*

Il **corrige** aussitôt : il change l'étiquette, ou remet le mail en boîte. Rien
n'est jamais figé, et une correction ne supprime jamais rien.

**Pour que le tri connaisse vos clients — l'export de la liste.** Pour distinguer
un vrai client d'un simple contact, l'assistant s'appuie sur **votre liste de
clients**, que vous déposez dans votre dossier protégé (un simple export depuis
votre outil de gestion / O2S). Là encore, cette liste est lue **anonymisée** :
l'assistant fait la correspondance sans jamais voir une vraie adresse. Et quand
vous ré-exportez votre liste (nouveaux clients, départs), le tri se met à jour
**tout seul** dès le lendemain — vous n'avez rien de technique à refaire.

---

## Ce que vous verrez si vous oubliez

C'est rassurant, pas gênant : **si l'assistant essaie d'ouvrir un fichier protégé
sans passer par le coffre, il est bloqué.** Vous verrez un message du type :

> 🔒 *« Bubble Shield — accès bloqué. Ce fichier est dans un dossier client
> protégé. Je le lis via le coffre, qui me renvoie une version anonymisée. »*

Ce n'est **pas une erreur** : c'est la protection qui fonctionne. L'assistant
enchaîne alors tout seul par la bonne méthode. Vous n'avez rien à faire — c'est le
filet de sécurité qui vous garantit qu'**une donnée brute ne peut pas fuiter, même
par mégarde.**

---

## À faire / À ne pas faire

**✅ À faire**
- Ranger les documents clients **dans le dossier protégé** avant de les traiter.
- Pour un e-mail sensible : **l'enregistrer d'abord** dans le dossier protégé,
  puis demander à l'assistant de l'analyser.
- Relire les documents importants : Bubble Shield est une protection forte, pas une
  garantie magique.

**❌ À ne pas faire**
- **Coller ou glisser un document client directement dans la conversation.** Ce
  qui est collé dans le chat est sous les yeux de l'assistant *avant* que Bubble
  Shield ne puisse agir — c'est la seule faille, et elle dépend de vous.
- Travailler sur un fichier client rangé **en dehors** d'un dossier protégé.
- Supprimer le fichier-repère `.bubble-shield.json` (cela désactive la protection
  de ce dossier).

---

## Questions fréquentes

**Mes données partent-elles sur internet ?**
Non. Le remplacement se fait **sur votre ordinateur**. Le registre noms ↔
étiquettes ne quitte jamais votre machine. L'IA ne reçoit que la version anonyme.

**Est-ce conforme au RGPD ?**
Bubble Shield fait de la **pseudonymisation locale** — une mesure de sécurité que le
RGPD encourage (articles 25 & 32) et qui sert votre minimisation des données. Cela
ne remplace pas vos obligations contractuelles (DPA avec le fournisseur d'IA) ni
une validation par votre DPO/avocat, mais cela vient **par-dessus**, en défense en
profondeur.

**Et mes e-mails ?**
La protection garantie passe par le dossier : enregistrez le message dans votre
dossier protégé, puis travaillez dessus. (Un assistant peut être *incité* à
anonymiser un e-mail lu directement, mais dans Cowork ce n'est pas une garantie —
le réflexe « e-mail → dossier protégé » l'est.)

**Puis-je choisir ce qui est masqué ou gardé ?**
Oui. Par exemple, vous pouvez **garder les montants en clair** (utile pour
demander une analyse d'allocation) tout en **masquant les noms et le poste**. Votre
interlocuteur Bubble Invest vous montre ce réglage à la mise en place.

**Comment protéger un deuxième dossier ?**
Même geste : demandez à l'assistant de protéger l'autre dossier. Pour arrêter de
protéger un dossier, il suffit de supprimer son fichier-repère.

---

## En cas de doute

Écrivez à votre interlocuteur Bubble Invest — on regarde ensemble vos flux de
travail réels et on cale la configuration la plus simple et la plus sûre pour votre
cabinet.

---

> *Ceci ne constitue pas un conseil juridique. La mise en conformité d'un cabinet
> doit être validée par un avocat spécialisé et, le cas échéant, par votre DPO.
> Bubble Shield est une mesure technique de réduction du risque : elle ne remplace
> ni le contrat de sous-traitance (DPA), ni le mécanisme de transfert hors UE, ni
> votre politique de conservation.*
