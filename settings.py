"""Fournisseur, clé et modèles — réglables depuis l'app, pas depuis .env.

ai.chat lit config.AI_* à chaque appel : réécrire ces attributs sur `config`
prend effet à la requête suivante, sans redémarrage.

La clé vit en base plutôt que dans .env parce que l'app doit pouvoir la
changer. Ce n'est pas un coffre-fort : data/market.db est en clair sur ta
machine, exactement comme .env l'était. Elle n'est jamais renvoyée à la page —
seuls les derniers caractères sont affichés.
"""
import time
import db, config

# Ce que la page de réglages possède. Le reste reste dans config.py.
FIELDS = ("AI_PROVIDER", "AI_BASE_URL", "AI_MODEL", "AI_API_KEY",
          "SMART_MODEL", "SMART_BUDGET_USD",
          # compte de repli, utilisé quand le principal n'a plus de jetons
          "ALT_PROVIDER", "ALT_BASE_URL", "ALT_MODEL", "ALT_API_KEY",
          # abonnement Claude Code, entretien de l'assistant uniquement
          "CLAUDE_CLI", "CLAUDE_CLI_MODEL",
          # eBay : API officielle, pas du crawl
          "EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET", "EBAY_MARKETPLACE")
SECRET = ("AI_API_KEY", "ALT_API_KEY", "EBAY_CLIENT_SECRET")

def stored():
    return {r["k"]: r["v"] for r in db.q("SELECT k,v FROM settings")}

def _coerce(k, v):
    if k == "CLAUDE_CLI":
        return str(v).strip() in ("1", "true", "on", "oui")
    if k == "SMART_BUDGET_USD":
        try:
            return float(str(v).replace(",", "."))
        except ValueError:
            return None
    v = str(v).strip()
    return v.rstrip("/") if k.endswith("BASE_URL") else v

def apply(values):
    """Écrit ces réglages sur `config`. Une valeur vide ne remplace rien."""
    for k, v in values.items():
        if k not in FIELDS or v is None or (v == "" and k != "CLAUDE_CLI"):
            continue
        v = _coerce(k, v)
        if v is None or v == "":
            continue
        setattr(config, k, v)
    # la chaîne de repli dépend du modèle ET du fournisseur : la recalculer
    config.AI_FALLBACKS = config.fallback_chain()

def load():
    """Au démarrage : la base a le dernier mot sur .env."""
    apply(stored())

def save(values):
    now = time.time()
    for k, v in values.items():
        if k not in FIELDS or v is None or (v == "" and k != "CLAUDE_CLI"):
            continue          # champ laissé vide = on garde l'ancienne valeur
        if _coerce(k, v) in (None, ""):
            continue
        db.run("INSERT OR REPLACE INTO settings(k,v,updated_at) VALUES(?,?,?)",
               (k, str(v).strip(), now))
    apply(values)

def clear(key):
    """Retire un réglage : .env (ou le défaut de config.py) reprend la main."""
    db.run("DELETE FROM settings WHERE k=?", (key,))

def masked(v):
    """sk-or-v1-abc...4f21 — assez pour reconnaître la clé, pas pour s'en servir."""
    v = v or ""
    return f"{v[:8]}…{v[-4:]}" if len(v) > 14 else ("définie" if v else "")

def current():
    """Ce que l'app utilise vraiment en ce moment, clé masquée."""
    return {"AI_PROVIDER": config.AI_PROVIDER,
            "AI_BASE_URL": config.AI_BASE_URL,
            "AI_MODEL": config.AI_MODEL,
            "SMART_MODEL": config.SMART_MODEL,
            "SMART_BUDGET_USD": config.SMART_BUDGET_USD,
            "AI_API_KEY": masked(config.AI_API_KEY),
            "ALT_PROVIDER": config.ALT_PROVIDER,
            "ALT_BASE_URL": config.ALT_BASE_URL,
            "ALT_MODEL": config.ALT_MODEL,
            "ALT_API_KEY": masked(config.ALT_API_KEY),
            "CLAUDE_CLI": config.CLAUDE_CLI,
            "CLAUDE_CLI_MODEL": config.CLAUDE_CLI_MODEL,
            "EBAY_CLIENT_ID": config.EBAY_CLIENT_ID,
            "EBAY_CLIENT_SECRET": masked(config.EBAY_CLIENT_SECRET),
            "EBAY_MARKETPLACE": config.EBAY_MARKETPLACE,
            "from_db": sorted(stored().keys())}

def _reachable(url, timeout=1.0):
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False

def detect():
    """Ce qui est utilisable sur cette machine, sans rien demander à personne.

    Sert à proposer un branchement en un clic plutôt qu'un formulaire vide :
    un Ollama déjà lancé ou un `claude` déjà installé n'ont besoin d'aucune clé.
    """
    import shutil
    found = []
    if shutil.which("claude"):
        found.append({"id": "claude_cli", "label": "Abonnement Claude Code",
                      "note": "Le binaire `claude` est installé. Sert l'entretien de "
                              "l'assistant, sur ton abonnement, sans clé ni budget."})
    if _reachable("http://localhost:11434/api/tags"):
        found.append({"id": "ollama", "label": "Ollama (déjà lancé)",
                      "note": "Un serveur Ollama répond sur cette machine. "
                              "Gratuit, hors ligne, aucune clé."})
    return found

def connect(which, key=""):
    """Branche un service en un geste. Retourne (ok, message)."""
    if which == "claude_cli":
        save({"CLAUDE_CLI": "1"})
        import ai
        if not ai.cli_available():
            return False, "Le binaire `claude` est introuvable."
        txt, why = ai._cli_chat("Réponds en JSON.", 'Renvoie {"ok":true}',
                                config.CLAUDE_CLI_MODEL, timeout=90)
        if not txt:
            return False, (f"Activé, mais la session Claude a expiré. "
                           f"Va dans Connexions et clique « Se connecter » — "
                           f"tout se fait depuis l'app. ({why[:60]})")
        return True, "Abonnement Claude Code branché pour l'entretien de l'assistant."
    if which == "ebay":
        import ebay
        if key:                       # « App ID:Cert ID » collés d'un bloc
            cid, _, sec = key.partition(":")
            save({"EBAY_CLIENT_ID": cid.strip(), "EBAY_CLIENT_SECRET": sec.strip()})
        ebay._token.update(value=None, expires=0)
        return ebay.probe()
    if which not in config.PROVIDERS:
        return False, "Service inconnu."
    base, model = config.PROVIDERS[which]
    info = config.PROVIDER_INFO.get(which, {})
    if not info.get("local") and not key:
        return False, f"Il faut coller une clé {info.get('label', which)}."
    save({"AI_PROVIDER": which, "AI_BASE_URL": base, "AI_MODEL": model,
          "SMART_MODEL": config.PROVIDER_SMART.get(which, model),
          **({"AI_API_KEY": key} if key else {})})
    return probe()

def probe():
    """Un vrai appel minuscule. Retourne (ok, message lisible)."""
    import ai
    ids = ai.list_models()
    if ids is None:
        return False, (f"{config.AI_BASE_URL}/models ne répond pas : clé refusée, "
                       f"ou adresse incorrecte pour « {config.AI_PROVIDER} ».")
    note = ""
    if ids and config.AI_MODEL not in ids:
        note = f" Attention : « {config.AI_MODEL} » n'est pas dans la liste."
    txt = ai.chat("Réponds en JSON.", 'Renvoie exactement {"ok":true}',
                  max_tokens=64, models=[config.AI_MODEL])
    if not txt:
        return False, (f"Clé acceptée ({len(ids)} modèles), mais « {config.AI_MODEL} » "
                       f"n'a pas répondu.{note}")
    return True, f"Tout fonctionne : {len(ids)} modèles, « {config.AI_MODEL} » répond.{note}"

def demo():
    db.init()
    assert masked("sk-or-v1-abcdefghijklmnop").startswith("sk-or-v1")
    assert "abcdefghijklmnop" not in masked("sk-or-v1-abcdefghijklmnop")
    assert masked("") == "" and masked("short") == "définie"
    assert _coerce("AI_BASE_URL", " https://x/v1/ ") == "https://x/v1"
    assert _coerce("ALT_BASE_URL", "https://y/v1/") == "https://y/v1"
    assert _coerce("SMART_BUDGET_USD", "12,5") == 12.5
    assert _coerce("SMART_BUDGET_USD", "abc") is None
    assert _coerce("CLAUDE_CLI", "1") is True and _coerce("CLAUDE_CLI", "0") is False
    save({"CLAUDE_CLI": "1"}); assert config.CLAUDE_CLI is True
    save({"CLAUDE_CLI": "0"}); assert config.CLAUDE_CLI is False, \
        "une case décochée doit pouvoir éteindre l'option"
    db.run("DELETE FROM settings WHERE k='CLAUDE_CLI'")
    before = config.AI_MODEL
    apply({"AI_MODEL": "", "AI_PROVIDER": "openai"})
    assert config.AI_MODEL == before, "un champ vide ne doit rien écraser"
    assert config.AI_FALLBACKS == [before], "hors openrouter, pas de repli étranger"
    apply({"AI_PROVIDER": "openrouter", "AI_MODEL": before})
    assert len(config.AI_FALLBACKS) > 1
    print("settings ok")

if __name__ == "__main__":
    demo()
