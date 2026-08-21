"""L'entretien : questions fermées, puis critères et modèles concrets."""
import json, hashlib, re, time, shutil, subprocess
from concurrent.futures import ThreadPoolExecutor
import db, config, net
from .client import chat, _parse_json, looks_truncated, _slug
from .budget import smart_chat, budget_left
from .classify import norm_category

QUESTION_SYSTEM = """Tu aides quelqu'un à acheter un objet d'occasion dont il ne connaît
pas les critères. Pose 4 à 6 questions FERMÉES qui changent réellement le bon choix
(taille, poids, niveau, sexe, usage, budget). Pas de question dont la réponse ne
changerait pas la recherche.

Réponds en JSON uniquement :
{"category":"<slug court: ski|tennis|moto|velo|...>",
 "questions":[{"id":"snake_case","text":"question courte en français",
   "type":"choice"|"bool",
   "options":["..."],            // obligatoire et non vide si type=choice
   "scope":"global"|"domain"}]}

type="choice" = un seul choix, type="multi" = plusieurs réponses possibles,
type="bool" = oui/non. AUCUN autre type : jamais de texte libre, jamais de nombre à
saisir. Une mesure se pose en tranches ("160-170 cm").

Utilise "multi" quand plusieurs réponses peuvent être vraies en même temps (usages,
terrains pratiqués, marques acceptées, accessoires souhaités). Utilise "choice" quand
les options s'excluent (niveau, taille, sexe, budget).
scope="global" pour ce qui vaut pour tout achat (taille, sexe, poids),
scope="domain" pour ce qui ne vaut que pour cet objet (niveau de ski, type de terrain)."""

CRITERIA_SYSTEM = """Tu convertis un besoin en critères de recherche pour de l'occasion
en Suisse romande / France voisine. Réponds en JSON uniquement :

{"name":"nom court de la recherche",
 "query":"les mots que taperait un vendeur dans son titre",
 "category":"phone|computer|photo|audio|tv|console|game|furniture|appliance|clothing|sport|bike|car|moto|realestate|tool|other",
 "price_min":number|null, "price_max":number|null,
 "exclude_kw":"mots séparés par des virgules qui trahissent une mauvaise annonce",
 "condition_min":"new|like_new|good|fair|parts"|null,
 "targets":[{"name":"Marque Modèle Numéro","query":"ce qu'on tape dans la barre de recherche",
             "why":"raison courte"}],
 "sources":["parmi la liste fournie, uniquement celles qui ont du sens"],
 "other_markets":[{"name":"...","url":"URL de recherche, avec le terme si possible",
                   "why":"une raison courte","country":"CH|FR|EU"}],
 "tradeoffs":[{"demande":"ce que l'acheteur a dit vouloir",
                "propose":"ce que tu proposes à la place",
                "pourquoi":"la raison concrète, 1-2 phrases",
                "sinon":"ce qu'il faudrait accepter pour avoir quand même sa préférence"}],
 "explain":"une phrase expliquant tes choix à l'acheteur"}

query doit contenir les mots réellement présents dans les annonces, pas ta paraphrase.
Traduis les réponses en contraintes concrètes (une taille de skis se déduit de la taille
et du niveau). N'invente pas de budget si rien ne le suggère.

DÉSACCORDS — si tes modèles contredisent une préférence explicite de l'acheteur
(catégorie, marque, style, budget), tu DOIS le dire dans "tradeoffs". Ne passe jamais
outre en silence : il a le droit de savoir qu'on l'a contredit et pourquoi.
Exemple : il demande un GT, il mesure 1m90, tu proposes des roadsters → explique que sur
ce budget les GT adaptés à sa taille sont rares/hors budget, et dis ce qu'il devrait
accepter (budget plus élevé, modèle plus ancien) pour avoir un vrai GT.
Si tu respectes toutes ses préférences, renvoie une liste vide.

BUDGET — règle importante : un budget est un PLAFOND, jamais un plancher. Si l'acheteur
répond « 150-300€ », il veut dépenser AU PLUS 300€ ; une bonne affaire à 90€ l'intéresse
encore plus. Dans ce cas price_max=300 et price_min=null.
Ne mets un price_min que pour écarter des annonces manifestement fausses ou cassées, et
alors très bas (environ 10 % du plafond), jamais au niveau du budget annoncé.

targets : 6 à 10 MODÈLES PRÉCIS et réels, adaptés au profil, classés du plus au moins
pertinent (ex. « Salomon QST 99 », « Head Kore 99 », « Nordica Enforcer 100 »). C'est le
cœur de la recherche : on cherchera chaque modèle par son nom. N'invente aucun modèle ;
n'en propose que des courants sur le marché de l'occasion. `query` doit être court —
marque + modèle + numéro — sans « occasion » ni taille : c'est ce qu'on tape dans la
barre de recherche du site.

sources : ne coche que celles où l'objet se trouve VRAIMENT. Un appartement n'est pas sur
leboncoin pour un acheteur suisse ; une moto n'est pas sur ricardo.
other_markets : 3 à 6 sites d'occasion pertinents que la liste ne couvre PAS — généralistes
régionaux ou spécialisés du domaine (matériel de ski, vélo, photo…). Donne l'URL de
recherche réelle. Ne cite pas de site que tu ne connais pas."""

def _valid_questions(raw):
    """Keep only closed questions.

    The MCQ/true-false rule is enforced here, not in the prompt: a model that
    slips in a free-text field would otherwise put it straight in front of the
    user.
    """
    out = []
    for q in (raw or []):
        if not isinstance(q, dict):
            continue
        qid, text, typ = q.get("id"), q.get("text"), q.get("type")
        if not qid or not text or typ not in ("choice", "multi", "bool"):
            continue
        opts = [str(o) for o in (q.get("options") or []) if str(o).strip()]
        if typ in ("choice", "multi") and len(opts) < 2:
            continue
        out.append({"id": str(qid), "text": str(text), "type": typ,
                    "options": opts if typ in ("choice", "multi") else [],
                    "scope": "global" if q.get("scope") == "global" else "domain"})
    return out[:8]

def ask_questions(query):
    """-> (category, questions, from_cache).

    Cached per category AND per phrasing: the model answers "une paire de ski"
    with category "ski", so storing only under the category meant the lookup
    (keyed on the phrase) never hit and every interview was paid for twice.
    Both keys are written, so "des skis" reuses what "une paire de ski" built.
    """
    key = _slug(query)
    row = db.q("SELECT * FROM interview_templates WHERE cat_key=?", (key,), one=True)
    if row:
        try:
            qs = json.loads(row["questions"])
            if qs:
                return row["sample_query"] or row["cat_key"], qs, True
        except Exception:
            pass
    txt, _ = smart_chat(QUESTION_SYSTEM, f"Objet recherché : {query}", "questions")
    data = _parse_json(txt) or {}
    qs = _valid_questions(data.get("questions"))
    cat = _slug(data.get("category") or query) or key
    if len(qs) >= 2:
        blob = json.dumps(qs, ensure_ascii=False)
        for k in {cat, key}:              # category and the phrase the user typed
            db.run("""INSERT OR REPLACE INTO interview_templates
                      (cat_key,sample_query,questions,model,created_at)
                      VALUES(?,?,?,?,?)""",
                   (k, cat, blob, config.SMART_MODEL, time.time()))
    return cat, qs, False

FOLLOWUP_SYSTEM = """Tu affines un besoin d'achat d'occasion. On te donne l'objet et les
réponses déjà obtenues.

Pose UNIQUEMENT les questions qui changent encore le bon choix compte tenu de ces
réponses — typiquement pour préciser une réponse large. Exemple : « niveau avancé » ne
suffit pas, la répartition piste/freeride (80/20, 50/50) ou piste/freestyle change
complètement le ski recherché.

Si les réponses suffisent déjà à choisir, renvoie une liste vide. Ne repose jamais une
question déjà posée. Maximum 4 questions.

JSON uniquement, même format que précédemment :
{"questions":[{"id":"...","text":"...","type":"choice"|"multi"|"bool","options":[...],
               "scope":"global"|"domain"}]}
"multi" quand plusieurs réponses peuvent être vraies à la fois."""

MAX_ROUNDS = 3          # bounds both the user's patience and the cost

def followup_questions(query, answers, asked_ids=()):
    """Second (or third) wave, conditioned on what was already answered.

    Returns [] when the picture is complete, which ends the interview.
    """
    payload = json.dumps({"objet": query, "reponses": answers,
                          "deja_posees": sorted(asked_ids)}, ensure_ascii=False)
    txt, _ = smart_chat(FOLLOWUP_SYSTEM, payload, "relance")
    qs = _valid_questions((_parse_json(txt) or {}).get("questions"))
    return [q for q in qs if q["id"] not in set(asked_ids)]

def build_criteria(query, answers, profile=None, known_sources=None):
    """Answers + known profile -> criteria, source picks, and market leads."""
    payload = json.dumps({"objet": query, "reponses": answers,
                          "profil_connu": profile or {},
                          "sources_disponibles": sorted(known_sources or [])},
                         ensure_ascii=False)
    txt, _ = smart_chat(CRITERIA_SYSTEM, payload, "criteres")
    d = _parse_json(txt)
    if not d and looks_truncated(txt):
        # the answer was cut mid-JSON: ask again with room. This failed
        # silently before and fell back to the raw query.
        print(f"  [ia] réponse tronquée ({len(txt or '')} car.) — nouvelle tentative")
        txt, _ = smart_chat(CRITERIA_SYSTEM, payload, "criteres-long", max_tokens=12000)
        d = _parse_json(txt)
    if not d:
        txt, _ = smart_chat(CRITERIA_SYSTEM, payload, "criteres-retry")
        d = _parse_json(txt)
    ok = bool(d and d.get("query"))
    d = d or {}
    def num(v):
        try:
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None
    picks = [x for x in (d.get("sources") or []) if x in (known_sources or [])]
    targets = []
    for t in (d.get("targets") or []):
        if isinstance(t, dict) and t.get("name"):
            targets.append({"name": str(t["name"])[:80],
                            "query": str(t.get("query") or t["name"])[:80],
                            "why": str(t.get("why") or "")[:140]})
    leads = []
    for m in (d.get("other_markets") or []):
        if isinstance(m, dict) and m.get("name") and str(m.get("url", "")).startswith("http"):
            leads.append({"name": str(m["name"])[:60], "url": str(m["url"])[:300],
                          "why": str(m.get("why") or "")[:140],
                          "country": str(m.get("country") or "")[:4]})
    # The model still occasionally turns "150-300€" into a floor of 150, which
    # throws away the bargains the user most wants. A budget is a ceiling.
    pmin, pmax = num(d.get("price_min")), num(d.get("price_max"))
    if pmin and pmax and pmin > 0.25 * pmax:
        pmin = None
    elif pmin and not pmax:
        pmin = None            # a lone floor is almost always a misread budget
    trades = []
    for t in (d.get("tradeoffs") or []):
        if isinstance(t, dict) and t.get("pourquoi"):
            trades.append({"demande": str(t.get("demande") or "")[:120],
                           "propose": str(t.get("propose") or "")[:120],
                           "pourquoi": str(t.get("pourquoi") or "")[:400],
                           "sinon": str(t.get("sinon") or "")[:200]})
    return {"ok": ok, "sources": picks, "targets": targets[:10],
            "tradeoffs": trades[:4],
            "other_markets": leads[:6],
            "name": (d.get("name") or query)[:60],
            "query": d.get("query") or query,
            "category": norm_category(d.get("category")),
            "price_min": pmin, "price_max": pmax,
            "exclude_kw": d.get("exclude_kw") or "",
            "condition_min": d.get("condition_min") or None,
            "explain": d.get("explain") or ""}

