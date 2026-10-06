# Brancher un nouveau site

> **Où se trouve quoi** — le code est découpé en paquets, un fichier par sujet :
> `sources/` (un par site), `web/` (un par domaine de pages), `ai/`, `engine/`.
> Chaque `__init__.py` ré-exporte tout, donc `import sources` marche comme avant.
>
> **Un piège à connaître :** réassigner un nom ré-exporté ne change que ce
> nom-là. `ai.chat = mon_stub` ne touche pas ce que `smart_chat` appelle, parce
> que `smart_chat` résout `chat` dans **son** module. Pour remplacer une
> fonction, viser le module qui la résout : `ai.budget.chat = mon_stub`.


Tout ce qu'il faut savoir pour ajouter une source. Un adaptateur, c'est **une
fonction** ; tout le reste — filtres, distance, IA, dédoublonnage, traduction,
notifications — est déjà là et s'applique tout seul.

---

## 1. Le contrat

```python
@adapter("mon_site")
def mon_site(query, spec=None):
    """query : le texte cherché. spec : la ligne `searches` (dict-like) ou None.
       Retourne une liste de dicts. Une liste vide est une réponse valable."""
    return [...]
```

- `sources.py` contient le décorateur `@adapter(nom)`, qui inscrit la fonction
  dans `ADAPTERS`. Rien d'autre à déclarer : le nom devient utilisable partout
  (recherches, page Sources, filtres).
- `spec` porte `query`, `reference`, `price_min`, `price_max`, `condition_min`,
  `seller_type`, `exclude_kw`, `origins`, `sources`. Tu peux l'ignorer : les
  filtres sont réappliqués après coup de toute façon.
- **Ne lève pas d'exception pour dire « rien trouvé »** — retourne `[]`.
  `sources.search()` attrape les exceptions et les affiche en `error`, ce qui
  déclenche un backoff et une notification de panne.

## 2. Le dict d'une annonce

Seul `url` est obligatoire — c'est la clé d'unicité. Tout le reste est
facultatif, et **mieux vaut `None` qu'une valeur inventée** : une annonce sans
prix s'affiche « prix inconnu », une annonce avec un faux prix pollue les
médianes du catalogue pour de bon.

| champ | type | note |
|---|---|---|
| `url` | str | **obligatoire**, unique en base |
| `source` | str | le nom de ton adaptateur |
| `source_id` | str | identifiant chez eux |
| `title` | str | ce sur quoi le matching travaille |
| `description` | str | |
| `price` | float | en unités, pas en centimes |
| `currency` | str | `CHF`, `EUR`… |
| `price_type` | str | `fixed` / `auction` / `negotiable` / `free` |
| `category` | str | libre ; l'IA normalise ensuite |
| `condition` | str | `new` / `like_new` / `good` / `fair` / `parts` |
| `seller_type` | str | `private` / `pro` — `None` si le site ne le dit pas |
| `seller_name`, `seller_key` | str | `seller_key` sert à regrouper les annonces d'un même vendeur |
| `location_raw` | str | tel qu'affiché |
| `postal_code`, `country` | str | **le code postal suffit** : les coordonnées sont résolues hors ligne |
| `lat`, `lon` | float | laisse `None`, `geo.py` s'en charge |
| `shipping` | 0/1 | 1 = livrable, ce qui court-circuite le filtre distance |
| `shipping_cost` | float | |
| `image` | str | vignette |
| `images` | str | **JSON**, pas une liste : `json.dumps([...])` |
| `posted_at` | float | epoch |
| `auction_end` | float | epoch ; active l'affichage du compte à rebours |
| `bids` | int | |
| `attrs` | str | **JSON** — tout ce qui est propre à la catégorie |
| `raw` | str | **JSON** du payload d'origine, tronqué à 20 000 |

Les champs absents de cette liste sont ignorés en silence (`engine.COLS`).

## 3. Ce que tu récupères gratuitement

Une fois l'adaptateur inscrit, sans une ligne de plus :

- **filtres** prix, mots-clés exclus, type de vendeur (`engine.passes_rules`) ;
- **distance en minutes** depuis chaque point de départ, à pied / vélo / voiture /
  transports (`engine.distance_ok`) — il te suffit d'avoir mis un code postal ;
- **dédoublonnage** entre sites (`engine.dup_key` : titre normalisé + prix + NPA) ;
- **tri par IA** puis fiche produit et médiane de prix ;
- **traduction** en français et anglais ;
- **cycle de vie** : une annonce disparue est marquée, jamais supprimée, ce qui
  alimente l'historique des prix ;
- **santé** : la page Sources dit *pourquoi* c'est vide, avec backoff exponentiel
  et notification quand une source qui marchait tombe.

## 4. Comment sortir sur le réseau

Trois voies, selon ce que le site permet. **Choisis d'abord, code ensuite.**

### a) `crawler.get(url)` — un site qui accepte les robots

```python
import crawler
r = crawler.get("https://www.exemple.ch/recherche?q=" + quote_plus(query))
if r is None:
    return []          # refusé ou en retrait : crawler l'a déjà journalisé
```

`crawler.get` lit `robots.txt`, respecte `crawl-delay` (plancher 15 s), fait des
requêtes conditionnelles, et met le domaine en retrait exponentiel sur 403/429/
CAPTCHA. Rien à gérer côté adaptateur. Ajoute le domaine à la liste de
`app.crawler_page()` pour qu'il apparaisse sur la page d'audit.

### b) `net.get` / `net.post_json` — une API officielle

Pour un point d'accès prévu pour être appelé, il n'y a pas de `robots.txt` qui
tienne. Passe par `net`, pas par `crawler` (`net.post_form` existe pour OAuth).

### c) `mailbox.py` — un site qui refuse le crawl mais envoie des alertes

Ajoute une entrée dans `mailbox.SITES` :

```python
"mon_site": {"from": ("mon-site.fr",),
             "link": r"https?://(?:www\.)?mon-site\.fr/annonce/(\d{6,})"},
```

L'adaptateur est créé automatiquement à partir de cette entrée. C'est la voie
utilisée pour leboncoin.

## 5. Statuts et santé

`sources.LAST_STATUS[nom] = (statut, détail)` pilote la page Sources. Statuts
reconnus : `ok`, `empty`, `blocked`, `login`, `error`. Si tu ne dis rien,
`sources.search()` déduit : des lignes → `ok`, rien → `empty` (ou `blocked` si
`net.LAST_BLOCKED` a été levé).

Renseigne-le toi-même quand tu sais mieux :

```python
if not creds:
    LAST_STATUS["mon_site"] = ("login", "identifiants absents")
    return []
```

La différence compte : `blocked | login | error` déclenchent un backoff,
`empty` non — une source qui renvoie légitimement zéro ne doit pas être mise en
pause.

## 6. Vérifier

```python
def demo():
    rows = mon_site("velo")          # ou un payload figé, sans réseau
    assert all(r["url"] for r in rows)
    assert all(r.get("price") is None or r["price"] > 0 for r in rows)
```

```bash
python -m seconde_main.sources   # les self-checks de tous les adaptateurs
python tests/test_core.py        # la suite complète, sans réseau
```

Puis dans l'app : page **Sources** → *Vérifier* lance l'adaptateur pour de vrai
et écrit le verdict.

## 7. Cas concret : remplacer une source par un client d'API

Si tu écris un client leboncoin, quatre points de contact — et rien d'autre :

1. **`sources/leboncoin.py`** avec `@adapter("leboncoin")`. Ne passe pas par
   `crawler.py` : un point d'accès prévu pour être appelé n'a pas de
   `robots.txt` qui tienne.
2. **`sources/__init__.py`** : ajoute `from .leboncoin import leboncoin`, sinon
   le module n'est jamais chargé et l'adaptateur ne s'inscrit pas.
3. **Collision de nom.** `sources/mail.py` inscrit déjà `leboncoin` à partir de
   `mailbox.SITES`. Deux adaptateurs ne peuvent pas porter le même nom : soit
   tu retires l'entrée de `mailbox.SITES`, soit tu renommes l'une des deux.
   L'ordre d'import décide sinon en silence, ce qui est pire.
4. **`DENIED_BY_OPERATOR`** (dans `sources/mail.py`) contient `leboncoin`, et
   `sources.demo()` vérifie qu'un site refusé n'a qu'un adaptateur e-mail.
   Retire l'entrée si tu as une voie autorisée — sinon l'auto-vérification
   échoue, et la page Crawler continue d'affirmer que le site n'est jamais
   contacté alors que c'est faux.

**Ne filtre pas par géographie côté site, sauf preuve du contraire.** C'est
tentant — l'API prend un rayon, l'app a des origines — et c'est presque toujours
une perte nette :

- une recherche **sans origine** accepte tout (`distance_ok` renvoie `True`
  d'emblée) : le rayon ne filtre alors rien d'utile ;
- **`shipping_ok` court-circuite la distance.** Une annonce livrable à 500 km est
  un match valide. Un rayon de 20 min la jette, et rien ne le signale ;
- quand il y a des origines, `geo.best_origin` calcule le **vrai temps de trajet**
  par mode, affiné par OSRM. Un cercle en kilomètres est plus grossier que ce
  qui existe déjà.

N'envoie un filtre géographique que quand il ne peut rien coûter — des origines
existent **et** la recherche refuse la livraison — et prends-le large : le filtre
exact repasse derrière, alors qu'un rayon trop court perd des annonces en silence.

**Pagine jusqu'au déjà-vu, pas jusqu'à la fin.** C'est un moniteur, pas un moteur
de recherche : trie par date décroissante et arrête-toi dès qu'une page ne
contient que des URL déjà en base. Tu récupères tout ce qui est nouveau depuis le
dernier passage sans jamais parcourir le corpus entier.

## 8. Les pièges déjà rencontrés

- **La plupart des sites font un ET sur tous les mots.** Un seul mot que le
  vendeur n'a pas écrit et la requête ne remonte rien : « Peak design 30l V2 »
  ne trouvait rien sur anibis alors que l'annonce y était, titrée « Peak Design
  Everyday Backpack 30L ». `sources.search()` retente donc **une fois** sans le
  dernier mot quand la première tentative est vide. Élargir est sans risque —
  la précision vient après, de `keyword_score`, `matches_target` et du tri IA.
- **Cherche le champ avant de deviner.** « Est-ce que le vendeur envoie ? » avait
  l'air absent de l'API leboncoin — il est dans `attributes["shippable"]`, avec
  l'état, le transporteur et la taille du colis. Scanner la description aurait
  produit un résultat approximatif là où la donnée exacte était disponible. Et
  ce champ-là est structurant : une annonce livrable **court-circuite tout le
  filtre distance**.

- **Séparateurs de milliers.** `2 100 CHF` avec une espace insécable ou une
  apostrophe typographique s'est enregistré en `2.00`. Réutilise `sources._num`.
- **Ne devine pas un prix** depuis le texte autour : « CAAD13 1 250 € » s'est lu
  `131250` parce que le motif laissait un séparateur coller n'importe quels
  chiffres.
- **`images` et `attrs` sont du JSON**, pas des objets Python. SQLite ne lie que
  des scalaires.
- **Une galerie d'annonce liste souvent les annonces voisines.** Exclure les
  ancêtres qui pointent vers un autre article, sinon 20 photos sur 25 sont
  celles du voisin.
- **Vérifie une URL construite** avant de la livrer : un champ `seoPath` inventé
  a produit 133 liens en 404 sans que rien ne le signale.
