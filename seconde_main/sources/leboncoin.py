"""leboncoin par le client d'API `lbc`. Aucun crawl : rien ne passe par crawler.py.

Le filtre géographique est délibérément **absent par défaut**. L'app sait déjà
faire mieux :

  - une recherche sans origine accepte tout (`distance_ok` renvoie True
    d'emblée) : un rayon envoyé au site ne filtrerait rien d'utile ;
  - `shipping_ok` court-circuite la distance — un rayon de 20 min jetterait une
    annonce livrable à 500 km que l'app aurait gardée ;
  - avec des origines, `geo.best_origin` calcule le vrai temps de trajet par
    mode, affiné par OSRM. Un cercle en kilomètres est plus grossier.

C'est un moniteur, pas un moteur de recherche : trié par date, il suffit de
remonter jusqu'au déjà-vu. Pas besoin du corpus national, seulement de ce qui
est nouveau depuis le dernier passage.
"""
import json, re, time
from seconde_main import db, config
from .registry import adapter, LAST_STATUS

try:
    import lbc
except ImportError:                       # absent : on le dit, on ne casse rien
    lbc = None

_client = None
PER_PAGE = 35
MAX_PAGES = 6                             # garde-fou : ~200 annonces par cycle
PAGE_DELAY = 4.0                          # entre deux pages, mesuré nécessaire

# leboncoin est derrière DataDome. Quelques requêtes rapprochées suffisent à
# se faire signaler — constaté en enchaînant quatre recherches sans pause.
# Un refus n'est pas une erreur à réessayer : c'est un « non » qui doit mettre
# la source en retrait, sinon on transforme un blocage temporaire en définitif.
BLOCKED_HINTS = ("datadome", "blocked", "suspicious", "403", "forbidden")

# « Remise en main propre » ne veut PAS dire « pas d'envoi ». Le plus souvent
# c'est une option EN PLUS — « envoi possible ou remise en main propre ». Ne
# rabattre le drapeau que quand le vendeur dit que c'est exclusif : marquer à
# tort une annonce non livrable la rend invisible depuis la Suisse, puisque
# `distance_ok` ne laisse passer les annonces lointaines QUE si elles sont
# livrables.
_HAND_ONLY = re.compile(
    r"(main\s*propre[^.!?\n]{0,30}\b(uniquement|seulement|exclusivement|obligatoire)"
    r"|\b(uniquement|seulement|exclusivement)[^.!?\n]{0,30}main\s*propre"
    r"|(pas|aucun|jamais)\s+d[e']?\s*(envoi|expédition|exp[ée]dition)"
    r"|(no|pas de)\s+shipping"
    r"|je\s+n[e']\s*(envoie|exp[ée]die)\s*pas)", re.I)

def hand_delivery_only(text):
    """Le vendeur exclut-il explicitement l'envoi ?

    Vrai seulement sur une exclusivité affirmée. « Envoi ou remise en main
    propre » reste livrable — c'est le cas le plus fréquent, et le confondre
    coûterait toutes les annonces françaises livrables.
    """
    return bool(_HAND_ONLY.search(text or ""))

# leboncoin -> vocabulaire de l'app. Un état inconnu vaut None, pas une
# supposition : `condition_min` filtre dessus.
CONDITION = {"neuf": "new", "commeneuf": "like_new", "tresbonetat": "good",
             "bonetat": "good", "etatsatisfaisant": "fair", "pourpieces": "parts"}


def client():
    global _client
    if _client is None and lbc is not None:
        _client = lbc.Client()
    return _client


def _known_urls():
    """Ce qu'on a déjà vu : sert à savoir quand arrêter de paginer."""
    return {r["url"] for r in
            db.q("SELECT url FROM listings WHERE source='leboncoin'")}


def _locations(spec):
    """Un filtre géographique SEULEMENT quand il ne peut rien coûter.

    C'est-à-dire : des origines existent ET la recherche refuse la livraison.
    Sinon on ne filtre pas et `engine.distance_ok` tranche sur le vrai temps de
    trajet. Le rayon est volontairement large : le filtre exact repasse
    derrière, alors qu'un rayon trop court perd des annonces en silence.
    """
    if not lbc or not spec or spec["shipping_ok"]:
        return []
    try:
        origins = json.loads(spec["origins"] or "[]")
    except (KeyError, TypeError, ValueError):
        return []
    out = []
    for o in origins:
        if o.get("lat") is None or o.get("lon") is None:
            continue
        mode = o.get("mode") or "car"
        km = (o.get("max_minutes") or 30) / 60.0 * config.MODE_SPEED_KMH.get(mode, 30.0)
        out.append(lbc.City(lat=o["lat"], lng=o["lon"],
                            radius=int(km * 1000 * 1.5),   # large exprès
                            city=o.get("label") or ""))
    return out


@adapter("leboncoin")
def leboncoin(query, spec=None):
    if lbc is None:
        LAST_STATUS["leboncoin"] = ("error", "module `lbc` non installé")
        return []
    kwargs = {"text": query, "limit": PER_PAGE,
              "sort": lbc.Sort.NEWEST, "ad_type": lbc.AdType.OFFER}
    locs = _locations(spec)
    if locs:
        kwargs["locations"] = locs
    # price/square passent par **kwargs côté lbc, pas par un paramètre nommé
    hi = (spec or {}).get("price_max") if spec else None
    if hi:
        kwargs["price"] = [int((spec.get("price_min") or 0)), int(hi)]

    known, rows, seen = _known_urls(), [], set()
    for page in range(1, MAX_PAGES + 1):
        try:
            result = client().search(page=page, **kwargs)
        except Exception as e:
            blocked = any(h in f"{type(e).__name__} {e}".lower() for h in BLOCKED_HINTS)
            LAST_STATUS["leboncoin"] = (
                "blocked" if blocked else "error",
                ("DataDome nous a signalés — on se met en retrait"
                 if blocked else f"{type(e).__name__}: {e}"[:200]))
            return rows
        ads = [a for a in (result.ads or []) if getattr(a, "url", None)]
        for a in ads:
            if a.url not in seen:
                seen.add(a.url)
                rows.append(_row(a))
        # trié par date : une page entièrement connue signifie que la suite l'est
        if not ads or all(a.url in known for a in ads):
            break
        if page >= getattr(result, "max_pages", MAX_PAGES):
            break
        time.sleep(PAGE_DELAY)     # ne pas enchaîner : c'est ce qui déclenche DataDome
    return rows


def _ts(iso):
    """« 2026-08-21 09:12:03 » -> epoch. leboncoin date en heure locale."""
    if not iso:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return time.mktime(time.strptime(str(iso)[:19], fmt))
        except ValueError:
            continue
    return None


def _attr(ad, key, label=False):
    at = (getattr(ad, "attributes", None) or {}).get(key)
    if at is None:
        return None
    return getattr(at, "value_label", None) if label else getattr(at, "value", None)


# Ce qui n'a rien à faire sur une fiche : identifiants internes, URLs d'avatar,
# drapeaux d'interface. Le reste est de l'information sur l'objet.
_ATTR_SKIP = {"profile_picture_url", "purchase_cta_visible", "negotiation_cta_visible",
              "is_bundleable", "is_eligible_to_warranty", "country_isocode3166",
              "shippable", "condition"}

def _attrs(ad):
    """Les attributs de catégorie, aplatis en {libellé: valeur lisible}.

    `shippable` et `condition` en sortent : ils ont leur propre colonne, et les
    répéter ici ferait deux sources de vérité pour la même chose.
    """
    out = {}
    for key, at in (getattr(ad, "attributes", None) or {}).items():
        if key in _ATTR_SKIP:
            continue
        val = getattr(at, "value_label", None) or getattr(at, "value", None)
        if val not in (None, "") and not str(val).startswith("http"):
            out[getattr(at, "key_label", None) or key] = val
    return out


def _shipping(ad):
    """0/1. L'API fait foi, sauf si le vendeur écrit noir sur blanc l'inverse."""
    api = str(_attr(ad, "shippable")).lower() == "true"
    if not api:
        return 0
    txt = f"{getattr(ad, 'subject', '') or ''} {getattr(ad, 'body', '') or ''}"
    return 0 if hand_delivery_only(txt) else 1


def _row(ad):
    """Une annonce lbc au format commun. Voir le tableau de SOURCES.md.

    Rien n'est deviné : un champ que l'API ne donne pas reste None. Un faux prix
    pollue les médianes du catalogue de façon permanente.

    `ad.user` déclenche une requête supplémentaire par annonce : on ne le touche
    pas ici. sellers.py enrichit le vendeur à l'ouverture de la fiche, quand ça
    concerne une annonce que tu regardes vraiment.
    """
    loc = getattr(ad, "location", None)
    g = lambda o, *names: next((getattr(o, n) for n in names
                                if o is not None and getattr(o, n, None) is not None), None)
    images = list(getattr(ad, "images", None) or [])
    return {
        "url": ad.url,
        "source": "leboncoin",
        "source_id": str(getattr(ad, "id", "") or ""),
        "title": g(ad, "subject", "title"),
        "description": getattr(ad, "body", None),
        "price": float(ad.price) if getattr(ad, "price", None) else None,
        "currency": "EUR",
        "price_type": "fixed",
        "category": getattr(ad, "category_name", None),
        # « shippable » est bien exposé, dans les attributs : pas besoin de
        # deviner depuis la description. Ça compte — une annonce livrable
        # court-circuite le filtre distance, et leboncoin est en France.
        "condition": CONDITION.get(str(_attr(ad, "condition") or "").lower()),
        # lbc ne livre le type de vendeur qu'en chargeant ad.user : ne rien inventer
        "seller_type": None,
        "seller_name": None,
        "seller_key": str(getattr(ad, "_user_id", "") or "") or None,
        "location_raw": g(loc, "city", "label"),
        "postal_code": g(loc, "zipcode"),          # suffit : geo.py résout hors ligne
        "country": (g(loc, "country") or "FR").upper()[:2],
        "lat": g(loc, "lat"), "lon": g(loc, "lng"),
        "shipping": _shipping(ad),
        "image": images[0] if images else None,
        "images": json.dumps(images[:8]),          # JSON, pas une liste
        "posted_at": _ts(getattr(ad, "first_publication_date", None)),
        "attrs": json.dumps(_attrs(ad), ensure_ascii=False),
        "raw": json.dumps({"id": getattr(ad, "id", None),
                           "category_id": getattr(ad, "category_id", None),
                           "brand": getattr(ad, "brand", None),
                           "status": getattr(ad, "status", None)},
                          ensure_ascii=False, default=str)[:20000],
    }


def demo():
    """Sans réseau : le format, et le fait qu'un module absent ne casse rien."""
    if lbc is None:
        assert leboncoin("maison") == []
        print("leboncoin ok (module absent)")
        return
    # une recherche qui accepte la livraison ne filtre RIEN géographiquement
    spec = {"shipping_ok": 1, "origins": '[{"lat":46,"lon":6,"max_minutes":20}]'}
    assert _locations(spec) == [], "un rayon jetterait les annonces livrables"
    spec = {"shipping_ok": 0, "origins": '[{"lat":46,"lon":6,"max_minutes":20,"mode":"car"}]'}
    assert len(_locations(spec)) == 1 and _locations(spec)[0].radius > 20_000
    assert _locations(None) == []
    assert _ts("2026-08-21 09:12:03") > 1_700_000_000
    # l'option en plus reste livrable ; l'exclusivité ne l'est pas
    assert not hand_delivery_only("Envoi possible ou remise en main propre")
    assert not hand_delivery_only("Remise en main propre à Lyon, envoi Mondial Relay")
    assert hand_delivery_only("Remise en main propre uniquement")
    assert hand_delivery_only("En main propre seulement, pas d'envoi")
    assert hand_delivery_only("Pas d'envoi, à récupérer sur place")
    assert hand_delivery_only("je n'envoie pas")
    assert not hand_delivery_only("70€ en main propre")   # simple mention de prix
    assert not hand_delivery_only("")
    assert _ts(None) is None and _ts("n'importe quoi") is None

    class FakeLoc:
        city, zipcode, lat, lng, country, label = "Annemasse", "74100", 46.19, 6.23, "FR", None
    class FakeAd:
        id, url, subject, body, price = 42, "https://www.leboncoin.fr/ad/x/42", "Vélo", "bon état", 1250.0
        images, category_name, first_publication_date = ["https://i/a.jpg"], "Vélos", "2026-08-21 09:12:03"
        location, attributes, _user_id, brand, status, category_id = FakeLoc(), {}, "u9", None, "active", "55"
    r = _row(FakeAd())
    assert r["price"] == 1250.0 and r["currency"] == "EUR" and r["country"] == "FR"
    assert r["postal_code"] == "74100" and r["source_id"] == "42"
    assert json.loads(r["images"]) == ["https://i/a.jpg"]
    assert r["seller_type"] is None, "le type de vendeur n'est pas connu sans ad.user"
    print("leboncoin ok")
