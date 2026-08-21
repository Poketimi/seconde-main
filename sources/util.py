"""Extraction partagée : JSON embarqué, galeries, nombres, dates.

Rien ici ne connaît de site en particulier. `_num` en particulier mérite d'être
réutilisé plutôt que réécrit : les séparateurs de milliers ont déjà transformé
un vélo à 2100 CHF en 2.00.
"""
import re, json, time
from urllib.parse import quote_plus, urljoin
import net, browser, crawler

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
    """Images, description et lien du vendeur, en UNE seule vue de page.

    Trois visites séparées tripleraient notre empreinte sur facebook pour rien —
    et c'est le compte personnel de l'utilisateur qui est en jeu.

    La description ne figure PAS dans les résultats de recherche, seulement sur
    la page de l'annonce : d'où 0 description sur 237 annonces facebook tant
    qu'on ne la lisait pas ici.
    """
    js = """() => {
        const own = i => !i.closest('a[href*="/marketplace/item/"]');
        const imgs = [...document.querySelectorAll('img')]
            .filter(i => (i.src || '').includes('fbcdn')
                      && (i.naturalWidth || i.width) >= 300 && own(i))
            .map(i => i.src);
        const a = document.querySelector('a[href*="/marketplace/profile/"]');
        // La description est le plus long bloc de texte de la page qui ne soit
        // ni un lien ni un bouton. Facebook change ses classes en permanence ;
        // viser la structure plutôt qu'un sélecteur qui cassera au prochain
        // déploiement.
        let best = "";
        for (const el of document.querySelectorAll('div[dir="auto"], span[dir="auto"]')) {
            if (el.closest('a,button,[role="button"],nav,form')) continue;
            if (el.querySelector('div[dir="auto"],span[dir="auto"]')) continue;
            const t = (el.innerText || "").trim();
            if (t.length > best.length) best = t;
        }
        return {images: imgs, profile: a ? a.getAttribute('href') : null,
                description: best.length >= 25 ? best : null};
    }"""
    out = browser.eval_page(url, js, wait_ms=6000, wait_for="img") or {}
    imgs, seen = [], set()
    for u in (out.get("images") or []):
        if u not in seen:
            seen.add(u); imgs.append(u)
    prof = out.get("profile") or ""
    m = re.search(r"/marketplace/profile/(\d+)", prof)
    desc = out.get("description") or None
    return {"images": imgs[:12],
            "description": desc,
            "shipping": delivery_from_text(desc),
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
# --- livraison ---------------------------------------------------------
# anibis, tutti et facebook ne publient AUCUN champ de livraison : la seule
# source est ce que le vendeur écrit. En allemand, français et italien, parce
# que c'est la Suisse.
#
# Deux pièges symétriques, tous deux vérifiés sur des annonces réelles :
#   « Abholung in Zürich oder Versand gegen Aufpreis » -> livrable
#   « Keine Garantie. Kein Umtausch. Versand möglich. » -> livrable
# Mentionner le retrait n'exclut pas l'envoi, et « kein » devant autre chose
# ne nie pas l'envoi.
_SHIP_YES = re.compile(
    r"(versand\s*(m[oö]glich|gegen|inkl|:|kostet|ab\b)"
    r"|postversand|per\s+post|verschick\w+|liefer(ung|bar)\s|paketversand"
    r"|envoi\s+(possible|contre|en\s+sus)|j['e]\s*envoie|exp[ée]dition\s+possible"
    r"|spedizione\s+(possibile|contro)|posso\s+spedire)", re.I)
_SHIP_NO = re.compile(
    r"(kein\w*\s+versand|nicht\s+versand|nur\s+abholung|abholung\s+nur"
    r"|selbstabholung\s+nur|nur\s+selbstabholung"
    r"|pas\s+d['e]\s*envoi|aucun\s+envoi|retrait\s+(uniquement|seulement)"
    r"|main\s*propre[^.!?\n]{0,30}\b(uniquement|seulement|exclusivement)"
    r"|\b(uniquement|seulement)[^.!?\n]{0,30}main\s*propre"
    r"|je\s+n['e]\s*(envoie|exp[ée]die)\s*pas"
    r"|solo\s+ritiro|ritiro\s+in\s+loco)", re.I)

def delivery_from_text(text):
    """1 livrable, 0 retrait seulement, None si le vendeur n'en dit rien.

    Deux affirmations contradictoires dans la même annonce -> None. Mieux vaut
    « on ne sait pas » qu'un pile ou face.
    """
    t = text or ""
    yes, no = bool(_SHIP_YES.search(t)), bool(_SHIP_NO.search(t))
    if yes and no:
        return None
    return 1 if yes else (0 if no else None)

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

