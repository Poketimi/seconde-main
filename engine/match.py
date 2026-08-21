"""Décider si une annonce correspond : mots-clés, modèle ciblé, règles, distance."""
import json, re, time, threading, subprocess, shutil, traceback
from concurrent.futures import ThreadPoolExecutor
import db, ai, geo, sources, config, i18n

_NOISE = {"occasion", "ski", "skis", "de", "la", "le", "les", "des", "du", "cm",
          "taille", "paire", "et", "avec", "pour", "neuf", "vendre"}

def _norm(t):
    t = (t or "").lower().translate(_ACCENTS)
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", t)).strip()

def target_tokens(name):
    """The tokens that actually identify a model: brand, model, and its number.

    A number is never optional -- Kore 93 and Kore 99 are different skis, and
    treating "99" as noise is how a keyword search returns the wrong ski.
    """
    out = []
    for w in _norm(name).split():
        if w in _NOISE and not w.isdigit():
            continue
        if len(w) >= 2 or w.isdigit():
            out.append(w)
    return out

CURRENCY = ("chf", "eur", "euro", "euros", "fr", "frs", "sfr")

def _number_belongs_to(word, num, hay, tight):
    """Is `num` this model's number, or just a price sitting next to the name?

    "Nordica Enforcer, 100 CHF" is a price; "Nordica Enforcer 100 - 186cm" is
    the model. The currency word right after is what separates them.
    """
    # `tight` keeps punctuation and only drops spaces, so "Enforcer, 100 CHF"
    # stays "enforcer,100" and is not mistaken for the model "Enforcer100"
    if f"{word}{num}" in tight:            # "QST99": never a price
        return True
    for m in re.finditer(rf"\b{re.escape(word)}\s+{re.escape(num)}\b", hay):
        after = hay[m.end():].strip().split(" ")
        if not after or after[0] not in CURRENCY:
            return True                    # at least one honest occurrence
    return False

def matches_target(title, target_name, description=None):
    """True only if EVERY identifying token of the model appears.

    Deliberately strict: with concrete models to look for, a partial match is
    almost always the wrong product rather than a near miss.
    """
    toks = target_tokens(target_name)
    if not toks:
        return False
    raw = f"{title} {description or ''}".lower().translate(_ACCENTS)
    hay = _norm(raw)
    words = set(hay.split())
    tight = re.sub(r"\s+", "", raw)    # sellers write "QST99" as often as "QST 99"
    for i, t in enumerate(toks):
        if not t.isdigit():
            if t not in hay:
                return False
            continue
        # A model number must sit next to its model word. Accepting it anywhere
        # made "Head Kore 93, 99 euros" match Kore 99 -- the 99 was the price.
        prev = toks[i - 1] if i else ""
        if prev and not prev.isdigit():
            if not _number_belongs_to(prev, t, hay, tight):
                return False
            continue
        if t not in words:      # leading number with nothing to anchor to
            return False
    return True

_ACCENTS = str.maketrans("àâäáãçèéêëìíîïñòóôöõùúûüýÿ", "aaaaaceeeeiiiinooooouuuuyy")

def dup_key(d):
    t = (d.get("title") or "").lower().translate(_ACCENTS)
    t = re.sub(r"[^a-z0-9]+", " ", t).strip()
    t = re.sub(r"\s+", " ", t)
    if not t:
        return None
    price = d.get("price")
    price = f"{float(price):.0f}" if price not in (None, "") else "?"
    where = (d.get("postal_code") or (d.get("location_raw") or "")[:12] or "?").strip().lower()
    return f"{t}|{price}|{where}"

COND_RANK = {"parts": 0, "fair": 1, "good": 2, "like_new": 3, "new": 4, "unknown": 2}

def passes_rules(d, s):
    p = d.get("price")
    if s["price_min"] is not None and p is not None and p < s["price_min"]:
        return False
    if s["price_max"] is not None and p is not None and p > s["price_max"]:
        return False
    if s["seller_type"] and s["seller_type"] != "any" and d.get("seller_type") != s["seller_type"]:
        return False
    blob = f"{d.get('title') or ''} {d.get('description') or ''}".lower()
    for kw in (s["exclude_kw"] or "").split(","):
        if kw.strip() and kw.strip().lower() in blob:
            return False
    return True

NOT_THE_ITEM = ("coque", "housse", "étui", "chargeur", "câble", "cable", "vitre",
                "verre trempé", "protection", "réparation", "reparation", "repair",
                "service", "pièces", "pieces", "hülle", "zubehör", "ersatzteil",
                "recherche", "cherche", "achète", "suche")

def keyword_score(d, s):
    """Fallback relevance when no AI key is configured.

    ponytail: title keywords only -- crude on purpose. Set DEEPSEEK_API_KEY and
    ai.analyse() replaces this with something that actually reads the ad.
    """
    title = (d.get("title") or "").lower()
    blob = f"{title} {(d.get('description') or '').lower()}"
    terms = [t for t in (s["query"] or "").lower().split() if len(t) >= 2]
    if not terms:
        return 50
    score = 100 * sum(1 for t in terms if t in blob) / len(terms)
    if any(w in title for w in NOT_THE_ITEM):
        score *= 0.2
    return score

def distance_ok(d, s, coords):
    """-> (ok, minutes, origin_label, mode). Shipped items skip distance."""
    origins = json.loads(s["origins"] or "[]")
    if not origins:
        return True, None, None, None
    if s["shipping_ok"] and d.get("shipping"):
        return True, None, "livraison", None
    if not coords:
        return False, None, None, None
    return geo.best_origin(coords, origins)

