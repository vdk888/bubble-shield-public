# Bubble Shield — ready-to-say scripts for a non-technical advisor

Read this before a demo, or when the user asks "how does this actually work / is
it really safe?". These are scripts to *say* (or adapt), in plain French. Pick the
length that fits; don't read all three at once.

## The 30-second pitch

« Bubble Shield, c'est un coffre. Avant d'envoyer quoi que ce soit à l'IA, il remplace
les informations qui identifient votre client — nom, adresse, IBAN, e-mail — par
des étiquettes anonymes. L'IA travaille sur la version anonymisée, sans savoir de
qui il s'agit, puis on remet les vrais noms dans sa réponse à la fin. Le nom de
votre client ne quitte jamais votre ordinateur. »

## The cloakroom analogy (la métaphore du vestiaire)

« Imaginez un vestiaire de théâtre. À l'entrée, chaque manteau — chaque donnée
sensible — reçoit un numéro. L'IA ne voit que les numéros&nbsp;: elle fait tout
son travail (vérifier la cohérence d'un dossier, rédiger une déclaration
d'adéquation) sans jamais savoir à qui appartient quel manteau. Quand elle rend
sa copie, on échange les numéros contre les vrais manteaux. Et la liste qui
associe les noms aux numéros&nbsp;? Elle reste dans un tiroir fermé, chez vous,
jamais envoyée nulle part. »

## "Est-ce que c'est vraiment sûr ?"

« Il y a deux protections. La première&nbsp;: un verrou. Tant qu'il est activé,
l'assistant est techniquement incapable d'ouvrir un fichier de vos dossiers
clients — même par erreur, il est arrêté. La deuxième&nbsp;: l'anonymisation, qui
remplace les données identifiantes avant tout envoi. Et tout se passe sur votre
machine&nbsp;: aucune donnée ne part sur internet.

Je reste honnête avec vous&nbsp;: c'est une protection forte, pas une formule
magique. Pour les cas limites, l'outil signale "à relire" — un coup d'œil humain
reste utile. Et ça ne remplace pas vos obligations RGPD habituelles (registre,
analyse d'impact). Mais pour empêcher qu'un nom ou un IBAN parte en clair vers
une IA, c'est exactement fait pour ça. »

## "Et les montants, les profils de risque ?"

« Justement — on garde en clair ce dont l'IA a besoin pour vous être utile. Les
montants en euros, par exemple&nbsp;: si vous voulez demander "est-ce que cette
allocation est cohérente avec le profil de risque du client ?", il faut les
chiffres. Donc on les conserve. En revanche, le poste dans l'entreprise — genre
"directeur marketing chez TotalEnergies" — on le masque, parce que ça permet de
reconnaître la personne. Et tout ça est réglable&nbsp;: vous décidez, type par
type, ce qui est masqué ou gardé, dans l'onglet Réglages. »

## Demo walk-through (what to show, in order)

1. Open the webapp's **Accueil** tab. Use the **built-in fictional sample** (never
   real client data on screen).
2. Click anonymise → show the **before/after side by side**. Point out a name
   becoming `⟦NOM_0001⟧`, an IBAN becoming `⟦IBAN_0001⟧`, and a **montant left in
   clear**.
3. Open **Contrôle & réglages** → show the stats ("voilà combien de fois l'outil a
   tourné, ce qui a été signalé à relire") and scroll to the **masquer/conserver**
   table. Flip one toggle, save, re-run to show it takes effect.
4. Close with the reassurance: "tout ça, sur votre machine, rien en ligne."

## Words to avoid → words to use

| Avoid (jargon) | Say instead |
|---|---|
| PII | les informations qui identifient le client |
| token | étiquette / numéro anonyme |
| hook / guard | le verrou |
| fail-closed | en cas de doute, ça bloque |
| anonymisation engine | le coffre / l'outil |
| localhost / 127.0.0.1 | sur votre ordinateur, hors ligne |
