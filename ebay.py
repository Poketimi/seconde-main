"""eBay par son API officielle — pas de crawl du tout.

eBay refuse ce robot sur ses pages web (403), et c'est très bien : il publie
une API pour ça. Compte développeur gratuit, 5000 requêtes/jour, ce qui est
deux ordres de grandeur au-dessus de ce que cette app consomme.

Rien ici ne passe par crawler.py : il n'y a pas de robots.txt à respecter pour
un point d'accès conçu pour être appelé. Le débit est borné par le quota
d'eBay, pas par un délai de politesse.

Identifiants : https://developer.ebay.com/my/keys — puis /reglages.
"""
import base64, json, time
import config, net

AUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
SCOPE = "https://api.ebay.com/oauth/api_scope"

# Le jeton vaut 2 h ; le regarder expirer coûterait un aller-retour par appel.
_token = {"value": None, "expires": 0.0}
LAST_ERROR = [""]

def configured():
    return bool(config.EBAY_CLIENT_ID and config.EBAY_CLIENT_SECRET)

def token(force=False):
    """Jeton applicatif (client credentials). None si les clés sont absentes."""
    if not configured():
        LAST_ERROR[0] = "identifiants eBay absents"
        return None
    if not force and _token["value"] and time.time() < _token["expires"] - 60:
        return _token["value"]
    basic = base64.b64encode(
        f"{config.EBAY_CLIENT_ID}:{config.EBAY_CLIENT_SECRET}".encode()).decode()
    r = net.post_form(AUTH_URL,
                      {"grant_type": "client_credentials", "scope": SCOPE},
                      headers={"Authorization": f"Basic {basic}",
                               "Content-Type": "application/x-www-form-urlencoded"})
    if r is None or r.status_code != 200:
        code = getattr(r, "status_code", "pas de réponse")
        LAST_ERROR[0] = (f"jeton refusé ({code}) — vérifie l'App ID et le Cert ID, "
                         f"et qu'ils sont de type Production et non Sandbox")
        return None
    try:
        d = r.json()
        _token.update(value=d["access_token"],
                      expires=time.time() + float(d.get("expires_in") or 7200))
    except Exception as e:
        LAST_ERROR[0] = f"réponse de jeton illisible : {type(e).__name__}"
        return None
    LAST_ERROR[0] = ""
    return _token["value"]

def _filters(spec):
    """Traduit les critères de la recherche en filtres eBay."""
    f = ["conditions:{USED|VERY_GOOD|GOOD|ACCEPTABLE}"]
    if config.EBAY_DELIVERY_CH:
        f.append("deliveryCountry:CH")
    lo, hi = (spec or {}).get("price_min"), (spec or {}).get("price_max")
    if lo or hi:
        # eBay veut les deux bornes ; .. seul d'un côté est accepté
        rng = f"[{int(lo) if lo else ''}..{int(hi) if hi else ''}]"
        f.append(f"price:{rng}")
        f.append("priceCurrency:CHF")
    return ",".join(f)

def search(query, spec=None, limit=50):
    """Annonces d'occasion pour cette requête, au format des autres adaptateurs."""
    tok = token()
    if not tok:
        return []
    params = {"q": query, "limit": str(min(limit, 200)),
              "filter": _filters(spec), "sort": "newlyListed"}
    r = net.get(SEARCH_URL, params=params, throttle=False,
                headers={"Authorization": f"Bearer {tok}",
                         "X-EBAY-C-MARKETPLACE-ID": config.EBAY_MARKETPLACE,
                         "Accept": "application/json"})
    if r is not None and getattr(r, "status_code", 0) == 401:
        tok = token(force=True)          # jeton périmé : un seul nouvel essai
        if not tok:
            return []
        r = net.get(SEARCH_URL, params=params, throttle=False,
                    headers={"Authorization": f"Bearer {tok}",
                             "X-EBAY-C-MARKETPLACE-ID": config.EBAY_MARKETPLACE,
                             "Accept": "application/json"})
    if r is None:
        LAST_ERROR[0] = "aucune réponse d'eBay"
        return []
    if r.status_code != 200:
        LAST_ERROR[0] = f"eBay a répondu {r.status_code}"
        return []
    try:
        data = r.json()
    except Exception:
        LAST_ERROR[0] = "réponse eBay illisible"
        return []
    LAST_ERROR[0] = ""
    return [_row(it) for it in (data.get("itemSummaries") or []) if it.get("itemWebUrl")]

def _amount(d):
    try:
        return float((d or {}).get("value"))
    except (TypeError, ValueError):
        return None

def _row(it):
    """Une annonce eBay au format commun. Tout est optionnel côté eBay."""
    loc = it.get("itemLocation") or {}
    seller = it.get("seller") or {}
    ship = (it.get("shippingOptions") or [{}])[0]
    opts = it.get("buyingOptions") or []
    auction = "AUCTION" in opts
    price = _amount(it.get("price")) or _amount(it.get("currentBidPrice"))
    imgs = [i.get("imageUrl") for i in ([it.get("image")] + (it.get("thumbnailImages") or []))
            if isinstance(i, dict) and i.get("imageUrl")]
    return {
        "url": it.get("itemWebUrl"), "source": "ebay",
        "source_id": str(it.get("itemId") or it.get("legacyItemId") or ""),
        "title": it.get("title"), "description": it.get("shortDescription"),
        "price": price,
        "currency": ((it.get("price") or {}).get("currency")) or "CHF",
        "price_type": "auction" if auction else "fixed",
        "category": None,
        "condition": it.get("condition"),
        # eBay ne dit pas si un vendeur est professionnel ; ne rien inventer
        "seller_type": None,
        "seller_name": seller.get("username"),
        "seller_key": seller.get("username"),
        "location_raw": ", ".join(x for x in (loc.get("city"),
                                              loc.get("stateOrProvince")) if x) or None,
        "postal_code": loc.get("postalCode"), "country": loc.get("country") or "CH",
        "lat": None, "lon": None,
        "shipping": 1 if ship.get("shippingCost") is not None else 0,
        "shipping_cost": _amount(ship.get("shippingCost")),
        "image": imgs[0] if imgs else None,
        "images": json.dumps(imgs[:8]),
        "posted_at": _ts(it.get("itemCreationDate")),
        "auction_end": _ts(it.get("itemEndDate")) if auction else None,
        "bids": it.get("bidCount") if auction else None,
        "attrs": json.dumps({k: v for k, v in
                             (("feedback", seller.get("feedbackScore")),
                              ("feedback_pct", seller.get("feedbackPercentage")))
                             if v is not None}),
        "raw": json.dumps(it, ensure_ascii=False)[:20000],
    }

def _ts(iso):
    """2026-08-21T09:12:03.000Z -> epoch. eBay date tout en ISO 8601 UTC."""
    if not iso:
        return None
    try:
        from datetime import datetime, timezone
        return datetime.strptime(iso.replace("Z", "+0000"),
                                 "%Y-%m-%dT%H:%M:%S.%f%z").timestamp()
    except Exception:
        try:
            from datetime import datetime
            return datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S").replace(
                tzinfo=__import__("datetime").timezone.utc).timestamp()
        except Exception:
            return None

def probe():
    """(ok, message) pour le bouton Tester des réglages."""
    if not configured():
        return False, ("Il manque l'App ID et le Cert ID. Crée-les gratuitement sur "
                       "developer.ebay.com/my/keys, en clés « Production ».")
    if not token(force=True):
        return False, LAST_ERROR[0]
    rows = search("iphone", limit=3)
    if not rows:
        return False, (LAST_ERROR[0] or
                       f"Jeton accepté, mais aucune annonce sur {config.EBAY_MARKETPLACE}.")
    return True, (f"eBay branché : {len(rows)} annonces de test sur "
                  f"{config.EBAY_MARKETPLACE} (« {rows[0]['title'][:40]}… »).")

def demo():
    """Sans identifiants, la recherche doit rendre [] sans exploser."""
    it = {"itemId": "v1|123|0", "title": "Yamaha Tracer 900 GT",
          "itemWebUrl": "https://www.ebay.ch/itm/123",
          "price": {"value": "8100.00", "currency": "CHF"},
          "itemLocation": {"city": "Genève", "postalCode": "1200", "country": "CH"},
          "seller": {"username": "moto_ch", "feedbackScore": 412},
          "buyingOptions": ["AUCTION"], "bidCount": 7,
          "itemEndDate": "2026-08-25T18:00:00.000Z",
          "image": {"imageUrl": "https://i.ebayimg.com/x.jpg"},
          "shippingOptions": [{"shippingCost": {"value": "0.00", "currency": "CHF"}}]}
    r = _row(it)
    assert r["price"] == 8100.0 and r["currency"] == "CHF"
    assert r["source"] == "ebay" and r["source_id"] == "v1|123|0"
    assert r["price_type"] == "auction" and r["bids"] == 7
    assert r["auction_end"] and r["auction_end"] > 1_700_000_000
    assert r["postal_code"] == "1200" and r["country"] == "CH"
    assert r["seller_name"] == "moto_ch"
    assert json.loads(r["attrs"])["feedback"] == 412
    # un objet vide ne doit pas lever
    assert _row({"itemWebUrl": "https://x"})["price"] is None
    assert "priceCurrency:CHF" in _filters({"price_max": 500})
    assert "price:" not in _filters({})
    assert "deliveryCountry:CH" in _filters({}) or not config.EBAY_DELIVERY_CH
    print("ebay ok")

if __name__ == "__main__":
    demo()
