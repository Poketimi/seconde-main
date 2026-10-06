"""Quelles annonces valent vraiment le coup d'œil, parmi celles qui matchent.

Le score de tri répond à « est-ce bien la chose demandée ». C'est une autre
question : parmi les choses qui LE sont, laquelle est la bonne affaire ?

Les signaux existent déjà en base — médiane du produit, écart à cette médiane,
état, ancienneté du compte vendeur, trajet, fin d'enchère. Ce module ne fait
que les rassembler et demander au modèle de désigner ce qui sort du lot, avec
une raison en une phrase.

Il a le droit de ne rien désigner. Un lot de trente annonces médiocres doit
produire zéro recommandation, pas trois enthousiasmes fabriqués.
"""
import hashlib, json, time
from seconde_main import db, config
from .client import chat, _parse_json

MAX_CANDIDATES = 25          # au-delà, le modèle survole plus qu'il ne lit
MAX_PICKS = 3

SYSTEM = """Tu conseilles un acheteur d'occasion. On te donne des annonces qui
correspondent DÉJÀ à sa recherche : la pertinence n'est pas la question.

La question : lesquelles sont de bonnes affaires, et pourquoi ?

Ce qui rend une annonce intéressante :
- un prix nettement sous la médiane du même produit (`ecart_median` négatif) ;
- un état meilleur que les autres au même prix ;
- un vendeur ancien et bien noté, surtout face à un compte récent ;
- une enchère qui finit bientôt sans surenchère ;
- un accessoire ou un lot inclus qui change la valeur.

Ce qui doit te rendre méfiant, et que tu dois DIRE :
- un prix très en dessous du marché sans raison visible ;
- un compte vendeur récent avec peu d'historique ;
- un état non précisé sur un objet cher.

Choisis AU PLUS {max_picks} annonces. Tu peux n'en choisir aucune : si rien ne
sort du lot, renvoie une liste vide. N'invente jamais un argument qui ne
figure pas dans les données fournies — pas de « très demandé », pas de « rare »,
rien que tu ne puisses lire ci-dessous.

Deuxième question, indépendante : **la recherche elle-même est-elle bien réglée ?**
Propose un ajustement SEULEMENT si tu vois dans les données une raison précise :

- toutes les annonces butent contre le plafond de prix -> le plafond est trop bas ;
- une seule variante remonte alors que la requête en couvre plusieurs -> requête trop étroite ;
- un mot exclu écarte visiblement de bonnes annonces -> exclusion trop large ;
- le lot est minuscule alors que l'objet est courant -> requête trop spécifique.

Pas de raison visible = pas d'ajustement. Ne propose jamais d'élargir « au cas où ».

Réponds en JSON uniquement :
{{"picks":[{{"id":<id de l'annonce>,"pourquoi":"une phrase, concrète, en français"}}],
  "resume":"une phrase sur l'ensemble du lot, ou \\"\\" si rien à dire",
  "ajustement":{{"query":"<nouvelle requête ou null>",
                "price_max":<nombre ou null>,
                "exclude_kw":"<nouvelle liste ou null>",
                "pourquoi":"une phrase disant ce que ça devrait débloquer"}}}}
"ajustement" vaut null s'il n'y a rien à changer."""

EMPTY_SYSTEM = """Une recherche d'occasion ne remonte AUCUNE annonce. Dis pourquoi,
et propose un réglage plus large.

Causes possibles, par ordre de fréquence : requête trop spécifique (référence
exacte, année, variante), plafond de prix sous le marché, mot exclu trop large,
objet réellement rare.

Reste proche de l'intention : élargir « Peak Design Everyday 30L V2 » en
« Peak Design 30L » est utile, en « sac à dos » ne l'est pas — ça noierait
l'utilisateur.

Réponds en JSON uniquement :
{"ajustement":{"query":"<requête élargie ou null>",
               "price_max":<nombre ou null>,
               "exclude_kw":"<nouvelle liste ou null>",
               "pourquoi":"une phrase : ce qui coince, et ce que ça débloque"}}
Renvoie "ajustement": null si la recherche te paraît juste et l'objet simplement rare."""


def _brief(m):
    """Ce que le modèle a besoin de savoir sur une annonce. Rien de plus."""
    d = {"id": m["listing_id"], "titre": (m["title"] or "")[:110],
         "prix": m["price"], "monnaie": m["currency"]}
    if m["price_median"]:
        d["mediane_produit"] = round(m["price_median"])
        d["annonces_comparables"] = m["n_listings"]
    if m["deal_delta"] is not None:
        d["ecart_median_pct"] = round(m["deal_delta"])
    if m["condition"]:
        d["etat"] = m["condition"]
    if m["travel_minutes"] is not None:
        d["trajet_min"] = round(m["travel_minutes"])
    if m["shipping"] == 1:
        d["livrable"] = True
    if m["auction_end"]:
        d["fin_enchere_h"] = round((m["auction_end"] - time.time()) / 3600, 1)
        d["encheres"] = m["bids"]
    if m["member_since"]:
        d["vendeur_depuis"] = m["member_since"]
    if m["rating"] is not None:
        d["vendeur_note"] = m["rating"]
    d["vue_il_y_a_h"] = round((time.time() - (m["first_seen"] or 0)) / 3600)
    return d


def candidates(search_id):
    return db.q("""SELECT m.listing_id, m.deal_delta, m.travel_minutes,
                          l.title, l.price, l.currency, l.condition, l.shipping,
                          l.auction_end, l.bids, l.first_seen,
                          p.price_median, p.n_listings,
                          s.member_since, s.rating
                   FROM matches m
                   JOIN listings l ON l.id = m.listing_id
                   LEFT JOIN products p ON p.id = l.product_id
                   LEFT JOIN sellers s ON s.id = l.seller_ref
                   WHERE m.search_id = ? AND l.active = 1
                   ORDER BY m.score DESC, m.created_at DESC
                   LIMIT ?""", (search_id, MAX_CANDIDATES))


def fingerprint(rows):
    """Change dès qu'une annonce entre, sort, ou change de prix."""
    key = "|".join(f"{r['listing_id']}:{r['price']}" for r in rows)
    return hashlib.sha256(key.encode()).hexdigest()[:16]


# Champs qu'un ajustement a le droit de toucher. Tout le reste — origines,
# sources, type de vendeur — reste la décision de l'utilisateur.
TWEAKABLE = ("query", "price_max", "exclude_kw")


def _clean_tweak(raw, search):
    """Garde un ajustement s'il change vraiment quelque chose de permis."""
    if not isinstance(raw, dict):
        return None
    out = {}
    _MISSING = object()
    for k in TWEAKABLE:
        v = raw.get(k, _MISSING)
        # None = « rien à changer ». "" sur un champ texte = « vider ce champ »,
        # ce qui est un ajustement légitime : c'est ainsi qu'on retire une
        # exclusion qui écarte justement ce qu'on cherche.
        if v is _MISSING or v is None or v == []:
            continue
        if k == "price_max":
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            # un plafond revu à la baisse n'élargit rien : c'est une erreur
            if search["price_max"] and v <= float(search["price_max"]):
                continue
        else:
            v = str(v).strip()
            if v == (search[k] or "").strip():
                continue                 # identique : ce n'est pas un ajustement
        out[k] = v
    if not out:
        return None
    out["pourquoi"] = _trim(raw.get("pourquoi"), 400)
    return out


def _trim(txt, n):
    """Couper sur un mot, pas au milieu : « devrait faire rem » n'aide personne."""
    t = str(txt or "").strip()
    if len(t) <= n:
        return t
    cut = t[:n]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > n * 0.6 else cut).rstrip(" ,;") + "…"


def _save_tweak(search, raw, key):
    t = _clean_tweak(raw, search)
    db.run("UPDATE searches SET tweak_json=?, tweak_key=? WHERE id=?",
           (json.dumps(t, ensure_ascii=False) if t else None, key, search["id"]))
    return t


def suggest_when_empty(search, force=False):
    """Aucune annonce : proposer un réglage plus large. Retourne l'ajustement.

    Jamais appliqué tout seul — l'utilisateur voit l'avant/après et décide.
    """
    key = "empty:" + hashlib.sha256(
        f"{search['query']}|{search['price_max']}|{search['exclude_kw']}".encode()
    ).hexdigest()[:16]
    if not force and search["tweak_key"] == key:
        return None                  # déjà proposé pour cette configuration
    payload = json.dumps({"requete": search["query"],
                          "prix_max": search["price_max"],
                          "mots_exclus": search["exclude_kw"],
                          "reference": search["reference"]}, ensure_ascii=False)
    data = _parse_json(chat(EMPTY_SYSTEM, payload, job="reco", max_tokens=500))
    if data is None:
        return None
    return _save_tweak(search, data.get("ajustement"), key)


def recommend(search, force=False):
    """Écrit les recommandations sur les matchs. Retourne le nombre retenu.

    Ne rappelle pas le modèle quand le lot n'a pas bougé : un scan qui ne
    trouve rien de neuf ne doit rien coûter.
    """
    rows = candidates(search["id"])
    if not rows:
        # Rien du tout : c'est l'autre question — la recherche est-elle bien
        # réglée ? Un lot vide ne se commente pas, il se diagnostique.
        suggest_when_empty(search, force=force)
        return 0
    if len(rows) < 2:
        return 0                     # rien à comparer, rien à conseiller
    fp = fingerprint(rows)
    if not force and search["reco_key"] == fp:
        return 0
    payload = json.dumps({"recherche": search["query"],
                          "budget_max": search["price_max"],
                          "annonces": [_brief(r) for r in rows]},
                         ensure_ascii=False)
    txt = chat(SYSTEM.format(max_picks=MAX_PICKS), payload, job="reco",
               max_tokens=1200)
    data = _parse_json(txt)
    if data is None:
        return 0                     # pas de clé, ou modèle muet : on réessaiera

    known = {r["listing_id"] for r in rows}
    picks = [p for p in (data.get("picks") or [])
             if isinstance(p, dict) and p.get("id") in known][:MAX_PICKS]

    db.run("UPDATE matches SET reco_rank=NULL, reco_why=NULL WHERE search_id=?",
           (search["id"],))
    for rank, p in enumerate(picks, 1):
        db.run("UPDATE matches SET reco_rank=?, reco_why=? "
               "WHERE search_id=? AND listing_id=?",
               (rank, str(p.get("pourquoi") or "")[:400], search["id"], p["id"]))
    db.run("UPDATE searches SET reco_key=?, reco_summary=? WHERE id=?",
           (fp, str(data.get("resume") or "")[:300], search["id"]))
    _save_tweak(search, data.get("ajustement"), fp)
    return len(picks)


def demo():
    db.init()
    rows = [{"listing_id": 1, "price": 100}, {"listing_id": 2, "price": 200}]
    a = fingerprint(rows)
    assert a == fingerprint(rows), "empreinte instable"
    rows[1] = {"listing_id": 2, "price": 180}
    assert a != fingerprint(rows), "un changement de prix doit rouvrir le lot"

    m = {"listing_id": 7, "title": "Vélo", "price": 300.0, "currency": "CHF",
         "price_median": 500, "n_listings": 12, "deal_delta": -40.0,
         "condition": "good", "travel_minutes": 18.0, "shipping": 1,
         "auction_end": None, "bids": None, "first_seen": time.time() - 7200,
         "member_since": 2015, "rating": 4.8}
    b = _brief(m)
    assert b["ecart_median_pct"] == -40 and b["mediane_produit"] == 500
    assert b["livrable"] is True and b["vendeur_depuis"] == 2015
    assert "fin_enchere_h" not in b, "pas d'enchère, pas de compte à rebours"
    m2 = dict(m, price_median=None, deal_delta=None, condition=None,
              travel_minutes=None, shipping=None, member_since=None, rating=None)
    assert "mediane_produit" not in _brief(m2), "un champ absent ne doit rien inventer"
    print("recommend ok")


if __name__ == "__main__":
    demo()
