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
python3 test_core.py       # 72 suites, sans réseau, sur une base jetable
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
| leboncoin | ⛔ | `robots.txt` l'interdit — voir ci-dessous |
| autoscout24 / motoscout24 / immoscout24 | ⛔ | idem |

**Les quatre sites refusés le restent.** Leurs `robots.txt` interdisent l'accès automatisé :
leboncoin s'ouvre sur une interdiction en toutes lettres et n'autorise nommément que
quelques robots, sans règle `User-agent: *`. Ce n'est pas un obstacle technique à
contourner, c'est un refus à respecter — le code les écarte explicitement
(`sources.DENIED_BY_OPERATOR`) au lieu de chercher une autre route.

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

### Deux comptes, bascule automatique

Le bloc « compte de repli » prend un **second** service avec sa propre clé. Quand le
principal n'a plus de jetons — 401, 402 ou 429 — l'app bascule dessus au sein du même appel
et met l'épuisé de côté un quart d'heure, au lieu de retomber sur le tri par mots-clés.

Seul OpenRouter publie son solde (`/credits`, affiché sur la page). Ailleurs la bascule se
déclenche au premier appel refusé. Le registre `ai_spend` ne suffit pas : il ne compte que
le modèle d'entretien, et annonçait 0,36 $ dépensés quand le compte en avait consommé 0,82 $.

### Connexions

L'onglet **Connexions** rassemble tout ce à quoi l'app doit être reliée — comptes d'IA,
abonnement Claude Code, sessions de sites — avec l'état de chacun et un bouton pour le
réparer. Une session expirée s'y voit et s'y répare sans ligne de commande : le bouton
ouvre une fenêtre Terminal sur `claude auth login`, tu te connectes dans ton navigateur, et
la page se met à jour toute seule quand c'est fait.

L'app n'y touche à aucun identifiant : elle ouvre la porte, la connexion se fait dans ta
fenêtre et ton navigateur, exactement comme le bouton de session Facebook. Un test vérifie
que `cli_login` ne va lire ni trousseau ni fichier d'identifiants.

### Abonnement Claude Code

Si le binaire `claude` est installé, l'entretien de l'assistant peut passer par `claude -p`,
son mode non interactif prévu pour être scripté. Les appels partent alors sur ton
abonnement : ni clé d'API, ni plafond entamé.

Ce n'est pas le jeton OAuth de Claude Code recyclé en clé d'API — c'est le binaire lancé tel
quel. Le texte des annonces arrive par `stdin`, jamais dans la ligne de commande, et
`--allowedTools ""` coupe tous les outils.

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

## Mot de passe

L'app est **ouverte par défaut** : elle écoute sur `127.0.0.1`. Créer un compte dans
**/compte** est ce qui allume le verrou — tant que la table `users` est vide, rien ne change.
Un seul compte, pas d'e-mail, pas de récupération : vider `users` rouvre l'accès.

Le mot de passe n'est pas stocké, seulement son empreinte PBKDF2-SHA256 (200 000 tours, sel
par compte). La clé de signature des cookies est tirée au hasard et gardée en base. Les
routes `/api/*` répondent **401** au lieu de rediriger, pour que le rafraîchissement
automatique ne reçoive pas une page de connexion.

---

## Fichiers

| | |
|---|---|
| `app.py` | routes Flask (41) et rendu |
| `engine.py` | la boucle : scan → filtres → match → notification |
| `crawler.py` | acquisition identifiée : robots.txt, délais, cache conditionnel |
| `sources.py` | un adaptateur par site |
| `browser.py` | Chromium connecté, pour Facebook uniquement |
| `ai.py` | modèles, lots, bascule entre comptes, budget |
| `i18n.py` | traductions |
| `settings.py` | fournisseur / clé / modèles, réglables à chaud |
| `auth.py` | le compte et le mot de passe |
| `geo.py` | codes postaux, trajets |
| `db.py` | schéma SQLite et migrations |
| `net.py` | sortie HTTP pour les API (OpenRouter, OSRM, GeoNames) |
| `reference.py` | prix neuf de référence |
| `sellers.py` | réputation vendeur, surtout anti-arnaque sur Facebook |
| `test_core.py` | 72 suites, sans réseau |

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
- Scan séquentiel, mono-processus. À trois ou quatre recherches c'est instantané ; au-delà,
  il faudrait paralléliser par site.
- La clé d'API est en clair dans `data/market.db`, sur ta machine — comme elle l'était dans
  `.env`.
