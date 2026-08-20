"""Config + the tuning knobs. Everything you'd want to fiddle with lives here."""
import os
from pathlib import Path

ROOT = Path(__file__).parent
DB_PATH = ROOT / "data" / "market.db"

# --- .env loading (5 lines beats a python-dotenv dependency) ---
_env = ROOT / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

# --- AI provider -------------------------------------------------------
# Any OpenAI-compatible endpoint. Pick one with AI_PROVIDER in .env, or set
# AI_BASE_URL / AI_MODEL by hand to use something not listed here.
# The three "free" ones need a free account and no credit card.
PROVIDERS = {
    # OpenRouter's free roster changes often. `python3 ai.py test` lists the
    # ones your key can actually reach if this default has been retired.
    # Paid by default now that credit is on the account: the :free models are
    # rate-limited constantly (429), and the retries made scans slower than the
    # model ever was. deepseek-v4-flash is ~1.3s/call and costs cents a month.
    "openrouter": ("https://openrouter.ai/api/v1", "deepseek/deepseek-v4-flash"),
    "gemini":     ("https://generativelanguage.googleapis.com/v1beta/openai/",
                   "gemini-2.0-flash"),
    "groq":       ("https://api.groq.com/openai/v1",
                   "llama-3.3-70b-versatile"),
    "deepseek":   ("https://api.deepseek.com/v1", "deepseek-chat"),   # payant
    "ollama":     ("http://localhost:11434/v1", "qwen2.5:7b"),        # local, sans clé
    # Your own Claude / OpenAI account, billed by them, no middleman. Both
    # expose an OpenAI-compatible /chat/completions, which is all this app uses.
    "anthropic":  ("https://api.anthropic.com/v1", "claude-haiku-4-5-20251001"),
    "openai":     ("https://api.openai.com/v1", "gpt-5-mini"),
}

# The capable model to suggest per provider, and the extra models worth falling
# back to. Only OpenRouter hosts other vendors' models, so only it gets a chain.
PROVIDER_SMART = {
    "openrouter": "anthropic/claude-sonnet-5",
    "anthropic":  "claude-sonnet-5",
    "openai":     "gpt-5-mini",
    "deepseek":   "deepseek-chat",
    "gemini":     "gemini-2.0-flash",
    "groq":       "llama-3.3-70b-versatile",
    "ollama":     "qwen2.5:7b",
}
OPENROUTER_FALLBACKS = [
    "deepseek/deepseek-v3.2",                    # paid, cheap
    "nvidia/nemotron-3-super-120b-a12b:free",    # free tier, if credit runs out
    "google/gemma-4-26b-a4b-it:free",
]

# Ce que la page de réglages affiche pour chaque service : où prendre la clé,
# et ce que ça coûte. `local` = tourne sur ta machine, aucune clé, aucun compte.
PROVIDER_INFO = {
    "ollama":     {"label": "Ollama (local)", "local": True, "key_url": "https://ollama.com/download",
                   "note": "Gratuit et hors ligne. Plus lent et moins fin, mais aucun compte."},
    "openrouter": {"label": "OpenRouter", "key_url": "https://openrouter.ai/keys",
                   "note": "Un compte, tous les modèles. Des modèles :free sans carte."},
    "gemini":     {"label": "Google AI Studio", "key_url": "https://aistudio.google.com/apikey",
                   "note": "Palier gratuit large, sans carte bancaire."},
    "groq":       {"label": "Groq", "key_url": "https://console.groq.com/keys",
                   "note": "Gratuit et très rapide, modèles ouverts uniquement."},
    "anthropic":  {"label": "Anthropic", "key_url": "https://console.anthropic.com/settings/keys",
                   "note": "Ton compte Claude *API* — distinct d'un abonnement Claude."},
    "openai":     {"label": "OpenAI", "key_url": "https://platform.openai.com/api-keys",
                   "note": "Ton compte OpenAI *API* — distinct d'un abonnement ChatGPT."},
    "deepseek":   {"label": "DeepSeek", "key_url": "https://platform.deepseek.com/api_keys",
                   "note": "Payant, mais parmi les moins chers du marché."},
}

AI_PROVIDER = os.environ.get("AI_PROVIDER", "openrouter").strip().lower()
_base, _model = PROVIDERS.get(AI_PROVIDER, PROVIDERS["openrouter"])

# AI_API_KEY is the general name; DEEPSEEK_API_KEY still works.
AI_API_KEY  = (os.environ.get("AI_API_KEY")
               or os.environ.get("DEEPSEEK_API_KEY", "")).strip()
AI_BASE_URL = os.environ.get("AI_BASE_URL", _base).rstrip("/")
AI_MODEL    = os.environ.get("AI_MODEL", _model)

# Free models get rate-limited upstream constantly (429). Try these in order
# before giving up; the first that answers wins. `python3 ai.py test` shows
# which ones your key can reach right now.
def fallback_chain(model=None, provider=None):
    """Model first, then the spares -- but only ones the provider actually serves."""
    model = model or AI_MODEL
    provider = provider or AI_PROVIDER
    extra = OPENROUTER_FALLBACKS if provider == "openrouter" else []
    return list(dict.fromkeys([model, *extra]))

AI_FALLBACKS = fallback_chain()

# --- compte de repli ---------------------------------------------------
# Deux comptes, essayés dans l'ordre. Le principal (AI_* ci-dessus) peut être
# ton compte Anthropic ou OpenAI ; quand il n'a plus de jetons, l'app bascule
# toute seule sur celui-ci et continue au lieu de s'arrêter.
# Aucun fournisseur sauf OpenRouter ne publie son solde : la bascule se fait
# donc sur l'échec réel d'un appel (401/402/429), pas sur une estimation.
ALT_PROVIDER = os.environ.get("ALT_PROVIDER", "").strip().lower()
_abase, _amodel = PROVIDERS.get(ALT_PROVIDER, ("", ""))
ALT_API_KEY  = os.environ.get("ALT_API_KEY", "").strip()
ALT_BASE_URL = os.environ.get("ALT_BASE_URL", _abase).rstrip("/")
ALT_MODEL    = os.environ.get("ALT_MODEL", _amodel)

# --- eBay -------------------------------------------------------------
# Pas du crawl : une API officielle, gratuite, 5000 requêtes/jour. Les
# identifiants se créent sur https://developer.ebay.com/my/keys (compte
# développeur gratuit) puis se collent dans /reglages.
# EBAY_CH est le marché suisse ; DELIVERY_CH ne garde que ce qui est livrable
# en Suisse, ce qui rend EBAY_DE et EBAY_FR utiles aussi.
EBAY_CLIENT_ID     = os.environ.get("EBAY_CLIENT_ID", "").strip()
EBAY_CLIENT_SECRET = os.environ.get("EBAY_CLIENT_SECRET", "").strip()
EBAY_MARKETPLACE   = os.environ.get("EBAY_MARKETPLACE", "EBAY_CH").strip()
EBAY_DELIVERY_CH   = os.environ.get("EBAY_DELIVERY_CH", "1") != "0"

# --- abonnement Claude Code -------------------------------------------
# Lance le binaire `claude -p` déjà installé sur la machine : les appels
# passent par ton abonnement, pas par une clé d'API, et ne coûtent rien au
# budget. Réservé à l'entretien de l'assistant (2 appels par recherche) --
# le tri et la traduction en font des centaines par jour et taperaient dans
# les limites de débit de l'abonnement en quelques minutes.
CLAUDE_CLI = os.environ.get("CLAUDE_CLI", "0") == "1"
CLAUDE_CLI_MODEL = os.environ.get("CLAUDE_CLI_MODEL", "claude-sonnet-5")
GEOCODER_EMAIL = os.environ.get("GEOCODER_EMAIL", "anonymous@example.com")

POLL_SECONDS = int(os.environ.get("POLL_SECONDS", 1800))  # 30 min
# Politeness. We were making 2574 requests/day -- one every 34s, round the
# clock, from a single IP -- and ricardo, anibis, leboncoin and tutti all began
# refusing. No fingerprint survives that pattern; the rate is the tell.
PER_SITE_DELAY = 8.0        # seconds between two requests to the same host

# At most this many models go to any one source per cycle; the rest rotate in
# next time. Full coverage takes a few cycles instead of one burst.
TARGETS_PER_CYCLE = int(os.environ.get("TARGETS_PER_CYCLE", 3))

# After a source starts refusing, back off exponentially instead of hammering
# it: 10min, 20min, 40min... capped. A single success clears it.
BACKOFF_BASE = 600
BACKOFF_MAX = 6 * 3600
HTTP_TIMEOUT = 25

# --- travel-time model -------------------------------------------------
# ponytail: crow-fly * detour / speed is the cheap prefilter; OSRM refines
# the survivors. Tune these if the estimates read wrong for your region.
MODE_SPEED_KMH = {"foot": 4.5, "bike": 15.0, "car": 55.0, "transit": 28.0}
DETOUR_FACTOR  = {"foot": 1.25, "bike": 1.3, "car": 1.35, "transit": 1.45}
OSRM_URL = "https://router.project-osrm.org"
OSRM_PROFILE = {"foot": "foot", "bike": "bike", "car": "driving"}  # no transit
# refine with OSRM when the estimate is within this factor of the limit
REFINE_BAND = 1.6

# Browser tier: headless is faster, but some sites re-trigger their security
# check on headless and stay quiet on a visible window. Flip if a site that
# worked during `login` keeps coming back BLOCKED.
# Headless by default: a visible Chrome window grabs macOS focus on every
# scan. Facebook returns identical results headless once you are logged in,
# so only the login flow forces a window.
BROWSER_HEADLESS = os.environ.get("BROWSER_HEADLESS", "1") != "0"

# Nothing is ever deleted. A listing not seen for this long is marked gone --
# "gone", not "sold": a seller may simply have withdrawn it, and we cannot tell.
# Auctions are different: a past end date IS a real ending.
# Browser-backed sources (facebook & co) cost ~6s and a real page view each.
# Querying every model on every cycle would mean hundreds of automated views an
# hour -- the surest way to get the account restricted. Only this many models
# are queried per cycle on those sources; the rest rotate in next time, so
# coverage is complete over a few cycles instead of all at once.
BROWSER_TARGETS_PER_CYCLE = int(os.environ.get("BROWSER_TARGETS_PER_CYCLE", 2))

GONE_AFTER_HOURS = float(os.environ.get("GONE_AFTER_HOURS", 48))

# --- assisted search ---------------------------------------------------
# A genuinely capable model, used ONLY to run the interview: twice per new
# search (generate questions, turn answers into criteria). It never sees a
# listing -- per-listing work stays on AI_MODEL above.
# Measured on OpenRouter: $2/M in, $10/M out => ~$0.011 per interview,
# about 900 interviews for $10.
SMART_MODEL       = os.environ.get("SMART_MODEL", "anthropic/claude-sonnet-5")
SMART_BUDGET_USD  = float(os.environ.get("SMART_BUDGET_USD", 10.0))
SMART_BUDGET_DAYS = 365
SMART_PRICES = {                      # $/M tokens (in, out), for the fallback estimate
    "anthropic/claude-sonnet-5": (2.00, 10.00),
    "anthropic/claude-haiku-4.5": (1.00, 5.00),
    "openai/gpt-5-mini": (0.25, 2.00),
    "deepseek/deepseek-v4-pro": (0.66, 1.98),
    # same models billed through your own account (no "vendor/" prefix)
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-5": (10.00, 50.00),
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "gpt-5-mini": (0.25, 2.00),
    "deepseek-chat": (0.28, 0.42),
}
# A model absent from the table is estimated at this rate, so an unknown id
# still counts against the cap instead of billing invisibly.
SMART_PRICE_DEFAULT = (2.00, 10.00)

# AI spend guards
AI_MAX_CALLS_PER_CYCLE = 40
AI_ENRICH_BUDGET = int(os.environ.get("AI_ENRICH_BUDGET", 200))  # per day
# Every listing is translated into these (i18n.AUTO) as it arrives; one call
# covers 5 listings x both languages, so this is ~12 calls per cycle.
AI_TRANSLATE_BUDGET = int(os.environ.get("AI_TRANSLATE_BUDGET", 60))
