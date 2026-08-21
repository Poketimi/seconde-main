"""Le registre des adaptateurs et le point d'entrée commun.

Un adaptateur est une fonction `(query, spec) -> [dict]`, inscrite par
`@adapter("nom")`. Ce module ne connaît aucun site : il tient la liste, appelle,
et traduit un résultat vide en une raison lisible. Voir SOURCES.md.
"""
import net, browser
# probe_market lit une page inconnue : il lui faut les extracteurs partagés.
# Ils manquaient depuis la découpe en paquet — le module s'importait très bien,
# et l'erreur ne sortait qu'à l'appel, dans l'assistant.
from .util import _next_data, find_lists, jsonld_listings
# probe_market lit une page inconnue : il lui faut les extracteurs partagés.
# Ils manquaient depuis la découpe en paquet — le module se chargeait, et
# l'erreur n'apparaissait qu'à l'appel, dans l'assistant.


ADAPTERS = {}
LAST_STATUS = {}          # source -> (status, detail)
LAST_STRATEGY = {}        # source -> which tier actually produced the rows

# Sites qui n'acceptent aucun client HTTP simple : leurs annonces vivent
# derrière la session de l'utilisateur. Rien ici ne contourne quoi que ce soit.
NEEDS_BROWSER = ("fb_marketplace",)

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
        # « locked » et « unavailable » sont des pannes DE NOTRE CÔTÉ : un
        # Chromium tué en plein scan (redémarrage de l'app), ou deux recherches
        # qui veulent le même profil. Les compter comme « error » mettait
        # Facebook en retrait exponentiel alors que le site n'avait rien
        # refusé — d'où des heures sans annonces après chaque redémarrage.
        reason = browser.last_reason()
        LAST_STATUS[source] = ({"blocked": "blocked", "login": "login",
                                "locked": "busy", "unavailable": "busy"}.get(reason, "empty"),
                               {"blocked": "contrôle de sécurité",
                                "login": "connexion requise",
                                "locked": "profil navigateur occupé — réessai au prochain cycle",
                                "unavailable": "navigateur indisponible ici"}.get(reason, ""))
    elif net.LAST_BLOCKED.get("blocked"):
        LAST_STATUS[source] = ("blocked", "contrôle de sécurité")
    else:
        LAST_STATUS[source] = ("empty", "0 résultat")
    return rows

