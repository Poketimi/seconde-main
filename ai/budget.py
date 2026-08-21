"""Ce que ça coûte : crédit réel du fournisseur, registre, plafond."""
import json, hashlib, re, time, shutil, subprocess
from concurrent.futures import ThreadPoolExecutor
import db, config, net
from .client import chat, tiers, cli_available

_credit_cache = {"at": 0.0, "left": None}

def credit(max_age=300):
    """Crédit réellement restant chez le fournisseur, en $, ou None s'il ne le dit pas.

    Le registre ai_spend ne compte que le modèle d'entretien : il annonçait
    0.36 $ dépensés quand le compte en avait réellement consommé 0.82 $. Pour
    savoir s'il « reste des jetons », il faut demander au fournisseur.
    """
    if time.time() - _credit_cache["at"] < max_age:
        return _credit_cache["left"]
    left = None
    if config.AI_PROVIDER == "openrouter" and config.AI_API_KEY:
        r = net.get(f"{config.AI_BASE_URL}/credits",
                    headers={"Authorization": f"Bearer {config.AI_API_KEY}"},
                    throttle=False)
        try:
            d = r.json()["data"]
            left = float(d["total_credits"]) - float(d["total_usage"])
        except Exception:
            left = None
    _credit_cache.update(at=time.time(), left=left)
    return left

def spend_since(days=None):
    """What the interview model has cost over the window, in USD."""
    days = days or config.SMART_BUDGET_DAYS
    r = db.q("SELECT COALESCE(SUM(cost_usd),0) c FROM ai_spend WHERE ts > ?",
             (time.time() - days * 86400,), one=True)
    return float(r["c"] if r else 0.0)

def budget_left():
    return max(0.0, config.SMART_BUDGET_USD - spend_since())

def record_spend(model, usage, purpose):
    """Log a call. Uses the provider's own cost when it reports one.

    OpenRouter returns usage.cost when asked ("usage": {"include": true}); if
    a provider omits it we fall back to the price table, so the cap is never
    enforced on nothing.
    """
    tin = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    tout = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    cost = usage.get("cost")
    if cost is None:
        pin, pout = config.SMART_PRICES.get(model, config.SMART_PRICE_DEFAULT)
        cost = (tin * pin + tout * pout) / 1e6
    db.run("""INSERT INTO ai_spend(ts,model,purpose,tokens_in,tokens_out,cost_usd)
              VALUES(?,?,?,?,?,?)""",
           (time.time(), model, purpose, tin, tout, float(cost)))
    return float(cost)

def smart_chat(system, user, purpose, max_tokens=6000):
    """The capable model, with the yearly cap enforced.

    Over budget it degrades to the cheap model rather than refusing: a duller
    interview beats a broken one.
    """
    # deux raisons de rétrograder : le plafond que tu t'es fixé, et le compte
    # réellement vide. La seconde n'était pas vérifiée : le registre ne voit que
    # ses propres appels, pas ce que le modèle bon marché a consommé à côté.
    left = credit()
    dry = left is not None and left <= 0.05
    over = budget_left() <= 0 or dry
    chain = list(config.AI_FALLBACKS) if over else [config.SMART_MODEL, *config.AI_FALLBACKS]
    if dry:
        print(f"  [ia] crédit {config.AI_PROVIDER} épuisé — repli sur {config.AI_MODEL}")
    elif over:
        print(f"  [ia] plafond {config.SMART_BUDGET_USD}$ atteint — repli sur {config.AI_MODEL}")
    return chat(system, user, models=chain, max_tokens=max_tokens, cli=True,
                job="entretien",
                on_usage=lambda m, u: record_spend(m, u, purpose)), over

