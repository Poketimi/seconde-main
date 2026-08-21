"""Les alertes que les sites t'envoient déjà, lues dans ta boîte mail.

leboncoin interdit le crawl — « Access is only permitted with special
permission from Leboncoin.fr » — et n'autorise que des robots nommés. Mais il
propose ses propres alertes : tu enregistres une recherche sur leur site, et
ils t'envoient les nouvelles annonces par e-mail.

C'est la même donnée, obtenue à l'envers : ce n'est plus nous qui allons la
chercher, c'est le site qui l'envoie, à sa cadence et de son plein gré. Aucun
robots.txt n'entre en jeu, aucune requête ne part vers leboncoin.

Aujourd'hui : leboncoin. Tout site qui refuse le crawl et propose des alertes
s'ajoute en une entrée dans SITES.

ACCÈS : IMAP en LECTURE SEULE, sur un mot de passe d'application dédié (Gmail,
iCloud, Proton...) — jamais le mot de passe principal du compte. Rien n'est
supprimé ni marqué lu.
"""
import email, imaplib, json, re, time
from email.header import decode_header, make_header
import config, db

# De qui viennent les alertes, et à quoi ressemble un lien d'annonce.
SITES = {
    # Vide : leboncoin passe maintenant par son API (sources/leboncoin.py).
    # Rajouter un site ici recrée son adaptateur automatiquement — c'est une
    # ligne, et tout le reste (IMAP, extraction, réglages) est déjà en place.
}

# Les séparateurs de milliers ne valent que par groupes de trois. Sans cette
# contrainte, « Cannondale CAAD13 1 250 € » se lisait comme un seul nombre :
# 131250 — même famille de bug que le vélo à 2100 CHF enregistré à 2.00.
PRICE = re.compile(
    r"(\d{1,3}(?:[ \u00a0\u202f'\u2019.]\d{3})+(?:[.,]\d{1,2})?"
    r"|\d+(?:[.,]\d{1,2})?)\s*(?:€|EUR|CHF|Fr\.)", re.I)
LAST_ERROR = [""]

def configured():
    return bool(config.IMAP_HOST and config.IMAP_USER and config.IMAP_PASSWORD)

def _decode(v):
    try:
        return str(make_header(decode_header(v or "")))
    except Exception:
        return v or ""

def _body(msg):
    """Le HTML du message, ou le texte à défaut."""
    best = ""
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        if part.get_content_maintype() == "multipart":
            continue
        try:
            raw = part.get_payload(decode=True) or b""
            txt = raw.decode(part.get_content_charset() or "utf-8", "replace")
        except Exception:
            continue
        if part.get_content_type() == "text/html":
            return txt
        best = best or txt
    return best

def site_of(sender):
    s = (sender or "").lower()
    for name, cfg in SITES.items():
        if any(d in s for d in cfg["from"]):
            return name
    return None

def parse(site, html, seen_at=None):
    """Annonces extraites d'un e-mail d'alerte, au format des adaptateurs.

    Le gabarit exact de ces e-mails n'est pas documenté et change ; on lit donc
    ce qui est structurellement stable — les liens vers les annonces — et on
    récupère titre et prix dans le voisinage quand ils y sont, sans jamais
    inventer un prix absent.
    """
    cfg = SITES.get(site)
    if not cfg or not html:
        return []
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        soup = None
    now = seen_at or time.time()
    out, seen = [], set()

    anchors = soup.find_all("a", href=True) if soup else []
    for a in anchors:
        m = re.search(cfg["link"], a["href"])
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        # le titre est le texte du lien, ou celui du bloc qui le contient
        title = " ".join((a.get_text(" ") or "").split())
        # le prix est rarement DANS le lien : on lit le bloc qui l'entoure.
        # Sans bloc reconnaissable, on prend tout le message — mais seulement
        # s'il ne contient qu'une annonce, sinon on attraperait le prix d'une
        # autre.
        holder = a.find_parent(["td", "tr", "li", "div", "table"])
        if holder is None and soup is not None and len(anchors) <= 2:
            holder = soup
        around = " ".join((holder.get_text(" ") if holder else "").split())
        if len(title) < 4:
            title = around[:120]
        pm = PRICE.search(around) or PRICE.search(title)
        out.append({
            "url": a["href"].split("?")[0], "source": site,
            "source_id": m.group(1), "title": title[:200] or None,
            "description": None,
            "price": _num(pm.group(1)) if pm else None,
            "currency": "EUR" if site == "leboncoin" else "CHF",
            "price_type": "fixed", "country": "FR" if site == "leboncoin" else "CH",
            "first_seen": now, "posted_at": now,
            "attrs": json.dumps({"via": "alerte e-mail"}),
            "raw": json.dumps({"via": "email", "site": site}),
        })
    return [o for o in out if o["title"]]

_THOUSANDS = "'’‘`    ⁠ "

def _num(v):
    if v is None:
        return None
    t = str(v)
    for ch in _THOUSANDS:
        t = t.replace(ch, "")
    t = t.replace(",", ".")
    try:
        return float(re.sub(r"[^\d.]", "", t) or 0) or None
    except ValueError:
        return None

def fetch(days=7, limit=200):
    """Toutes les annonces trouvées dans les alertes récentes. LECTURE SEULE."""
    if not configured():
        LAST_ERROR[0] = "IMAP non configuré"
        return []
    try:
        M = imaplib.IMAP4_SSL(config.IMAP_HOST, config.IMAP_PORT)
        M.login(config.IMAP_USER, config.IMAP_PASSWORD)
    except Exception as e:
        LAST_ERROR[0] = f"connexion refusée : {type(e).__name__} — " \
                        f"utilise un mot de passe d'application, pas celui du compte"
        return []
    out = []
    try:
        # readonly=True : rien n'est marqué lu, rien n'est déplacé, rien n'est effacé
        M.select(config.IMAP_FOLDER, readonly=True)
        since = time.strftime("%d-%b-%Y", time.localtime(time.time() - days * 86400))
        for site, cfg in SITES.items():
            for dom in cfg["from"]:
                typ, data = M.search(None, f'(SINCE {since} FROM "{dom}")')
                if typ != "OK":
                    continue
                ids = (data[0] or b"").split()[-limit:]
                for i in ids:
                    typ, raw = M.fetch(i, "(BODY.PEEK[])")   # PEEK : ne marque pas lu
                    if typ != "OK" or not raw or not raw[0]:
                        continue
                    msg = email.message_from_bytes(raw[0][1])
                    out += parse(site, _body(msg))
    except Exception as e:
        LAST_ERROR[0] = f"lecture impossible : {type(e).__name__}: {e}"
    finally:
        try:
            M.logout()
        except Exception:
            pass
    # une même annonce revient dans plusieurs alertes
    uniq = {}
    for o in out:
        uniq.setdefault(o["url"], o)
    LAST_ERROR[0] = "" if uniq else (LAST_ERROR[0] or "aucune alerte trouvée")
    return list(uniq.values())

def probe():
    """(ok, message) pour le bouton Tester."""
    if not configured():
        return False, ("Renseigne le serveur IMAP, l'adresse et un mot de passe "
                       "d'application, puis crée des alertes sur leboncoin.")
    rows = fetch(days=30)
    if not rows:
        return False, (LAST_ERROR[0] or "Connexion établie, mais aucune alerte "
                       "reconnue dans les 30 derniers jours.")
    per = {}
    for r in rows:
        per[r["source"]] = per.get(r["source"], 0) + 1
    return True, "Alertes lues : " + ", ".join(f"{k} {v}" for k, v in sorted(per.items()))

def demo():
    # SITES est vide tant qu'aucun site n'est branché sur les alertes : le test
    # de l'extracteur ne doit pas dépendre de ce réglage-là.
    SITES.setdefault("leboncoin", {
        "from": ("leboncoin.fr",),
        "link": r"https?://(?:www\.)?leboncoin\.fr/(?:ad|vi)/[\w/-]*?(\d{6,})"})
    try:
        _demo_body()
    finally:
        if not _CONFIGURED_SITES:
            SITES.pop("leboncoin", None)

_CONFIGURED_SITES = dict(SITES)      # figé à l'import : ce qui est vraiment branché

def _demo_body():
    html = """<html><body>
      <table><tr><td>
        <a href="https://www.leboncoin.fr/ad/velos/2891234567?utm=alert">
           Vélo de course Cannondale CAAD13</a>
        <span>1 250 €</span> — Annemasse (74)
      </td></tr>
      <tr><td><a href="https://www.leboncoin.fr/ad/motos/2891999888">Yamaha Tracer 900</a>
        <span>6 900 €</span></td></tr>
      <tr><td><a href="https://example.com/pub">Publicité</a></td></tr>
      </table></body></html>"""
    rows = parse("leboncoin", html)
    assert len(rows) == 2, rows
    a, b = rows
    assert a["source"] == "leboncoin" and a["source_id"] == "2891234567"
    assert a["url"].endswith("2891234567"), a["url"]   # le tracking est retiré
    assert "Cannondale" in a["title"]
    assert a["price"] == 1250.0, a["price"]            # espace insécable géré
    assert a["currency"] == "EUR" and a["country"] == "FR"
    assert b["price"] == 6900.0
    assert parse("leboncoin", "") == [] and parse("inconnu", html) == []
    assert site_of("alerte@leboncoin.fr") == "leboncoin"
    assert site_of("spam@example.com") is None
    # sans prix affiché, ne rien inventer
    solo = parse("leboncoin", '<a href="https://www.leboncoin.fr/ad/x/2891234567">Titre seul</a>')
    assert solo and solo[0]["price"] is None
    print("mailbox ok")

if __name__ == "__main__":
    import sys
    if "test" in sys.argv:
        ok, msg = probe()
        print(("✓ " if ok else "✗ ") + msg)
        for r in fetch(days=30)[:10]:
            print(f"  [{r['source']}] {r['price']} {r['currency']}  {r['title'][:60]}")
    else:
        demo()
