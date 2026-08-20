"""New-price reference for a product, to judge whether a used price is good.

Knowing a 9800X3D is ~332 CHF new makes "265 used" instantly readable — far
more useful than comparing only against other used listings.

Source: toppreise.ch, a Swiss price comparison site. That choice also settles
the delivery question: it lists Swiss retailers, so anything quoted there ships
within Switzerland. Foreign retailers (amazon.de and friends) are deliberately
NOT used — their CH deliverability is unreliable and unverifiable from a price.
"""
import re, time
from urllib.parse import quote_plus
import db, net

REFRESH_AFTER = 7 * 86400
SEARCH = "https://www.toppreise.ch/produktsuche?q={q}"
MIN_PLAUSIBLE, MAX_PLAUSIBLE = 5, 20000

def _text(html):
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", t))

def _tokens(name):
    return [w for w in re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).split() if len(w) > 1]

def rows(html):
    """(name, price) per result row.

    Scraping loose numbers off the page cannot tell whose price is whose: it
    returned 7.50 for an Apple TV 4K, which was a replacement remote sitting
    further down the results.
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for n in soup.select(".product-name"):
        node, txt = n, None
        for _ in range(6):
            node = node.parent
            if node is None:
                break
            pc = node.select_one(".productPrice, .priceContainer")
            if pc:
                txt = pc.get_text(" ")
                break
        if not txt:
            continue
        m = re.search(r"CHF\s*([\d'’ ]{1,9}(?:[.,]\d{2})?)", txt)
        if not m:
            continue
        try:
            v = float(m.group(1).replace("'", "").replace("’", "").replace(" ", "").replace(",", "."))
        except ValueError:
            continue
        if MIN_PLAUSIBLE < v < MAX_PLAUSIBLE:
            out.append((re.sub(r"\s+", " ", n.get_text(" ")).strip(), v))
    return out

# "Ersatzfernbedienung FÜR Apple TV 4K" contains the product name and costs
# 7.50 -- accessories name the thing they fit, so the preposition is the tell.
FOR_WORDS = (" für ", " fuer ", " pour ", " for ", " compatible ", " kompatibel ",
             " adapté ", " passend ")
ACCESSORY_WORDS = ("hülle", "case", "cover", "coque", "housse", "fernbedienung",
                   "remote", "kabel", "cable", "câble", "halterung", "support",
                   "adapter", "adaptateur", "schutz", "protection", "tasche",
                   "ersatz", "spare", "sticker", "skin")

def is_accessory(title, product_name):
    t = f" {title.lower()} "
    if any(w in t for w in ACCESSORY_WORDS):
        return True
    # the product named after "für/pour/for" is what the item FITS, not what it is
    for w in FOR_WORDS:
        i = t.find(w)
        if i > 0 and product_name.split()[0].lower() in t[i:]:
            return True
    return False

def lookup(product_name):
    """Cheapest NEW price in CHF for this exact product, or None.

    Only rows whose title really is the product count -- accessories for it are
    the trap, and they are always cheaper.
    """
    if not product_name:
        return None
    url = SEARCH.format(q=quote_plus(product_name))
    r = net.get(url)
    if r is None or r.status_code >= 400:
        return None
    from engine import matches_target          # same strict rule as the scan
    cands = [(n, v) for n, v in rows(r.text) if matches_target(n, product_name)]
    if not cands:
        return None

    # A keyword blacklist kept leaking: "Ersatzfernbedienung für Apple TV 4K",
    # then "APPLE TV SECURITY MOUNT (4K)". Telling an item from an accessory
    # for it is exactly what ai.analyse already does well on listings, so use
    # that instead of growing the blacklist forever.
    import ai
    # Drop the obvious accessories with the cheap rule FIRST. Taking the 8
    # cheapest rows straight away buried the real Apple TV (182 CHF) under
    # eight mounts and remotes, and the lookup returned nothing at all.
    obvious = [(n, v) for n, v in cands if not is_accessory(n, product_name)]
    cands = sorted(obvious or cands, key=lambda x: x[1])[:8]
    keep = cands
    if ai.available():
        verdicts = ai.analyse([{"title": n, "price": v, "currency": "CHF"} for n, v in cands],
                              f"{product_name} neuf, l'appareil lui-même et pas un accessoire")
        if verdicts:
            keep = [(n, v) for i, (n, v) in enumerate(cands)
                    if (verdicts.get(i) or {}).get("is_item")
                    and float((verdicts.get(i) or {}).get("score") or 0) >= 60]
    if not keep:
        return None
    name, price = min(keep, key=lambda x: x[1])
    return {"price": price, "url": url, "source": "toppreise", "matched": name}

def for_product(pid, name, force=False):
    """Cached per product; refreshed weekly."""
    p = db.q("SELECT ref_price, ref_url, ref_source, ref_checked FROM products WHERE id=?",
             (pid,), one=True)
    if p and not force and p["ref_checked"] and time.time() - p["ref_checked"] < REFRESH_AFTER:
        return dict(p) if p["ref_price"] else None
    hit = lookup(name)
    db.run("""UPDATE products SET ref_price=?, ref_url=?, ref_source=?, ref_checked=?
              WHERE id=?""",
           (hit["price"] if hit else None, hit["url"] if hit else None,
            hit["source"] if hit else None, time.time(), pid))
    return {"ref_price": hit["price"], "ref_url": hit["url"],
            "ref_source": hit["source"]} if hit else None

def demo():
    assert _tokens("AMD Ryzen 7 9800X3D") == ["amd", "ryzen", "9800x3d"]
    assert lookup("") is None
    assert lookup("zzzz produit inexistant 12345") is None, \
        "une page sans le produit ne doit pas rendre un prix"
    print("reference ok")

if __name__ == "__main__":
    demo()
