# Seconde Main

Un moniteur d'annonces d'occasion pour la Suisse romande. Tu décris ce que tu cherches —
produit, prix, et surtout **combien de minutes de trajet** tu acceptes depuis un ou
plusieurs points de départ — et un scan tourne en arrière-plan, remplit une base de tout ce
qu'il voit, et te prévient quand quelque chose colle.

Trois choses le distinguent d'un simple scraper :

- **La distance se compte en minutes, pas en kilomètres.** « 10 min à pied depuis l'EPFL »
  et « 20 min en voiture depuis Grandson » sont deux critères valides pour la même
  recherche, et une annonce qui livre passe outre.
- **Le crawler s'annonce.** Il lit `robots.txt`, respecte les délais demandés, se nomme
  dans son User-Agent, et s'arrête quand un site refuse — il n'essaie jamais de passer
  outre. Ce que ça implique est détaillé plus bas.
- **Rien n'est jamais supprimé.** Une annonce qui disparaît est marquée, pas effacée, ce
  qui permet de tracer l'évolution des prix d'un produit dans le temps.

```bash
pip install -r requirements.txt
python3 seed_places.py     # une fois : centroïdes des codes postaux, hors ligne
python3 app.py             # http://localhost:5055
```

Puis va sur **Réglages** et branche un service d'IA en un clic. Sans IA l'app tourne quand
même, en repli mots-clés — plus grossier, mais fonctionnel.

```bash
python3 test_core.py       # 97 suites, sans réseau, sur une base jetable
```

---

## Ce qui marche, et ce qui ne marche pas

Mesuré, pas supposé. `/sources` dans l'app affiche l'état courant.

| Source | État | Détail |
|---|---|---|
| **anibis** | ✅ | `__NEXT_DATA__`, plateforme SMG |
| **tutti** | ✅ | même plateforme ; porte aussi **voitures, motos et immobilier** |
| **ricardo** | ⚠️ sitemap | 50 000 URLs publiées pour les crawlers, mais **sans prix** |
| **facebook marketplace** | ✅ | via un vrai navigateur connecté à ton compte |
| **leboncoin** | 📬 alerte | jamais crawlé — lu dans les alertes e-mail qu'il envoie |

**Les quatre sites refusés le restent.** Leurs `robots.txt` interdisent l'accès automatisé :
leboncoin s'ouvre sur une interdiction en toutes lettres et n'autorise nommément que
quelques robots, sans règle `User-agent: *`. Ce n'est pas un obstacle technique à
contourner, c'est un refus à respecter — le code les écarte explicitement
(`sources.DENIED_BY_OPERATOR`) au lieu de chercher une autre route.

### La porte que leboncoin laisse ouverte

Leur `robots.txt` est sans ambiguïté, et il n'a pas de groupe `User-agent: *` — chaque
groupe vise un robot nommé :

> It's forbidden to use search robots or other automatic methods to access Leboncoin.fr.
> Access is only permitted with special permission from Leboncoin.fr.

Ce refus tient : **aucune requête ne part vers ces sites**. Mais ils proposent leurs
**propres alertes**. Tu enregistres la recherche chez eux, tu actives la notification par
e-mail, et l'app lit ces messages en IMAP (`mailbox.py`). C'est la même donnée obtenue à
l'envers : ce n'est plus nous qui allons la chercher, c'est le site qui l'envoie, à sa
cadence et de son plein gré. Aucun `robots.txt` n'entre en jeu.

**Mot de passe d'application obligatoire**, jamais celui du compte : Gmail, iCloud et Proton
en génèrent un dédié et révocable. L'accès est en **lecture seule** — `BODY.PEEK` et
`readonly=True`, donc rien n'est marqué lu, déplacé ni supprimé. Un libellé dédié évite de
parcourir toute la boîte.

L'invariant du code a changé en conséquence : ce n'est plus « un site refusé n'a aucun
adaptateur » mais « un site refusé n'a aucun adaptateur **qui le crawle** ». Un test vérifie
que `mailbox.py` n'importe ni `crawler`, ni `browser`, ni `net`.

En pratique on perd moins qu'il n'y paraît : **tutti appartient au même groupe (SMG) que
les Scout24** et publie les mêmes catégories. `moto yamaha` y sort des BMW F800GS,
`appartement lausanne` des 3-pièces à Morges et Yverdon.

---

## Le crawler

`crawler.py` est la couche d'acquisition, séparée de l'analyse. Elle est conçue pour être
**identifiable et blocable** :

- un User-Agent stable qui donne le nom du robot et un contact ;
- `robots.txt` lu et respecté, y compris les interdictions en prose ;
- `crawl-delay` honoré, plus un délai minimum de 15 s par domaine et du jitter ;
- requêtes conditionnelles (`ETag` / `If-Modified-Since`) : une page inchangée n'est pas
  retéléchargée ;
- un 403, un 429 ou un CAPTCHA met le domaine en pause exponentielle — jamais de nouvelle
  tentative déguisée ;
- tout est journalisé dans `crawl_log`, consultable dans l'app (**Crawler**).

Ce qu'il ne fait pas, délibérément : pas de résolution de CAPTCHA, pas de navigateur
furtif, pas d'usurpation d'empreinte TLS, pas de rotation d'IP. Un site qui veut bloquer
ce robot y arrive en une ligne de `robots.txt`, et c'est voulu.

### Facebook

Seule exception : Facebook exige une session connectée. Le scan passe donc par un vrai
Chromium, avec **ton** profil, sans aucun patch de furtivité.

```bash
pip install playwright && playwright install chromium
python3 browser.py login https://www.facebook.com     # une fois, fenêtre visible
python3 browser.py check                              # état des sessions
```

Les cookies restent dans `data/browser-profile/` (jamais commité). Les scans tournent en
headless — une fenêtre visible volait le focus du Mac à chaque cycle — et un seul processus
peut ouvrir un profil, donc les accès navigateur sont sérialisés.

Les anciennes annonces sont rattrapées **au compte-gouttes** :
`FB_BACKFILL_PER_CYCLE` (5) par cycle, espacées de `FB_BACKFILL_DELAY` (20 s), les plus
récentes d'abord — soit ~24 h pour 236 annonces. Ouvrir les 236 d'un coup est exactement le
motif qui fait restreindre un compte. Une annonce sans description est marquée comme telle
pour ne pas rouvrir sa page à chaque cycle.

La description n'est **pas** dans les résultats de recherche, seulement sur la page de
l'annonce. Elle est donc lue au moment où tu ouvres la fiche, dans la même vue de page que
les images et le vendeur — trois visites séparées tripleraient l'empreinte sur ton compte
pour rien. Une annonce jamais ouverte reste sans description, et c'est voulu.

⚠️ C'est ton compte personnel : Meta peut le restreindre. `BROWSER_TARGETS_PER_CYCLE` limite
le nombre de pages vues par cycle pour cette raison.

---

## L'IA

L'app parle à n'importe quel service **compatible OpenAI**. Tout se branche depuis
**Réglages**, sans toucher `.env` ni redémarrer.

Deux modèles, deux usages :

| | appelé | à prendre |
|---|---|---|
| **tri des annonces** | des centaines de fois par jour | petit et rapide |
| **entretien de l'assistant** | 2 fois par nouvelle recherche | le meilleur possible |

L'IA sert là où les règles échouent : distinguer un iPhone d'une coque ou d'un service de
réparation, et normaliser « IPhone 13 Pro 512 go » et « Apple iPhone 13 Pro Max 512gb » vers
une même fiche produit — pour que la recherche suivante sorte de SQLite au lieu du réseau.

Le texte scrapé est traité comme **non fiable** : il est classé, jamais exécuté, jamais
utilisé pour construire une URL ou une commande, et le prompt dit au modèle d'ignorer les
instructions qu'il contiendrait.

### Brancher un service

La page Réglages détecte ce qui est déjà utilisable sur la machine (un Ollama qui tourne, un
binaire `claude` installé) et propose les autres avec un lien direct vers leur page de clés.

Un **abonnement** ChatGPT Plus, Claude Pro ou Gemini Advanced **ne donne pas accès à
l'API** — aucun bouton ne peut changer ça. Ce qui marche :

- une **clé d'API** (compte séparé, facturé à l'usage ; Google AI Studio et Groq ont un
  palier gratuit sans carte) ;
- un **modèle local** via Ollama : gratuit, hors ligne, plus lent et moins fin ;
- pour l'entretien seulement, l'**abonnement Claude Code** (voir plus bas).

### Qui fait quoi, et qui paie

Trois travaux, trois profils de coût. Le tableau de `/reglages` assigne à chacun un compte
(**principal** ou **abonnement**) et un modèle ; modèle vide = celui du compte.

Deux comptes possibles : **principal** (ta clé d'API) et **abonnement** (Claude Code).

| travail | fréquence | ce qui compte |
|---|---|---|
| tri des annonces | des centaines d'appels/jour | petit et rapide |
| traduction | un appel par lot de 5 | sorties longues |
| entretien de l'assistant | 2 appels par recherche | la qualité du modèle |

La colonne « payé par » montre ce qui servira **réellement**, pas ce qui a été coché : si le
compte choisi est indisponible, la ligne le dit et nomme le remplaçant.

L'abonnement n'est pas proposé pour le tri ni la traduction, et `ai.route()` le refuserait
même si le formulaire était contourné. Raison mesurée : un appel `claude -p` de 9 jetons a
créé **23 703 jetons de cache** — le contexte de Claude Code voyage avec chaque appel. Deux
appels par recherche, aucun problème ; des centaines par jour épuiseraient les limites de
débit en minutes.

### Abonnement Claude Code

Si le binaire `claude` est installé, l'entretien de l'assistant peut passer par `claude -p`,
son mode non interactif prévu pour être scripté. Les appels partent alors sur ton
abonnement : ni clé d'API, ni plafond entamé.

Ce n'est pas le jeton OAuth de Claude Code recyclé en clé d'API — c'est le binaire lancé tel
quel. Le texte des annonces arrive par `stdin`, jamais dans la ligne de commande, et
`--allowedTools ""` coupe tous les outils.

Vérifié en bout de chaîne : avec `authMethod: claude.ai`, un appel d'entretien passe, et la
ligne de dépense enregistrée est `claude-code/claude-sonnet-5 … cost_usd 0.0`. Ni la clé
d'API ni le crédit OpenRouter ne bougent. Le `total_cost_usd` que Claude Code affiche est
une valeur **théorique** (ce que ça aurait coûté à l'API), pas un débit.

**Uniquement l'entretien**, délibérément : 2 appels par recherche passent sans problème,
mais le tri et la traduction en font des centaines par jour — ils tomberaient sur les
limites de débit de l'abonnement en quelques minutes, et chaque appel coûte un processus
complet (~3 s) contre ~1,3 s en direct. Un test vérifie que le travail en masse ne peut pas
y accéder. Si la session a expiré, l'app le dit et repart sur la clé d'API.

### Budget

Un plafond glissant sur 365 jours (`SMART_BUDGET_USD`, 10 $ par défaut) s'applique au modèle
d'entretien. Au-delà il retombe sur le modèle bon marché plutôt que de s'arrêter. Un modèle
absent de `SMART_PRICES` est estimé au tarif le plus cher, pour qu'un identifiant inconnu
compte quand même contre le plafond.

---

## Recommandations

Le score de tri répond à « est-ce bien la chose demandée ». Une fois le lot jugé, un
quatrième travail répond à l'autre question : **parmi les annonces qui correspondent,
lesquelles sont de bonnes affaires ?**

Les signaux existent déjà — médiane du produit, écart à cette médiane, état, ancienneté et
note du vendeur, trajet, fin d'enchère, fraîcheur. Le modèle les lit et désigne **au plus
trois** annonces, chacune avec une phrase concrète, plus un résumé du lot. Il a le droit de
n'en désigner **aucune** : trente annonces médiocres doivent donner zéro recommandation,
pas trois enthousiasmes fabriqués.

Il doit aussi dire ce qui l'inquiète. Sur un jeu d'essai comprenant un vélo à 420 CHF (70 %
sous la médiane, compte vendeur créé cette année, sans note), il l'a écarté et l'a signalé
de lui-même :

> L'annonce à 420 CHF est 70 % sous la médiane mais vient d'un compte créé en 2026 sans
> note, donc à éviter sans vérification.

**Un appel par scan, et seulement si le lot a bougé.** Une empreinte des annonces et de
leurs prix est gardée : un cycle qui ne trouve rien de neuf ne coûte rien. Le modèle se
règle comme les autres dans le tableau de `/reglages` (travail « reco »). Un identifiant
qu'il inventerait est écarté, et un modèle muet n'efface jamais la recommandation
précédente.

## L'assistant

Pour « une paire de ski » ou « une moto », personne ne sait quoi taper. L'assistant pose 4 à
6 questions **fermées** (QCM, choix multiple ou oui/non — jamais de texte libre), puis en
tire une **liste de modèles concrets** — Salomon QST 99, Head Kore 99, Nordica Enforcer 100
— et cherche chacun par son nom, ce qui est bien plus précis qu'une requête floue. La liste
reste modifiable.

S'il contredit une de tes préférences explicites, il doit le **dire** : ce que tu voulais,
ce qu'il propose, pourquoi, et ce qu'il faudrait accepter pour avoir ta version. Un budget
est toujours un **plafond**, jamais un plancher.

Tes réponses (taille, niveau, sexe) sont réutilisées silencieusement d'une recherche à
l'autre, et entièrement consultables, corrigeables et effaçables dans **Profil**.

---

## Livrable ou pas

Trois états, pas deux : **livrable**, **retrait sur place**, et **non précisé**. anibis,
tutti et facebook ne publient aucune information de livraison — leurs annonces sont donc
« non précisé », et non « retrait sur place » comme elles l'affichaient jusqu'ici. Afficher
une certitude qu'on n'a pas est pire que d'admettre l'ignorance : 1030 annonces
affirmaient un retrait que personne n'avait annoncé. Le sitemap ricardo est dans le même
cas et ne prétend plus rien.

⚠️ Côté filtre distance, « non précisé » compte comme non livrable : une annonce lointaine
dont on ignore la livraison ne passe pas. C'est le choix prudent — mais il veut dire qu'une
annonce livrable non déclarée reste invisible si elle est hors rayon.

Un filtre **livraison** est disponible sur la page de résultats — c'est la seule chose qui
compte quand le vendeur est à 600 km. Le drapeau vient de l'API du site (`shippable` chez
leboncoin, `face_to_face` pour un retrait sur place).

**En Suisse, le silence vaut « livrable ».** Un vendeur qui refuse d'expédier le dit
(« nur Abholung ») ; l'inverse va sans dire. anibis, tutti, ricardo et facebook appliquent
donc ce défaut, et le texte du vendeur le corrige quand il s'exprime. leboncoin en est
exclu : son API répond, et une supposition n'a pas à écraser une réponse.

⚠️ Conséquence à connaître : `distance_ok` laisse passer une annonce livrable **quelle que
soit la distance**. Avec ce défaut, le filtre trajet ne s'applique donc plus aux sources
suisses — ce qui est cohérent pour de la vente par colis, mais retire le garde-fou si tu
cherches quelque chose à récupérer en main propre. Un « nur Abholung » explicite le remet.

**Quand le site ne dit rien, le vendeur, lui, le dit souvent.** anibis, tutti et facebook
ne publient aucun champ de livraison — mais 92 des 793 descriptions annonçaient l'envoi en
toutes lettres. Le texte est donc lu, en **allemand, français et italien** :
« Versand möglich », « Postversand », « Envoi possible », « spedizione possibile » →
livrable ; « nur Abholung », « pas d'envoi », « solo ritiro » → retrait. Deux affirmations
contradictoires dans la même annonce → non précisé, plutôt qu'un pile ou face.

Deux pièges symétriques, tous deux vérifiés sur des annonces réelles : mentionner le
retrait n'exclut pas l'envoi (« Abholung in Zürich **oder Versand** gegen Aufpreis »), et un
« kein » devant autre chose ne nie pas l'envoi (« **Kein Umtausch.** Versand möglich »).

Quand le site **sait**, il fait foi : sur leboncoin, `shippable` prime, et le texte du
vendeur peut le **rabattre**, jamais le lever : « remise en main propre
uniquement », « pas d'envoi », « je n'envoie pas ». Une simple mention ne suffit pas —
« envoi possible **ou** remise en main propre » reste livrable, et c'est le cas le plus
fréquent. Confondre les deux coûterait toutes les annonces françaises livrables, puisque
`distance_ok` ne laisse passer une annonce lointaine **que** si elle est marquée livrable.

## Traductions

Chaque annonce qui entre en base est traduite en **français et en anglais**, et
l'**original est toujours conservé** : il reste dans `listings`, les traductions vivent à
côté dans `listing_i18n`. Le sélecteur de langue est dans la barre du haut et vaut partout.
15 langues sont proposées ; une langue demandée est produite une fois puis gardée.

Toutes les langues demandées sortent du même appel, par lots de 5 annonces — ouvrir une
annonce en italien complète donc aussi le français et l'anglais s'ils manquaient. Marques,
modèles, références et nombres ne sont pas traduits, mais le mot d'unité qui les accompagne
l'est : « 65 Zoll » → « 65 pouces ».

Le **matching n'utilise jamais les traductions** : les filtres et le score lisent le texte
publié. Une traduction est un affichage, jamais un critère.

---

## Trajet en minutes

Géocodage **hors ligne** via les centroïdes de codes postaux GeoNames (`seed_places.py`) —
pas d'appel réseau par annonce. Un préfiltre à vol d'oiseau écarte l'évident, puis OSRM
affine ce qui est près de la limite. Les horaires de transports publics viennent de
`transport.opendata.ch`.

Les vitesses et facteurs de détour sont dans `config.py` : si les estimations te semblent
fausses pour ta région, c'est là que ça se règle.

---

## Interface

Cinq onglets : **Accueil**, **Catalogue**, **Favoris**, **Assistant**, **Réglages**. Ce qui
se consulte rarement — Connexions, Sources, Mes données, Crawler, Mot de passe — vit sous
Réglages, qui les liste en tête de page. Les pages existent toujours et gardent leurs URL.


Conçue pour le téléphone d'abord : les styles de base visent un petit écran, les points de
rupture sont tous en `min-width`, et les grands écrans ajoutent des colonnes plutôt que de
corriger. Sur téléphone la barre du haut tient sur **une** rangée — marque, bouton, menu —
là où les huit onglets en occupaient trois, et les tableaux d'annonces deviennent des
cartes empilées : en tableau il fallait défiler horizontalement pour voir le prix.

Les écarts entre navigateurs viennent surtout de Safari, et sont corrigés à la source :

- `border-collapse:separate` — avec `collapse`, Safari n'applique **pas** `position:sticky`
  à un `<th>` et l'en-tête part au défilement ;
- `appearance:none` sur boutons et listes déroulantes, dont Safari ignore une partie du
  style, avec une flèche redessinée ;
- champs à **16 px** : en dessous, Safari iOS zoome sur le champ au tap et ne redézoome
  jamais ;
- bague de focus en `box-shadow` plutôt qu'`outline`, dont Safari ne suit pas toujours le
  `border-radius` ;
- styles de survol sous `@media(hover:hover)`, sinon l'état reste collé après un tap ;
- pas de `<details>` pour le menu : replié, il masque son contenu par `content-visibility`,
  que ni `display:contents` ni aucune règle d'auteur portable ne peut annuler — les onglets
  disparaissaient sur grand écran. Une case à cocher masquée fait le même travail, en CSS
  pur, partout pareil.

Un test vérifie ces points, plus le fait qu'aucune page ne déborde horizontalement en
375 px de large.

**Retour immédiat au clic.** Les pages sont rendues côté serveur : entre le clic et le
premier octet, il ne se passait rien à l'écran. Une barre fine démarre au clic et se
termine au chargement, et un bouton déjà envoyé se désactive — mais après l'envoi, sinon sa
valeur ne part pas. Elle se coupe sous `prefers-reduced-motion`, et s'efface au retour
arrière depuis le cache.

**L'assistant, mesuré.** Questions ~9 s, construction de la recherche ~35 s par la clé
d'API et ~57 s par l'abonnement — le binaire `claude` traîne son contexte à chaque appel.
L'écran d'attente affiche donc l'**étape réelle** rapportée par le job, le temps écoulé
compté par le serveur, et la durée habituelle, pour qu'une attente normale ne passe pas
pour une panne. La barre sature à 92 % : elle ne prétend jamais avoir fini avant que ce
soit vrai, et un échec s'affiche au lieu de laisser tourner le spinner.

Avant, la liste d'étapes se cochait toute seule toutes les 3 s. Sur 45 s d'attente, un faux
progrès est pire que pas de progrès.

**Ce qui rendait l'app lente.** `/reglages` mettait **1,3 s** : elle lançait
`claude auth status` — un processus, ~350 ms — deux fois par rendu, pour une information
qui ne change qu'à la connexion. Mise en cache une minute, invalidée dès qu'une connexion
aboutit, et préchauffée au démarrage : **4 ms**. Les autres pages étaient déjà entre 5 et
23 ms.

## Homelab (Docker)

```bash
docker compose up -d --build
# puis http://<ton-serveur>:5055 -> /compte : crée le premier compte
```

Le premier compte créé est **administrateur** : lui seul règle l'IA, les clés et
les autres comptes. Tant que la table `users` est vide, l'app reste ouverte —
c'est la création du compte qui allume le verrou. Crée-le **avant** d'exposer le
port.

Trois points qui comptent :

- **`./data` doit être monté.** Base, profil navigateur et journaux y vivent ;
  sans volume, un `--build` efface tes recherches.
- **Le conteneur écoute sur `0.0.0.0`, l'app en local sur `127.0.0.1`.** C'est
  `HOST` qui décide, et c'est docker qui publie le port. Rien n'est exposé par
  accident.
- **Facebook ne fonctionne pas en conteneur.** Il lui faut un Chromium avec *ta*
  session, qui ne se met pas dans une image. Les autres sources tournent
  normalement ; laisse la recherche Facebook active sur ta machine, ou accepte
  de perdre cette source côté serveur.

L'image tourne sans root, embarque un `HEALTHCHECK` sur `/api/status`, et
`.dockerignore` tient `data/` et `.env` hors de l'image.

## Plusieurs comptes

Chaque compte a ses **recherches**, ses **favoris** et son **profil**. Ce qui
reste commun : les annonces vues, les fiches produit et l'historique des prix —
c'est un catalogue partagé, et le dupliquer par utilisateur multiplierait le
scan et la dépense IA sans rien apporter.

Les réglages d'IA, les clés et les comptes sont **réservés à l'administrateur** :
il n'y a qu'un budget et qu'une clé, et il faut bien que quelqu'un en réponde.
Le dernier administrateur ne peut pas être supprimé.

Ce qui existait **avant** les comptes n'a pas de propriétaire et reste visible
par tout le monde : la migration n'efface ni ne réattribue rien. Assigne-le à la
main si tu veux le cloisonner :

```sql
UPDATE searches SET user_id = 1 WHERE user_id IS NULL;
```

## Mot de passe

L'app est **ouverte par défaut** : elle écoute sur `127.0.0.1`. Créer un compte dans
**/compte** est ce qui allume le verrou — tant que la table `users` est vide, rien ne change.
Un seul compte, pas d'e-mail, pas de récupération : vider `users` rouvre l'accès.

Le mot de passe n'est pas stocké, seulement son empreinte PBKDF2-SHA256 (200 000 tours, sel
par compte). La clé de signature des cookies est tirée au hasard et gardée en base. Les
routes `/api/*` répondent **401** au lieu de rediriger, pour que le rafraîchissement
automatique ne reçoive pas une page de connexion.

---

## Ajouter un site

Voir **[SOURCES.md](SOURCES.md)** : le contrat d'un adaptateur, le dict d'une
annonce, les trois voies réseau, et les pièges déjà rencontrés.

## Fichiers

Un paquet par sujet, un fichier par feature. Chaque `__init__.py` ré-exporte sa
surface publique, donc `import ai`, `import engine`, `import sources` continuent de
marcher tels quels.

| | |
|---|---|
| `app.py` | 12 lignes : construit l'app et lance la boucle |
| `web/` | l'interface — `searches` · `items` · `assistant` · `admin` · `api` · `helpers` |
| `sources/` | un fichier par site — `smg` (anibis+tutti) · `ricardo` · `facebook` · `mail` · `registry` · `util` |
| `ai/` | `client` (comptes, routage, appel) · `budget` · `classify` · `assistant` · `reco` · `checks` |
| `engine/` | `match` · `store` · `health` · `scan` · `checks` |
| `crawler.py` | acquisition identifiée : robots.txt, délais, cache conditionnel |
| `browser.py` | Chromium connecté, pour Facebook uniquement |
| `mailbox.py` | alertes e-mail IMAP (leboncoin) |
| `i18n.py` · `settings.py` · `auth.py` · `geo.py` · `db.py` · `net.py` · `reference.py` · `sellers.py` | un sujet chacun |
| `test_core.py` | 97 suites, sans réseau |

`web/_router.py` mérite un mot : découper en blueprints Flask aurait renommé tous
les endpoints (`url_for('index')` → `url_for('searches.index')`), cassant les
gabarits et la navigation. Ce collecteur laisse `@app.route(...)` inchangé et
rejoue les routes sur la vraie app, en gardant le nom de la fonction.

**Piège du découpage :** réassigner un nom ré-exporté (`ai.chat = stub`) ne change
que ce nom. Les appelants résolvent le leur dans leur propre module — viser
`ai.budget.chat`, `engine.scan.judge`, `sources.registry.probe_market`.

Pas d'ORM : les requêtes sont courtes et écrites à la main. `db.MIGRATIONS` ajoute les
colonnes venues après coup — SQLite n'a pas de `ADD COLUMN IF NOT EXISTS`.

`db._guard` refuse un `DELETE FROM` global sur `searches`, `listings`, `matches` ou
`targets` : un script de vérification a déjà vidé la base deux fois.

---

## Limites assumées

- Le repli sans IA est grossier : il note 100 un autoradio dont la description dit
  « compatible iPhone ».
- Ricardo passe par son sitemap, donc **sans prix** : les annonces sont cataloguées mais ne
  peuvent pas être filtrées par prix.
- Les transports publics sont estimés par défaut, routés seulement quand
  `transport.opendata.ch` répond.
- Un scan se fait en deux temps : la phase 1 écrit tout de suite chaque candidat avec un
  score mots-clés, la phase 2 (`engine.judge`) le remplace par un vrai verdict. Si le
  processus meurt entre les deux, `engine.finish_pending()` reprend au cycle suivant ce qui
  est resté provisoire depuis plus de 10 min. C'est nécessaire : redémarrer pendant un scan
  avait laissé **139 des 271 matchs** figés en « analyse en cours… », dont un foil Armstrong
  dans une recherche de sac à dos — accroché au seul mot « V2 » de « Peak design 30l V2 ».
- Scan séquentiel, mono-processus. À trois ou quatre recherches c'est instantané ; au-delà,
  il faudrait paralléliser par site.
- La clé d'API est en clair dans `data/market.db`, sur ta machine — comme elle l'était dans
  `.env`.
