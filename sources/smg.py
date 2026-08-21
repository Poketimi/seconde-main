"""anibis et tutti : même plateforme SMG, charge utile identique."""
import re, json, time
from urllib.parse import quote_plus, urljoin
import net, browser, crawler, geo
from .registry import adapter, ADAPTERS, LAST_STATUS, LAST_STRATEGY, try_strategies
from .util import (_next_data, _flight_blob, json_objects_with, find_lists,
                   jsonld_listings, hires, _thumb, _num, _ts, delivery_from_text)

# --- anibis + tutti: same SMG platform, identical payload ------------
@adapter("anibis")
def anibis(query, spec=None):
    return _smg_search("anibis", "https://www.anibis.ch", query)

@adapter("tutti")
def tutti(query, spec=None):
    """tutti.ch: 2.1M listings, same operator and payload as anibis.

    Worth having on its own, and it carries cars and property -- the categories
    the Cloudflare-walled *Scout24 sites hold.
    """
    return _smg_search("tutti", "https://www.tutti.ch", query)

def _smg_search(name, base, query):
    # /fr/q/<term> silently ignores the term and returns the whole catalogue
    # (millions of rows, query=null). Only ?query= actually searches.
    #
    # Fetched with the identified crawler: measured, anibis and tutti both
    # answer an honest bot UA with 200 and the full payload, so there is no
    # reason to impersonate a browser here.
    body = crawler.get(f"{base}/fr/q?query={quote_plus(query)}")
    if not body:
        return []
    conn = find_lists(_next_data(body), ["edges"]) or []        # GraphQL connection
    items = [e.get("node") or e for e in conn if isinstance(e, dict)]
    out = []
    for a in items:
        pc = a.get("postcodeInformation") or {}
        seo = a.get("seoInformation") or {}
        seller = a.get("sellerInfo") or {}
        lid = a.get("listingID") or a.get("id")
        # anibis exposes seoPath; tutti only frSlug, and its items live at
        # /fr/vi/<slug>/<id> -- without the slug every tutti link 404s.
        path = seo.get("seoPath") or seo.get("path") or ""
        if not path and seo.get("frSlug"):
            path = f"/fr/vi/{seo['frSlug'].strip('/')}/{lid}"
        cat = a.get("primaryCategory")
        out.append({
            "url": urljoin(base, path) if path else f"{base}/fr/vi/{lid}",
            "source": name, "source_id": str(lid),
            "title": a.get("title"), "description": a.get("body"),
            # anibis et tutti ne publient aucun champ de livraison : ce que le
            # vendeur écrit est la seule information disponible. None quand il
            # n'en dit rien — surtout ne pas supposer.
            "shipping": delivery_from_text(f"{a.get('title') or ''} {a.get('body') or ''}"),
            "price": _num(a.get("formattedPrice") or a.get("price")),
            "currency": "CHF", "price_type": "fixed",
            "category": cat.get("categoryID") if isinstance(cat, dict) else cat,
            "seller_type": "pro" if (seller.get("subscriptionInfo")
                                     or seller.get("logoURL")) else "private",
            "seller_name": seller.get("alias") or seller.get("name"),
            "seller_key": seller.get("alias") or seller.get("name"),
            "location_raw": pc.get("locationName"),
            "postal_code": pc.get("postcode"), "country": "CH",
            "lat": None, "lon": None,          # resolved offline from postcode
            "image": _thumb(a.get("thumbnail")),
            "images": json.dumps([u for u in (_thumb(i) for i in (a.get("images") or [])) if u][:8]),
            "posted_at": _ts(a.get("timestamp")),
            "attrs": json.dumps({"canton": (pc.get("canton") or {}).get("shortName")}
                                if pc.get("canton") else {}),
            "raw": json.dumps(a, ensure_ascii=False)[:20000],
        })
    return [o for o in out if o["url"]]

