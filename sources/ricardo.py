"""ricardo : par le sitemap qu'il publie pour les crawlers."""
import re, json, time
from urllib.parse import quote_plus, urljoin
import net, browser, crawler, geo
from .registry import adapter, ADAPTERS, LAST_STATUS, LAST_STRATEGY, try_strategies
from .util import (_next_data, _flight_blob, json_objects_with, find_lists,
                   jsonld_listings, hires, _thumb, _num, _ts)

# --- ricardo (flaky: rotates profiles, tolerates blocks) ---------------
@adapter("ricardo")
def ricardo(query, spec=None):
    """Ricardo through its PUBLISHED SITEMAP — the path it offers crawlers.

    Measured: ricardo's robots.txt invites crawlers (sitemaps + crawl-delay
    0.2) but its WAF answers 403 to any identified bot on content pages. So we
    take what it publishes and nothing more.

    The consequence is honest and visible to the user: the sitemap carries the
    title and the listing id, never a price. These results say "prix inconnu"
    and link out, rather than pretending to a number we were not given.
    """
    urls = []
    for sm in crawler.sitemaps("www.ricardo.ch"):
        if "index" in sm or "sitemap" in sm:
            urls = crawler.sitemap_urls(sm, limit=50000, want="pdp")
            if urls:
                break
    if not urls:
        urls = crawler.sitemap_urls("https://www.ricardo.ch/fr/sitemap-pdp.xml", limit=50000)
    if not urls:
        return []

    from engine import matches_target
    terms = [w for w in re.sub(r"[^\w\s]", " ", query.lower()).split() if len(w) > 1]
    out = []
    for u in urls:
        m = re.search(r"/a/(.+?)-(\d+)/?$", u)
        if not m:
            continue
        title = m.group(1).replace("-", " ")
        if not all(t in title for t in terms):
            continue
        out.append({
            "url": u, "source": "ricardo", "source_id": m.group(2),
            "title": title, "description": None,
            "price": None, "currency": "CHF", "price_type": None,
            "category": None, "condition": None,
            "seller_type": None, "seller_name": None, "seller_key": None,
            "location_raw": None, "postal_code": None, "country": "CH",
            # le sitemap ne porte ni prix ni livraison : None, pas 0. Un 0
            # affirmerait « retrait sur place » sans que ricardo l'ait dit.
            "lat": None, "lon": None, "shipping": None, "shipping_cost": None,
            "image": None, "images": "[]", "posted_at": None,
            "attrs": json.dumps({"source_path": "sitemap", "price_unknown": True}),
            "raw": json.dumps({"from": "sitemap", "url": u}),
        })
        if len(out) >= 60:
            break
    return out

