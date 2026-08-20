# Seconde Main

Tu décris ce que tu veux acheter (produit, référence, prix, et **combien de minutes
de trajet** tu acceptes depuis un ou plusieurs points de départ). Un scan tourne en
arrière-plan toutes les 5 minutes, remplit une base de toutes les annonces vues,
et te notifie quand quelque chose colle.

```bash
pip install -r requirements.txt
python3 seed_places.py          # une fois : centroïdes de codes postaux (offline)
cp .env.example .env            # y mettre DEEPSEEK_API_KEY (optionnel)
python3 app.py                  # http://localhost:5055
```

`python3 test_core.py` → vérifie filtres, trajets, dédup et le pipeline complet, sans réseau.

## État réel des sites

Testé le 2026-08-19 depuis la Suisse. C'est le point important : la moitié des sites
que tu as cités bloquent les clients HTTP.

| Site | État | Comment |
|---|---|---|
| **leboncoin** | ✅ marche | `__NEXT_DATA__`, livre même lat/lon |
| **anibis** | ✅ marche | `__NEXT_DATA__` → `listings.edges[].node` |
| **tutti** | ✅ marche | 2.1M annonces, même plateforme qu'anibis. **Porte les voitures, motos et immobilier** — les catégories des sites bloqués |
| **ricardo** | ✅ marche | payload RSC ; code postal + livraison dans `shipping[]` ; `zip_code`+`range` filtrent **côté serveur**. CAPTCHA si on tape trop vite → espacer `POLL_SECONDS` |
| autoscout24 / motoscout24 / immoscout24 | 🔒 Cloudflare Turnstile | profil navigateur |
| facebook marketplace | 🔒 login requis | profil navigateur |

**Le vrai contournement, c'est de ne pas en avoir besoin.** tutti.ch (même groupe SMG que
les *Scout24) n'est pas protégé et publie les mêmes catégories : `moto yamaha` y sort des
BMW F800GS et des Yamaha FJ 1200, `appartement lausanne` des 3-pièces à Lausanne, Morges,
Yverdon. Pour la plupart des recherches, tutti + ricardo + anibis remplacent les sites murés.

**Les sites verrouillés ne sont pas contournés.** Le programme ne résout aucun CAPTCHA.
La voie prévue : tu ouvres une vraie fenêtre et tu passes le contrôle toi-même, une fois.

```bash
python3 browser.py login https://www.autoscout24.ch/fr
```

Les cookies restent dans `data/browser-profile/` et les scans headless les réutilisent.
Quand le contrôle revient, le scan écrit `blocked` dans le journal au lieu d'insister — relance la commande.

Pour savoir ce que ta session achète vraiment, à l'instant T :

```bash
python3 browser.py check                 # OK / BLOCKED par site
```

Ce que le login change, honnêtement :

| Site | Le login aide ? |
|---|---|
| **facebook marketplace** | **oui** — le login *est* la porte. Mais compte perso = risque réel de blocage par Meta. |
| autoscout24 / motoscout24 / immoscout24 | **non** — le mur est Turnstile, pas l'identité. Ce qui passe, c'est le cookie `cf_clearance` obtenu en résolvant le contrôle toi-même : lié à ton IP + navigateur, et il **expire** (souvent quelques heures). |
| ricardo | **non** — CAPTCHA déclenché par le rythme. Espacer `POLL_SECONDS` aide plus qu'un compte. |

Les scans tournent **en headless** : une fenêtre visible volait le focus du Mac à chaque
cycle. Facebook rend exactement les mêmes résultats headless une fois la session ouverte
(31 annonces dans les deux cas), donc seule la connexion ouvre une fenêtre.
Si un site repasse `BLOCKED` juste après un `login` réussi, essaie quand même :

```bash
BROWSER_HEADLESS=0 python3 app.py
```

Un seul processus Chromium peut ouvrir un profil donné. Les accès au navigateur sont donc
**sérialisés** (`browser._BROWSER_LOCK`) : sans ça, deux recherches qui touchaient Facebook
en même temps échouaient toutes les deux avec « profile already open ». Elles font la queue
(~6s chacune).

Les URLs de recherche de ces sites sont des paris (`BROWSER_SEARCH` dans `sources.py`) :
ouvre-en une dans ton navigateur et corrige la chaîne si elle a bougé. Une ligne à changer.

### Ricardo : rayon côté serveur

Les paramètres d'URL viennent de [ricardify](https://github.com/kferrari/ricardify) (GPL-3.0,
dernier commit 2020). Son code ne tourne plus — `urlopen` sans en-têtes, et des sélecteurs
CSS hashés de 2020 — mais ses paramètres, eux, sont toujours honorés :

```
?zip_code=1003&range=20&item_condition=used
```

`range` est un rayon en km : Ricardo filtre avant de répondre. L'adaptateur déduit le code
postal du premier point de départ (`geo.nearest_postcode`) et convertit les minutes en km.
Aucun code n'a été repris — seulement les noms de paramètres, qui sont des faits sur le site.

## Ce que l'IA fait (et ne fait pas)

Sans clé tout fonctionne, en repli mots-clés. Pour brancher une IA **gratuite** :

1. Crée un compte et une clé (gratuit, sans carte) chez l'un des trois :
   - **OpenRouter** → https://openrouter.ai/keys (modèles suffixés `:free`)
   - **Google AI Studio** → https://aistudio.google.com/apikey
   - **Groq** → https://console.groq.com/keys
2. Colle-la dans `.env` (le fichier n'est jamais commité) :

```bash
AI_PROVIDER=openrouter
AI_API_KEY=sk-or-...
```

3. Vérifie :

```bash
python3 ai.py test
```

Le test confirme la clé, **liste les modèles réellement disponibles** si celui par défaut a
été retiré, puis classe trois annonces piège — un vrai iPhone, un service de réparation,
une coque — et dit si le tri est correct.

Presets dans `config.py` (`AI_PROVIDER=`) : `openrouter`, `gemini`, `groq`, `deepseek`,
`ollama`, **`anthropic`**, **`openai`**.

### Ton propre compte — page **Réglages**

Tout se règle depuis `/reglages`, sans toucher `.env` ni redémarrer : le service, l'adresse
de l'API, la clé, et **les deux modèles séparément** — celui qui trie les annonces (appelé
des centaines de fois par jour, à prendre petit) et celui qui mène l'entretien de
l'assistant (2 appels par recherche, c'est là qu'un bon modèle change le résultat). La
liste déroulante est remplie par un vrai `GET /models` : ce sont les modèles que *ta* clé
atteint, pas une liste écrite en dur. « Enregistrer et tester » fait un appel minuscule et
te dit exactement ce qui cloche si ça ne marche pas.

Choisir `anthropic` ou `openai` te fait passer par **ton compte chez eux**, facturé par eux,
sans intermédiaire — l'app ne parle qu'à `/chat/completions`, que les deux exposent.
Un champ laissé vide garde la valeur actuelle : la clé ne s'efface pas par accident, et la
page ne la réaffiche jamais (seulement `sk-or-v1…4f21`). Elle est stockée en clair dans
`data/market.db`, sur ta machine, exactement comme `.env` l'était — « Oublier la clé »
la retire et rend la main à `.env`.

### Ton abonnement Claude Code

Si le binaire `claude` est installé, `/reglages` peut faire passer **l'entretien de
l'assistant** par `claude -p`, le mode non interactif prévu pour être scripté. Les appels
partent alors sur ton **abonnement** : ni clé d'API, ni plafond entamé.

Ce n'est pas le jeton OAuth de Claude Code recyclé en clé d'API — c'est le binaire lancé
tel quel. Le texte des annonces arrive par `stdin`, jamais dans la ligne de commande, et
`--allowedTools ""` coupe tous les outils : le processus ne peut ni lire ni écrire de
fichier, quoi que contienne l'annonce.

**Uniquement l'entretien**, délibérément : 2 appels par recherche passent sans problème,
mais le tri et la traduction en font des centaines par jour. Les y envoyer taperait dans
les limites de débit de l'abonnement en quelques minutes, et chaque appel coûte un
processus complet (~3 s) contre ~1,3 s en direct. Le travail en masse reste donc sur la
clé d'API. Si la session `claude` a expiré — `claude` dans un terminal pour se
reconnecter — l'app le dit, met l'abonnement de côté un quart d'heure et repart sur la
clé : rien ne casse.

### Deux comptes, bascule automatique

Le bloc « compte de repli » de `/reglages` prend un **second** service avec sa propre clé et
son propre modèle. Quand le principal n'a plus de jetons — 401, 402 ou 429 — l'app bascule
dessus toute seule et continue, au lieu de retomber sur le tri par mots-clés ; le compte
épuisé est mis de côté un quart d'heure puis retenté. C'est ce qui permet de mettre ton
compte Anthropic en principal et de garder OpenRouter derrière : tant qu'il reste des
jetons chez le premier, c'est lui qui sert.

Seul OpenRouter publie son solde (`/credits`, affiché sur la page). Chez les autres la
bascule se déclenche donc au **premier appel refusé**, pas sur une estimation — et le
registre `ai_spend`, qui ne compte que le modèle d'entretien, ne suffisait pas : il
annonçait 0.36 $ dépensés quand le compte en avait réellement consommé 0.82 $.

Un modèle absent de `SMART_PRICES` est estimé au tarif le plus cher : un identifiant
inconnu compte quand même contre le plafond annuel au lieu de passer pour gratuit.

Les modèles gratuits sont **souvent rate-limités (429)** : `AI_FALLBACKS` en essaie
plusieurs dans l'ordre, le premier qui répond gagne. Les réponses font ~250 tokens par
annonce, donc les lots sont de **8** (`ai.BATCH`) et un lot tronqué est rejoué en deux
moitiés — sinon le JSON revient coupé et illisible, silencieusement.
Le roster gratuit d'OpenRouter bouge souvent : `deepseek-chat-v3-0324:free` a déjà disparu,
le défaut est maintenant `google/gemma-4-31b-it:free`. Si un modèle refuse le mode JSON,
la requête est rejouée sans — le prompt exige déjà du JSON.

L'IA sert là où les règles échouent :

- **distinguer l'objet de ses accessoires.** Sur « iphone 13 », anibis renvoie une coque,
  une protection d'écran et un *service de réparation*. Le filtre mots-clés confond ;
  l'IA lit l'annonce.
- **normaliser** « IPhone 13 Pro 512 go » et « Apple iPhone 13 Pro Max 512gb » vers une
  même fiche produit, pour que la recherche suivante sorte de SQLite au lieu du réseau.

Coût maîtrisé : appels **groupés par 20**, cache par hash de contenu (jamais deux fois la
même annonce), et l'IA ne voit que ce qui a déjà passé les filtres gratuits
(prix → distance → IA). En pratique quelques centimes par jour chez DeepSeek.

Le texte scrapé est traité comme **non fiable** : il est classé, jamais exécuté, et le
prompt dit au modèle d'ignorer les instructions qu'il contiendrait.

## Traductions

Une annonce tutti en allemand ou un vendeur tessinois sont illisibles pour la moitié des
acheteurs. Chaque annonce qui entre en base est donc traduite en **français et en anglais**
dès le cycle suivant (`i18n.backlog()`, sous `AI_TRANSLATE_BUDGET`), et l'**original est
toujours conservé** : il reste dans `listings.title/description`, les traductions vivent à
côté dans `listing_i18n`. La fiche d'une annonce affiche les langues en pastilles, plus un
menu « + autre langue » pour les 15 proposées — une langue demandée est produite une fois
puis gardée, et le choix suit la session.

Le choix de langue est dans la barre du haut et vaut **partout** : listes, résultats,
favoris, catalogue, pas seulement la fiche. Une annonce sans traduction reste affichée dans
sa langue d'origine plutôt que de disparaître, et « toujours l'originale » revient à l'état
initial en un clic. Le **matching n'utilise jamais les traductions** : les filtres et le
score continuent de lire le texte publié, la traduction est un affichage.

Une traduction ne coûte qu'un appel : les langues demandées sortent toutes du même, par
lots de 5 annonces. Ouvrir une annonce en italien complète donc aussi le français et
l'anglais s'ils manquaient, gratuitement. Le prompt interdit de traduire marques, modèles,
références et nombres, mais traduit le mot d'unité qui les accompagne : « Samsung QLED
QE65Q85R 65 Zoll » devient « … 65 pouces », pas « … 65 inches » dans la version française.
Sans clé, ou si l'appel échoue, la fiche affiche l'annonce telle qu'elle a été publiée.

## Trajet en minutes

Une annonce passe si **au moins un** point de départ convient (`10min à pied de Lausanne`
**ou** `20min en voiture de Grandson`). Les annonces livrées sautent le filtre distance.

Deux étages, pour ne pas payer une requête par annonce : estimation à vol d'oiseau
(gratuite) puis routage réel **OSRM** seulement pour ce qui approche la limite.
Les boutons de réglage sont en haut de `config.py` :

```python
MODE_SPEED_KMH = {"foot": 4.5, "bike": 15.0, "car": 55.0, "transit": 28.0}
DETOUR_FACTOR  = {"foot": 1.25, "bike": 1.3, "car": 1.35, "transit": 1.45}
REFINE_BAND    = 1.6     # affine via OSRM en dessous de limite × 1.6
```

`transit` reste une estimation : aucun routage transports gratuit sans clé API.
Si les temps en transports te paraissent faux, c'est `MODE_SPEED_KMH["transit"]` qu'il faut bouger.

Le géocodage est **offline** (GeoNames, 56k codes postaux CH+FR) : Nominatim bannit vite
si on lui envoie chaque annonce. Nominatim ne sert que pour les lieux nommés que tu tapes
(« EPFL »), et si sa réponse manque, « EPFL, Lausanne » retombe sur Lausanne.

## Base de données — ce que je te propose

`listings` garde **toutes** les annonces vues, pas seulement les matchs, plus le payload
d'origine (`raw`) pour pouvoir ré-extraire sans re-scraper.

`products` est la fiche canonique générée par l'IA. Ce qu'elle contient déjà :

| Champ | Pourquoi |
|---|---|
| `norm_key` | slug unique : la recherche suivante tape SQLite, pas le réseau |
| `canonical_name`, `brand`, `model`, `variant` | dédoublonne les formulations vendeur |
| `category`, `release_year`, `msrp` | contexte |
| `specs` (JSON) | par catégorie, sans multiplier les tables |
| `price_p25` / `price_median` / `price_p75`, `n_listings` | **dit tout de suite si c'est une bonne affaire** — chaque match affiche son écart à la médiane |

Les champs par catégorie vivent dans `listings.attrs` (JSON) :

- **voiture / moto** : `year, mileage_km, fuel, transmission, power_hp, doors`
- **immobilier** : `rooms, living_area_m2, floor, year_built, deal(rent|sale)`
- **téléphone / ordi** : `storage_gb, ram_gb, color, screen_in, battery_health`

À toi de compléter : la liste est dans le prompt `SYSTEM` de `ai.py`, une ligne par catégorie.

## Fichiers

L'IA catalogue **toutes** les annonces vues, pas seulement les matchs : `engine.enrich_backlog()`
tourne à chaque cycle sur ce que le filtre distance a écarté, sous budget (`AI_ENRICH_BUDGET`).
C'est ce qui remplit `products` et donc les médianes de prix.

`app.py` routes · `engine.py` boucle scan→filtre→match→notif · `sources.py` adaptateurs ·
`crawler.py` acquisition identifiée (robots.txt, crawl-delay, cache conditionnel) ·
`browser.py` tier navigateur · `ai.py` modèle bon marché + entretien · `i18n.py` traductions ·
`settings.py` fournisseur/clé/modèles réglables à chaud · `geo.py` trajets · `db.py` schéma ·
`net.py` sortie HTTP pour les API (OpenRouter, OSRM, GeoNames), sans usurpation d'empreinte.

## Mot de passe

L'app est **ouverte par défaut** : elle écoute sur `127.0.0.1`, sur ta machine. Créer un
compte dans `/compte` est ce qui allume le verrou — tant que la table `users` est vide, rien
ne change. Un seul compte, pas d'e-mail, pas de récupération : vider `users` rouvre l'accès.

Le mot de passe n'est pas stocké, seulement son empreinte PBKDF2-SHA256 (200 000 tours,
sel par compte) — deux comptes avec le même mot de passe n'ont pas la même empreinte. La
clé de signature des cookies est tirée au hasard et gardée en base ; elle était écrite en
dur dans le code, ce qui, avec un login, aurait laissé fabriquer un cookie de session.
Les routes `/api/*` répondent **401** au lieu de rediriger, pour que le rafraîchissement
automatique ne reçoive pas une page de connexion qu'il ne sait pas lire.

## Limites assumées

- Repli mots-clés grossier sans clé IA : il note 100 un autoradio dont la description dit « compatible iPhone ».
- `transit` estimé, pas routé.
- Scan séquentiel, mono-processus. À 3-4 recherches c'est instantané ; au-delà, paralléliser par site.
- Usage personnel : les CGU de ces sites interdisent en général le scraping automatisé.
