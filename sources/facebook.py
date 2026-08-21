"""facebook marketplace : via un vrai navigateur, connecté à ton compte."""
import re, json, time
from urllib.parse import quote_plus, urljoin
import net, browser, crawler, geo
from .registry import adapter, ADAPTERS, LAST_STATUS, LAST_STRATEGY, try_strategies
from .util import (_next_data, _flight_blob, json_objects_with, find_lists,
                   jsonld_listings, hires, _thumb, _num, _ts)

# --- generic: any search URL, tier-2 extraction ------------------------
def generic(url, source_name):
    r = net.get(url)
    if r is None:
        return []
    rows = jsonld_listings(r.text, url)
    for x in rows:
        x.setdefault("source", source_name)
        x.setdefault("country", None)
    return rows

# Sites reachable only through a real browser session. The URLs are best-effort:
# check one in a browser and fix the template here if a site moved its search.
BROWSER_SEARCH = {
    "autoscout24":    "https://www.autoscout24.ch/fr/voitures/recherche?query={q}",
    "motoscout24":    "https://www.motoscout24.ch/fr/motos/recherche?query={q}",
    "immoscout24":    "https://www.immoscout24.ch/fr/immobilier/rechercher?query={q}",
    "fb_marketplace": "https://www.facebook.com/marketplace/search/?query={q}",
}

# Facebook renders everything client-side and ships no JSON blob, so the DOM
# is the only source. Each result card is an <a href="/marketplace/item/ID">
# whose innerText is roughly: price / title / locality.
FB_JS = """() => [...document.querySelectorAll('a[href*="/marketplace/item/"]')]
  .map(a => ({ href: a.getAttribute('href'),
               txt: (a.innerText || '').trim(),
               img: (a.querySelector('img') || {}).src || null }))
  .filter(x => x.txt)"""

@adapter("fb_marketplace")
def fb_marketplace(query, spec=None):
    url = f"https://www.facebook.com/marketplace/search/?query={quote_plus(query)}"
    cards = browser.eval_page(url, FB_JS, wait_ms=7000,
                              wait_for='a[href*="/marketplace/item/"]')
    if not cards:
        return []
    out, seen = [], set()
    for c in cards:
        m = re.search(r"/marketplace/item/(\d+)", c.get("href") or "")
        if not m or m.group(1) in seen:
            continue
        lid = m.group(1)
        seen.add(lid)
        lines = [l.strip() for l in (c.get("txt") or "").split("\n") if l.strip()]
        if not lines:
            continue
        price = next((_num(l) for l in lines if re.search(r"(CHF|EUR|€|Fr\.)", l)), None)
        gratis = any(re.search(r"\b(gratuit|free)\b", l, re.I) for l in lines)
        rest = [l for l in lines if not re.search(r"(CHF|EUR|€|Fr\.)", l)]
        # "Chamoson, VS" / "Le Flon, FR" is a locality, never the title -- taking
        # the longest line blindly filed a whole town as the item's name
        is_loc = lambda l: bool(re.search(r",\s*[A-Z]{2}$", l)) and len(l) < 40
        locality = next((l for l in reversed(rest) if is_loc(l)), None)
        titles = [l for l in rest if l != locality and not is_loc(l)]
        if not titles:
            continue                      # price + town only: nothing to match on
        title = max(titles, key=len)
        out.append({
            "url": f"https://www.facebook.com/marketplace/item/{lid}/",
            "source": "fb_marketplace", "source_id": lid,
            "title": title[:200], "description": None,
            "price": 0.0 if (gratis and price is None) else price,
            "currency": "CHF", "price_type": "negotiable",
            "category": None, "condition": None,
            "seller_type": "private", "seller_name": None,
            "location_raw": locality, "postal_code": None, "country": "CH",
            "lat": None, "lon": None,           # resolved offline from the locality
            "image": c.get("img"),
            "images": "[]", "posted_at": None, "attrs": "{}",
            "raw": json.dumps(c, ensure_ascii=False)[:4000],
        })
    return out

def _browser_adapter(name, tmpl):
    def fn(query, spec=None):
        html = browser.fetch(tmpl.format(q=quote_plus(query)))
        if not html:
            return []
        rows = jsonld_listings(html, tmpl)
        if not rows:                       # try the site's embedded state
            nd = _next_data(html)
            items = find_lists(nd, ["ads", "edges", "listings", "results", "items"]) or []
            for it in items:
                it = it.get("node") or it
                url = it.get("url") or it.get("seoUrl") or it.get("link")
                if not url or not (it.get("title") or it.get("name")):
                    continue
                rows.append({"url": urljoin(tmpl, url),
                             "title": it.get("title") or it.get("name"),
                             "description": it.get("body") or it.get("description"),
                             "price": _num(it.get("price")),
                             "raw": json.dumps(it, ensure_ascii=False)[:20000]})
        for x in rows:
            x.setdefault("source", name)
            x.setdefault("currency", "CHF")
            x.setdefault("country", "CH")
        return [x for x in rows if x.get("url")]
    fn.__name__ = f"{name}_browser"
    ADAPTERS[name] = fn
    return fn

# autoscout24 / motoscout24 / immoscout24 refuse identified crawlers AND serve
# a Cloudflare challenge to a browser. Driving a browser at them anyway would
# be circumventing an explicit denial, so no adapter is registered for them.
# They stay visible in the UI as "refusé par le site", which is the honest
# state, and would come back if the operator allowlisted this crawler.
