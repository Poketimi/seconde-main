"""Outbound HTTP — deprecated in favour of crawler.py.

This module used curl_cffi to rotate TLS fingerprints across chrome/safari/
firefox profiles so that sites could not tell us from a browser. That is
fingerprint impersonation, and it is exactly what this project no longer does:
the crawler identifies itself honestly and accepts the answer it gets.

Kept only for requests to services that are meant to be called programmatically
and are not marketplace crawling: the OpenRouter API, OSRM routing, Nominatim
geocoding, GeoNames downloads. Those go out with the same honest User-Agent.
"""
import time
import urllib.request, urllib.error
from urllib.parse import urlencode, urlparse
import json as _json

import config

LAST_BLOCKED = {"url": None, "blocked": False}
CHALLENGE_HINTS = ("datadome", "geo.captcha-delivery.com", "please enable js",
                   "captcha", "un instant", "just a moment", "checking your browser",
                   "vérification rapide de sécurité", "sécurité de votre connexion",
                   "challenges.cloudflare.com", "access denied", "unusual traffic")

def _ua():
    import crawler
    return crawler.USER_AGENT

def looks_challenged(text):
    low = (text or "")[:200000].lower()
    return any(h in low for h in CHALLENGE_HINTS)

_last_hit = {}

class _Resp:
    """Minimal response object, so existing callers keep working."""
    def __init__(self, status, text, headers=None, content=b""):
        self.status_code = status
        self.text = text
        self.content = content
        self.headers = headers or {}
    def json(self):
        return _json.loads(self.text)

def get(url, params=None, headers=None, timeout=None, throttle=True, **_ignored):
    """Plain, identified GET. No impersonation, no profile rotation."""
    if params:
        url = f"{url}{'&' if '?' in url else '?'}{urlencode(params)}"
    host = urlparse(url).netloc
    if throttle:
        wait = config.PER_SITE_DELAY - (time.time() - _last_hit.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        _last_hit[host] = time.time()
    h = {"User-Agent": _ua(), "Accept": "*/*"}
    h.update(headers or {})
    LAST_BLOCKED.update(url=url, blocked=False)
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout or config.HTTP_TIMEOUT) as r:
            raw = r.read()
            text = raw.decode("utf-8", "replace")
            return _Resp(r.status, text, dict(r.headers), raw)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        if looks_challenged(body) or e.code in (401, 403, 429):
            LAST_BLOCKED.update(url=url, blocked=True)
        return None
    except Exception:
        return None

def post_form(url, fields, headers=None, timeout=None):
    """POST urlencodé — ce qu'OAuth2 client_credentials attend, pas du JSON."""
    h = {"User-Agent": _ua(),
         "Content-Type": "application/x-www-form-urlencoded"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=urlencode(fields).encode(),
                                 headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout or 30) as r:
            return _Resp(r.status, r.read().decode("utf-8", "replace"), dict(r.headers))
    except urllib.error.HTTPError as e:
        try:
            return _Resp(e.code, e.read().decode("utf-8", "replace"))
        except Exception:
            return _Resp(e.code, "")
    except Exception:
        return None

def post_json(url, payload, headers=None, timeout=None):
    h = {"User-Agent": _ua(), "Content-Type": "application/json"}
    h.update(headers or {})
    data = _json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout or 120) as r:
            return _Resp(r.status, r.read().decode("utf-8", "replace"), dict(r.headers))
    except urllib.error.HTTPError as e:
        try:
            return _Resp(e.code, e.read().decode("utf-8", "replace"))
        except Exception:
            return _Resp(e.code, "")
    except Exception:
        return None
