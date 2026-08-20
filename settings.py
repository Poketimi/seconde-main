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
          "ALT_PROVIDER", "ALT_BASE_URL", "ALT_MODEL", "ALT_API_KEY")
SECRET = ("AI_API_KEY", "ALT_API_KEY")

def stored():
    return {r["k"]: r["v"] for r in db.q("SELECT k,v FROM settings")}

def _coerce(k, v):
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
        if k not in FIELDS or v is None or v == "":
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
        if k not in FIELDS or v is None or v == "":
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
            "from_db": sorted(stored().keys())}

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
    before = config.AI_MODEL
    apply({"AI_MODEL": "", "AI_PROVIDER": "openai"})
    assert config.AI_MODEL == before, "un champ vide ne doit rien écraser"
    assert config.AI_FALLBACKS == [before], "hors openrouter, pas de repli étranger"
    apply({"AI_PROVIDER": "openrouter", "AI_MODEL": before})
    assert len(config.AI_FALLBACKS) > 1
    print("settings ok")

if __name__ == "__main__":
    demo()
