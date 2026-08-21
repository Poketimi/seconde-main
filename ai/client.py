"""Parler au modèle : comptes, routage par travail, appel, cache."""
import json, hashlib, re, time, shutil, subprocess
from concurrent.futures import ThreadPoolExecutor
import db, config, net

_cooldown_until = [0.0]

COOLDOWN = 900          # 15 min

def cooling_down():
    return time.time() < _cooldown_until[0]

def available():
    """A local endpoint (ollama, llama.cpp, LM Studio) needs no key at all.

    True as long as ONE of the two accounts can still be tried: the primary
    running dry must not read as "no AI".
    """
    if cooling_down():
        return False
    return bool(tiers())

def _is_local(url):
    return any(h in (url or "") for h in ("localhost", "127.0.0.1", "0.0.0.0", "::1"))

def _slug(*parts):
    s = " ".join(str(p) for p in parts if p)
    s = re.sub(r"[^\w\s-]", "", s.lower())
    return re.sub(r"[\s_-]+", "-", s).strip("-")[:120]

CLI_TIMEOUT = 180

_which_cache = []

def cli_available():
    """shutil.which parcourt le PATH ; tiers() l'appelle à chaque requête."""
    if not _which_cache:
        _which_cache.append(bool(shutil.which("claude")))
    return _which_cache[0]

CLI_LOGIN = {"running": False, "message": "", "started": 0.0}

# `claude auth status` lance un processus : ~350 ms, et /reglages l'appelait
# deux fois par rendu — d'où une page à 1,3 s pour de l'information qui ne
# change qu'à la connexion.
_auth_cache = {"at": 0.0, "val": None}
AUTH_TTL = 60

def cli_auth(max_age=AUTH_TTL):
    """État de la session `claude` : {loggedIn, authMethod, ...} ou None.

    C'est `claude auth status`, qui répond déjà en JSON. Rien n'est lu dans le
    trousseau : on demande au binaire, il répond ce qu'il veut bien dire.
    Le résultat est gardé une minute ; `cli_login` l'invalide pour que la page
    Connexions bascule dès que la connexion aboutit.
    """
    if max_age and time.time() - _auth_cache["at"] < max_age:
        return _auth_cache["val"]
    exe = shutil.which("claude")
    if not exe:
        _auth_cache.update(at=time.time(), val=None)
        return None
    try:
        r = subprocess.run([exe, "auth", "status"], capture_output=True,
                           text=True, timeout=20)
        val = json.loads(r.stdout)
    except Exception:
        val = None
    _auth_cache.update(at=time.time(), val=val)
    return val

def cli_login():
    """Ouvre une fenêtre Terminal sur `claude auth login`.

    La connexion se fait dans TA fenêtre et TON navigateur : l'app ne voit ni
    ne manipule aucun identifiant, elle se contente d'ouvrir la porte. Même
    principe que le bouton de connexion Facebook.

    Passer par un petit script évite d'échapper quoi que ce soit vers
    AppleScript, et donc toute la classe de bugs de guillemets qui va avec.
    """
    exe = shutil.which("claude")
    if not exe:
        CLI_LOGIN.update(running=False, message="binaire `claude` introuvable")
        return False
    path = config.ROOT / "data" / "claude-login.command"
    path.write_text("#!/bin/sh\n"
                    "echo 'Connexion à ton compte Claude — suis les instructions,'\n"
                    "echo 'puis reviens sur Seconde Main : la page se mettra à jour seule.'\n"
                    "echo\n"
                    f"exec {exe} auth login --claudeai\n")
    path.chmod(0o755)
    try:
        subprocess.Popen(["open", "-a", "Terminal", str(path)])
    except Exception as e:
        CLI_LOGIN.update(running=False, message=f"impossible d'ouvrir Terminal : {e}")
        return False
    _tier_down.pop("abonnement", None)      # on retente dès que la session revient
    _auth_cache.update(at=0.0, val=None)    # la page doit voir la nouvelle session
    CLI_LOGIN.update(running=True, started=time.time(),
                     message="fenêtre Terminal ouverte — connecte-toi puis reviens ici")
    return True

def cli_login_done():
    """Appelé par le sondage : la fenêtre a-t-elle abouti ?"""
    if not CLI_LOGIN["running"]:
        return False
    st = cli_auth(max_age=0) or {}
    if st.get("loggedIn"):
        CLI_LOGIN.update(running=False, message="connecté")
        return True
    if time.time() - CLI_LOGIN["started"] > 600:
        CLI_LOGIN.update(running=False, message="abandon après 10 min")
    return False

def _cli_chat(system, user, model, timeout=CLI_TIMEOUT):
    """Un appel via `claude -p`. Retourne (texte, usage) ou (None, raison).

    Le texte des annonces arrive par stdin, jamais dans la ligne de commande,
    et tous les outils sont coupés : ce processus ne peut ni lire ni écrire de
    fichier, quoi que contienne l'annonce.
    """
    exe = shutil.which("claude")
    if not exe:
        return None, "binaire `claude` introuvable"
    cmd = [exe, "-p", "--allowedTools", "", "--output-format", "json"]
    if model:
        cmd += ["--model", model]
    try:
        r = subprocess.run(cmd, input=f"{system}\n\n---\n\n{user}",
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"pas de réponse en {timeout}s"
    except Exception as e:
        return None, str(e)
    try:
        out = json.loads(r.stdout)
    except Exception:
        return None, (r.stderr or r.stdout or "sortie illisible")[:200]
    if out.get("is_error") or out.get("subtype") != "success":
        return None, str(out.get("result") or out.get("subtype"))[:200]
    return out.get("result"), (out.get("usage") or {})

_tier_down = {}

TIER_COOLDOWN = 900         # 15 min

DRY = (401, 402, 403, 429)  # clé refusée, plus de crédit, quota épuisé

def route(job):
    """(compte, modèle) réglés pour ce travail. Vides = comportement par défaut."""
    r = (getattr(config, "JOB_ROUTES", None) or {}).get(job) or {}
    acct = r.get("account") or ""
    if acct == "abonnement" and job in config.JOBS_NO_SUBSCRIPTION:
        acct = ""            # garde-fou : jamais le travail en masse sur l'abonnement
    return acct, (r.get("model") or "")

def job_table():
    """Ce que chaque travail utilise réellement — compte, modèle, et qui paie.

    Résolu, pas déclaré : si le compte choisi est indisponible, la ligne dit ce
    qui servira à la place plutôt que ce qui a été coché.
    """
    rows = []
    for job, meta in config.JOBS.items():
        acct, model = route(job)
        live = tiers(cli_ok=(acct == "abonnement"), prefer=acct)
        used = live[0] if live else None
        sub = bool(used and used["provider"] == "claude_cli")
        rows.append({
            "job": job, "label": meta["label"], "hint": meta["hint"],
            "account": acct, "model": model,
            "sub_allowed": job not in config.JOBS_NO_SUBSCRIPTION,
            "resolved_account": used["name"] if used else None,
            "resolved_model": (model or (used["model"] if used else None)),
            "paid_by": ("abonnement" if sub else
                        (f"clé {used['provider']}" if used else "aucun compte")),
        })
    return rows

def tiers(cli_ok=False, prefer=""):
    """Les comptes utilisables, dans l'ordre, celui en panne exclu.

    `cli_ok` ouvre l'abonnement Claude Code, qui passe devant les autres :
    il ne consomme aucun crédit. Il reste fermé par défaut pour que le travail
    en masse ne parte jamais dedans.
    """
    out = []
    if (cli_ok or prefer == "abonnement") and config.CLAUDE_CLI and cli_available() \
            and time.time() >= _tier_down.get("abonnement", 0):
        out.append({"name": "abonnement", "provider": "claude_cli", "base": "cli",
                    "key": "", "model": config.CLAUDE_CLI_MODEL})
    for name, prov, base, key, model in (
            ("principal", config.AI_PROVIDER, config.AI_BASE_URL,
             config.AI_API_KEY, config.AI_MODEL),):
        if not base or not (key or _is_local(base)):
            continue
        if time.time() < _tier_down.get(name, 0):
            continue
        out.append({"name": name, "provider": prov, "base": base,
                    "key": key, "model": model})
    if prefer:                       # le compte choisi passe devant, sans exclure les autres
        out.sort(key=lambda t: 0 if t["name"] == prefer else 1)
    return out

def tier_status():
    """Pour l'interface : quel compte sert, et lequel est en pause."""
    live = {t["name"] for t in tiers(cli_ok=True)}
    rows = [("principal", config.AI_PROVIDER, config.AI_MODEL)]
    if config.CLAUDE_CLI:
        rows.insert(0, ("abonnement", "claude_cli (assistant seulement)",
                        config.CLAUDE_CLI_MODEL))
    return [{"name": n, "provider": p, "model": m, "live": n in live,
             "retry_in": max(0, int(_tier_down.get(n, 0) - time.time()))}
            for n, p, m in rows if p]

def chat(system, user, temperature=0.0, max_tokens=8000, models=None, on_usage=None,
         cli=False, job=None):
    """Cached chat completion. Returns text, or None if unavailable/failed.

    `models` overrides the fallback chain (used by smart_chat for the
    interview model). It is part of the cache key, so the cheap and expensive
    models never read each other's answers.

    Two accounts are tried in order. When the first has no tokens left it is
    parked for a quarter of an hour and the second takes over, so a dry
    account degrades the app instead of stopping it.
    """
    acct, want_model = route(job) if job else ("", "")
    chain = list(models) if models else list(config.AI_FALLBACKS)
    if want_model:
        chain = [want_model] + [m for m in chain if m != want_model]
    key = hashlib.sha256(
        f"{chain[0]}|{system}|{user}|{temperature}".encode()).hexdigest()
    row = db.cache_get("ai_cache", key)
    if row:
        return row["v"]
    # La disponibilité dépend du travail : `available()` ne regarde ni le
    # routage ni l'abonnement, donc un travail dirigé vers `abonnement`
    # échouait silencieusement dès qu'aucune clé d'API n'était configurée.
    usable = tiers(cli_ok=cli, prefer=acct)
    if cooling_down() or not usable:
        return None
    seen, r, used = [], None, None
    for tier in usable:
        if tier["provider"] == "claude_cli":
            txt, info = _cli_chat(system, user, want_model or tier["model"])
            if txt:
                if on_usage:
                    try:
                        # Rien n'est facturé : c'est l'abonnement. Les jetons sont
                        # réels, le coût est 0, et la valeur que Claude Code
                        # annonce (ce que ça aurait coûté à l'API) est notée pour
                        # que l'écart soit visible plutôt que supposé.
                        u = info if isinstance(info, dict) else {}
                        on_usage(f"claude-code/{want_model or tier['model']}",
                                 {"prompt_tokens": u.get("input_tokens") or 0,
                                  "completion_tokens": u.get("output_tokens") or 0,
                                  "cost": 0.0})
                    except Exception:
                        pass
                db.run("INSERT OR REPLACE INTO ai_cache(k,v,created_at) VALUES(?,?,?)",
                       (key, txt, time.time()))
                return txt
            seen.append(f"abonnement={info}")
            # session expirée ou limite atteinte : inutile de réessayer à chaque appel
            _tier_down["abonnement"] = time.time() + TIER_COOLDOWN
            print(f"  [ia] abonnement Claude Code indisponible ({info}) — "
                  f"repli sur la clé d'API")
            continue
        want = dict.fromkeys([want_model] if want_model else chain)
        headers = {"Content-Type": "application/json",
                   **({"Authorization": f"Bearer {tier['key']}"} if tier["key"] else {})}
        url = f"{tier['base']}/chat/completions"
        dry = []
        for model in want:
            body = {"model": model,
                    "messages": [{"role": "system", "content": system},
                                 {"role": "user", "content": user}],
                    "temperature": temperature, "max_tokens": max_tokens,
                    "response_format": {"type": "json_object"}}
            # asking for the real cost is an OpenRouter extension; other providers
            # reject unknown top-level fields outright
            if tier["provider"] == "openrouter":
                body["usage"] = {"include": True}
            r = net.post_json(url, body, headers=headers)
            if r is not None and r.status_code == 400:
                # Not every model supports JSON mode, and not every endpoint accepts
                # the usage flag; the prompt demands JSON anyway. Drop both and retry.
                body.pop("response_format", None)
                body.pop("usage", None)
                r = net.post_json(url, body, headers=headers)
            if r is not None and r.status_code == 200:
                used = model
                break
            code = getattr(r, "status_code", "x")
            dry.append(code in DRY)
            seen.append(f"{tier['name']}:{model}={code}")
            r = None
        if r is not None:
            break
        if dry and all(dry) and len(tiers(cli_ok=True)) > 1:
            _tier_down[tier["name"]] = time.time() + TIER_COOLDOWN
            print(f"  [ia] compte {tier['name']} ({tier['provider']}) sans jetons — "
                  f"mis de côté {TIER_COOLDOWN // 60}min")
    if r is None:
        if all(v.endswith("429") for v in seen):
            _cooldown_until[0] = time.time() + COOLDOWN
            print(f"  [ai] quota gratuit épuisé ({len(seen)} modèles en 429) — "
                  f"pause {COOLDOWN // 60}min, repli mots-clés en attendant")
        else:
            print(f"  [ai] every model failed: {', '.join(seen)}")
        return None
    try:
        payload = r.json()
        txt = payload["choices"][0]["message"]["content"]
    except Exception:
        return None
    if on_usage:
        try:
            on_usage(used, payload.get("usage") or {})
        except Exception:
            pass
    db.run("INSERT OR REPLACE INTO ai_cache(k,v,created_at) VALUES(?,?,?)",
           (key, txt, time.time()))
    return txt

def looks_truncated(txt):
    """A reply cut off by max_tokens: opens a JSON object and never closes it."""
    if not txt:
        return False
    t = txt.strip().strip("`").replace("json", "", 1).strip()
    return t.startswith("{") and t.count("{") > t.count("}")

def _parse_json(txt):
    if not txt:
        return None
    try:
        return json.loads(txt)
    except Exception:
        m = re.search(r"\{.*\}", txt, re.S)
        try:
            return json.loads(m.group(0)) if m else None
        except Exception:
            return None

def list_models():
    """Model ids this key can reach, or None if the endpoint refused.

    Every OpenAI-compatible provider serves /models, so the settings page can
    offer a real list instead of asking you to type an id from memory.
    """
    r = net.get(f"{config.AI_BASE_URL}/models",
                headers={"Authorization": f"Bearer {config.AI_API_KEY}"}
                if config.AI_API_KEY else None, throttle=False)
    if r is None:
        return None
    try:
        return [m.get("id") for m in r.json().get("data", []) if m.get("id")]
    except Exception:
        return None

