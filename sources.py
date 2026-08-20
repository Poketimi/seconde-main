"""Marketplace adapters.

Each adapter: search(query, spec) -> [normalized listing dict].
Normalized keys mirror the `listings` table columns.

Extraction is tiered, cheapest first:
  1. the site's embedded JSON (__NEXT_DATA__ / RSC flight)  -- free, exact
  2. schema.org JSON-LD                                      -- free, generic
  3. ai.extract_listings() on cleaned text                   -- costs tokens
Tier 3 lives in engine.py so a new site works on day one and can be promoted
to tier 1 later by writing 20 lines here.

STATUS (probed 2026-08-19 from CH):
  leboncoin  OK   __NEXT_DATA__.props.pageProps.searchData.ads, incl. lat/lng
  anibis/tutti  OK  same SMG platform: listings.edges[].node (needs ?query=)
  ricardo    FLAKY  chrome profiles 403; safari/firefox pass but it rate-limits
                    aggressively. Parses the RSC flight payload.
  autoscout24 / motoscout24 / immoscout24, fb marketplace:
             BLOCKED without a real browser session -- see NEEDS_BROWSER.
"""
import re, json, time
from urllib.parse import quote_plus, urljoin
import net, browser, geo, crawler

ADAPTERS = {}
# Sites that answer no plain HTTP client: they sit behind Cloudflare Turnstile
# or a login. They go through browser.py with a profile you unlocked by hand.
# Nothing here attempts to solve a challenge.
# Only facebook remains on the browser tier, and not to get around anything:
# its listings live behind the user's own login, so no identified crawler could
# ever be permitted to read them. The *Scout24 sites moved to
# DENIED_BY_OPERATOR: they refuse this crawler, and that refusal stands.
NEEDS_BROWSER = ("fb_marketplace",)

def adapter(name):
    def deco(fn):
        ADAPTERS[name] = fn
        return fn
    return deco

# --- helpers -----------------------------------------------------------
def _next_data(html):
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None

def _flight_blob(html):
    """Decode a Next.js App Router RSC payload into one searchable string."""
    chunks = re.findall(r'self\.__next_f\.push\(\[1,\s*"((?:[^"\\]|\\.)*)"\]\)', html, re.S)
    out = []
    for c in chunks:
        try:
            out.append(json.loads('"' + c + '"'))
        except Exception:
            pass
    return "".join(out)

def json_objects_with(blob, marker):
    """Yield JSON objects in `blob` that contain `marker`, by brace matching."""
    for m in re.finditer(re.escape(marker), blob):
        # walk back to the opening brace of the enclosing object
        depth, start = 0, None
        for i in range(m.start(), -1, -1):
            ch = blob[i]
            if ch == '}':
                depth += 1
            elif ch == '{':
                if depth == 0:
                    start = i
                    break
                depth -= 1
        if start is None:
            continue
        depth = 0
        for j in range(start, min(len(blob), start + 20000)):
            ch = blob[j]
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    try:
                        yield json.loads(blob[start:j + 1])
                    except Exception:
                        pass
                    break

def find_lists(node, keys, depth=0):
    """Depth-first hunt for the first list-of-dicts under any of `keys`."""
    if depth > 12:
        return None
    if isinstance(node, dict):
        for k in keys:
            v = node.get(k)
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
        for v in node.values():
            r = find_lists(v, keys, depth + 1)
            if r:
                return r
    elif isinstance(node, list):
        for v in node[:20]:
            r = find_lists(v, keys, depth + 1)
            if r:
                return r
    return None

def jsonld_listings(html, base_url):
    """Tier 2: schema.org Product/Offer blocks. Works on a surprising number of sites."""
    out = []
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S):
        try:
            data = json.loads(m.group(1))
        except Exception:
            continue
        for node in (data if isinstance(data, list) else [data]):
            graph = node.get("@graph", [node]) if isinstance(node, dict) else []
            for g in graph:
                if not isinstance(g, dict):
                    continue
                if g.get("@type") in ("Product", "Offer", "IndividualProduct"):
                    offer = g.get("offers") or {}
                    if isinstance(offer, list):
                        offer = offer[0] if offer else {}
                    url = g.get("url") or offer.get("url")
                    out.append({
                        "url": urljoin(base_url, url) if url else None,
                        "title": g.get("name"),
                        "description": g.get("description"),
                        "price": _num(offer.get("price")),
                        "currency": offer.get("priceCurrency") or "CHF",
                        "image": g.get("image") if isinstance(g.get("image"), str) else None,
                        "raw": json.dumps(g)[:8000],
                    })
    return [o for o in out if o.get("url")]

def hires(url):
    """Swap a thumbnail URL for the biggest variant the CDN actually serves.

    Ricardo hands out t_265x200 (7 Ko) in search results; the same image at
    t_1000x750 is 61 Ko. anibis/tutti already serve /big/, their largest.
    """
    if not url:
        return url
    if "ricardostatic.ch" in url:
        return re.sub(r"/t_\d+x\d+/", "/t_1000x750/", url)
    return url

def gallery(listing_url, source):
    """Every photo for one listing, fetched on demand.

    Search payloads carry a single thumbnail (leboncoin excepted, which ships
    the whole set), so the gallery is only worth a request when someone
    actually opens the item.
    """
    if source == "ricardo":
        return _gallery_ricardo(listing_url)
    if source in ("anibis", "tutti"):
        return _gallery_smg(listing_url)
    if source == "fb_marketplace":
        return _gallery_fb(listing_url)
    return []

def _gallery_ricardo(url):
    r = net.get(url, profiles=["safari18_0", "firefox133"])
    if r is None:
        return []
    html = r.text
    og = re.search(r'<meta property="og:image" content="([^"]+)"', html)
    if not og:
        return []
    slug = og.group(1).rstrip("/").rsplit("/", 1)[-1]
    # the item's own photos repeat the og slug; related listings use another one
    out, seen = [], set()
    for uid, _var, s2 in re.findall(
            r"https://img\.ricardostatic\.ch/images/([0-9a-f-]{36})/(t_[\dx]+)/([^\"\\ ]+)", html):
        if s2.rstrip("/") != slug or uid in seen:
            continue
        seen.add(uid)
        out.append(f"https://img.ricardostatic.ch/images/{uid}/t_1000x750/{slug}")
    return out[:12]

def _gallery_smg(url):
    """anibis / tutti: the item page embeds images[].rendition.src."""
    r = net.get(url)
    if r is None:
        return []
    out = []
    nd = _next_data(r.text)
    for lst in ([find_lists(nd, ["images"])] if nd else []):
        for im in (lst or []):
            src = ((im.get("rendition") or {}).get("src")
                   if isinstance(im.get("rendition"), dict) else None) or _thumb(im)
            if src and src not in out:
                out.append(src)
    if not out:      # fallback: the page repeats them as og:image tags
        out = [u.replace("http://", "https://")
               for u in re.findall(r'og:image" content="([^"]+)"', r.text)]
    return out[:12]

def fb_item_details(url):
    """Images and the seller's profile link, in ONE page view.

    Two separate visits would double our footprint on facebook for no reason.
    """
    js = """() => {
        const own = i => !i.closest('a[href*="/marketplace/item/"]');
        const imgs = [...document.querySelectorAll('img')]
            .filter(i => (i.src || '').includes('fbcdn')
                      && (i.naturalWidth || i.width) >= 300 && own(i))
            .map(i => i.src);
        const a = document.querySelector('a[href*="/marketplace/profile/"]');
        return {images: imgs, profile: a ? a.getAttribute('href') : null};
    }"""
    out = browser.eval_page(url, js, wait_ms=6000, wait_for="img") or {}
    imgs, seen = [], set()
    for u in (out.get("images") or []):
        if u not in seen:
            seen.add(u); imgs.append(u)
    prof = out.get("profile") or ""
    m = re.search(r"/marketplace/profile/(\d+)", prof)
    return {"images": imgs[:12],
            "seller_key": m.group(1) if m else None,
            "profile_url": f"https://www.facebook.com/marketplace/profile/{m.group(1)}/"
                           if m else None}

def _gallery_fb(url):
    """Facebook needs the rendered page; only worth it when an item is opened.

    An item page also renders a grid of OTHER listings, and those images live
    inside links to their own /marketplace/item/ URL. Taking every fbcdn image
    put 20 strangers' photos (motorbikes, an Apple TV) into one CPU listing --
    so anything wrapped in a link to another item is excluded.
    """
    js = """() => {
        const own = i => !i.closest('a[href*="/marketplace/item/"]');
        return [...document.querySelectorAll('img')]
          .filter(i => (i.src || '').includes('fbcdn')
                    && (i.naturalWidth || i.width) >= 300
                    && own(i))
          .map(i => i.src);
    }"""
    return fb_item_details(url)["images"]

def _thumb(v):
    """anibis nests image urls: {normalRendition:{src:...}}. Accept str or dict."""
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        for k in ("retinaRendition", "normalRendition", "rendition"):
            r = v.get(k)
            if isinstance(r, dict) and r.get("src"):
                return r["src"]
        return v.get("src") or v.get("url")
    return None

# Thousands separators seen in the wild: ASCII quote (ricardo "1'250"),
# typographic quote, non-breaking space (facebook "2\xa0100 CHF"), narrow
# no-break space, thin space. Missing any of them silently divides the price
# by a thousand -- a 2100 CHF bike was being stored as 2.
_THOUSANDS = "'\u2019\u2018`\u00a0\u202f\u2009\u2007\u2060 "

def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v)
    for ch in _THOUSANDS:
        s = s.replace(ch, "")
    # 1,234,567 is thousands-grouped; 12,50 is a decimal comma
    if re.search(r"\d,\d{3}\b", s) and not re.search(r",\d{1,2}\b(?!\d)", s):
        s = s.replace(",", "")
    m = re.search(r"\d+(?:[.,]\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", "."))
    except ValueError:
        return None

def _ts(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return time.mktime(time.strptime(str(s)[:19], fmt))
        except Exception:
            pass
    return None

# --- leboncoin (verified) ---------------------------------------------
# leboncoin: NO adapter, by their own written policy.
#
#   "It's forbidden to use search robots or other automatic methods to access
#    Leboncoin.fr. Access is only permitted with special permission."
#
# Their robots.txt then allowlists Googlebot, Bingbot, Slurp, msnbot and
# ia_archiver, with no User-agent: * group at all -- so anyone not named is
# excluded. The 403 we kept hitting was not a puzzle to solve, it was that
# policy being enforced. The route back is a permission request to leboncoin,
# not a change of code.

def _leboncoin_rows(ads):
    out = []
    for a in ads:
        loc = a.get("location") or {}
        owner = a.get("owner") or {}
        imgs = (a.get("images") or {}).get("urls") or []
        price = a.get("price")
        if not a.get("url"):
            continue
        out.append({
            "url": a.get("url"),
            "source": "leboncoin", "source_id": str(a.get("list_id")),
            "title": a.get("subject"), "description": a.get("body"),
            "price": _num(price[0] if isinstance(price, list) and price else price),
            "currency": "EUR", "price_type": "fixed",
            "category": a.get("category_name"),
            "seller_type": "pro" if owner.get("type") == "pro" else "private",
            "seller_name": owner.get("name"),
            "seller_key": str(owner.get("user_id") or owner.get("store_id") or "") or None,
            "location_raw": loc.get("city_label") or loc.get("city"),
            "postal_code": loc.get("zipcode"), "country": "FR",
            "lat": loc.get("lat"), "lon": loc.get("lng"),
            "image": (a.get("images") or {}).get("thumb_url"),
            "images": json.dumps(imgs[:8]),
            "posted_at": _ts(a.get("first_publication_date")),
            "attrs": json.dumps({x.get("key"): x.get("value_label")
                                 for x in (a.get("attributes") or []) if x.get("key")}),
            "raw": json.dumps(a, ensure_ascii=False)[:20000],
        })
    return out

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
            "lat": None, "lon": None, "shipping": 0, "shipping_cost": None,
            "image": None, "images": "[]", "posted_at": None,
            "attrs": json.dumps({"source_path": "sitemap", "price_unknown": True}),
            "raw": json.dumps({"from": "sitemap", "url": u}),
        })
        if len(out) >= 60:
            break
    return out

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
DENIED_BY_OPERATOR = {
    "leboncoin": ("robots.txt interdit explicitement l'accès automatisé et "
                  "n'autorise que Googlebot/Bingbot/Slurp — accès sur "
                  "autorisation expresse uniquement"),
    "autoscout24": "Cloudflare refuse les robots identifiés ; sitemap 403 également",
    "motoscout24": "même protection qu'autoscout24",
    "immoscout24": "même protection qu'autoscout24",
}

LAST_STATUS = {}          # source -> (status, detail)
LAST_STRATEGY = {}        # source -> which tier actually produced the rows

def try_strategies(source, strategies):
    """Run extraction tiers in order until one yields rows.

    The tiers are about READING a page in different ways -- embedded JSON,
    schema.org, the rendered DOM -- not about getting access a different way.
    When a site refuses us, every tier gets the same refusal, and that is the
    site's decision to make.
    """
    blocked = False
    for name, fn in strategies:
        try:
            rows = fn() or []
        except Exception as e:
            print(f"  [{source}/{name}] {type(e).__name__}: {e}")
            continue
        if rows:
            LAST_STRATEGY[source] = name
            return rows, False
        if browser.last_reason() == "blocked" or net.LAST_BLOCKED.get("blocked"):
            blocked = True
    LAST_STRATEGY[source] = None
    return [], blocked

def probe_market(url):
    """Can we actually reach and read a suggested marketplace?

    The interview model has NO web access: it proposes sites from memory, and
    in testing 2 of 3 suggested domains did not resolve at all while the third
    existed with a wrong path. So every lead is verified before being shown.

    -> (status, detail) where status is
       dead      the domain does not resolve
       badurl    the site exists, that URL does not
       parsable  we could scrape it (structured listings found)
       manual    reachable and readable, but nothing structured to extract
    """
    from urllib.parse import urlparse
    u = urlparse(url)
    root = f"{u.scheme}://{u.netloc}/"
    r = net.get(url)
    if r is None or r.status_code >= 400:
        if net.get(root) is None:
            return "dead", "domaine inexistant"
        return "badurl", "site existant, URL de recherche fausse"
    html = r.text
    rows = jsonld_listings(html, url)
    if rows:
        return "parsable", f"{len(rows)} annonces via schema.org"
    nd = _next_data(html)
    items = find_lists(nd, ["ads", "edges", "listings", "results", "items", "products"]) if nd else None
    if items:
        return "parsable", f"{len(items)} éléments dans le state embarqué"
    return "manual", "lisible, mais rien de structuré à extraire"

def verify_markets(leads, workers=6):
    """Probe every lead at once and annotate it. Dead ones are dropped."""
    if not leads:
        return []
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(workers, len(leads))) as pool:
        results = list(pool.map(lambda m: probe_market(m.get("url", "")), leads))
    out = []
    for lead, (status, detail) in zip(leads, results):
        if status == "dead":
            continue                      # invented domain: not worth showing
        out.append({**lead, "status": status, "detail": detail})
    return out

def search(source, query, spec=None):
    """Run an adapter and record WHY it returned nothing.

    An empty list is ambiguous -- no stock, blocked, or signed out -- and the
    UI has to tell those apart to be useful.
    """
    fn = ADAPTERS.get(source)
    if not fn:
        LAST_STATUS[source] = ("error", "aucun adaptateur")
        return []
    try:
        rows = fn(query, spec)
    except Exception as e:
        LAST_STATUS[source] = ("error", f"{type(e).__name__}: {e}"[:200])
        print(f"  [{source}] adapter error: {type(e).__name__}: {e}")
        return []
    if rows:
        LAST_STATUS[source] = ("ok", f"{len(rows)} annonces")
    elif source in NEEDS_BROWSER:
        reason = browser.last_reason()
        LAST_STATUS[source] = ({"blocked": "blocked", "login": "login",
                                "locked": "error", "unavailable": "error"}.get(reason, "empty"),
                               {"blocked": "contrôle de sécurité",
                                "login": "connexion requise",
                                "locked": "profil navigateur déjà ouvert",
                                "unavailable": "navigateur indisponible"}.get(reason, ""))
    elif net.LAST_BLOCKED.get("blocked"):
        LAST_STATUS[source] = ("blocked", "contrôle de sécurité")
    else:
        LAST_STATUS[source] = ("empty", "0 résultat")
    return rows

def demo():
    """Parser check that does not depend on the network."""
    html = ('<script id="__NEXT_DATA__" type="application/json">'
            '{"props":{"pageProps":{"searchData":{"ads":[{"list_id":1,"subject":"iPhone 13",'
            '"url":"https://x/ad/1","price":[300],"location":{"zipcode":"1000","city":"Lausanne",'
            '"lat":46.5,"lng":6.6},"owner":{"type":"private","name":"bob"},"images":{}}]}}}}'
            '</script>')
    nd = _next_data(html)
    ads = find_lists(nd, ["ads"])
    assert ads and ads[0]["list_id"] == 1, "next_data walk broken"

    blob = 'x{"id":42,"title":"iPhone 13 Pro","buyNowPrice":450,"sellerNickname":"al"}y'
    objs = list(json_objects_with(blob, "buyNowPrice"))
    assert objs and objs[0]["id"] == 42, f"flight object scan broken: {objs}"

    assert _num("CHF 1'250.00") == 1250.0, _num("CHF 1'250.00")
    assert _num("450") == 450.0
    assert _ts("2026-06-28 17:16:45") is not None
    ld = jsonld_listings('<script type="application/ld+json">{"@type":"Product","name":"X",'
                         '"url":"/p/1","offers":{"price":"99","priceCurrency":"CHF"}}</script>',
                         "https://s.ch/")
    assert ld and ld[0]["price"] == 99.0 and ld[0]["url"] == "https://s.ch/p/1", ld
    assert set(NEEDS_BROWSER) <= set(ADAPTERS), "browser adapters not registered"
    assert not (set(DENIED_BY_OPERATOR) & set(ADAPTERS)), \
        "un site qui refuse ce robot ne doit avoir aucun adaptateur"
    print("sources ok: crawler =", ["anibis", "tutti", "ricardo(sitemap)", "leboncoin"],
          "| session utilisateur =", sorted(NEEDS_BROWSER),
          "| refusés =", sorted(DENIED_BY_OPERATOR))

if __name__ == "__main__":
    demo()
