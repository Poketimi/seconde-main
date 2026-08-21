"""Sites qui refusent le crawl mais envoient leurs propres alertes.

Un seul relevé IMAP sert tous ces adaptateurs. Ajouter un site = une entrée
dans `mailbox.SITES`, l'adaptateur est créé à partir d'elle.
"""
import re, time
import mailbox
from .registry import ADAPTERS, LAST_STATUS

# Les sites qui refusent le crawl mais envoient des alertes : on lit la boîte
# mail, jamais leur site. Un seul relevé IMAP sert tous ces adaptateurs.
_MAIL_CACHE = {"at": 0.0, "rows": []}

def _mail_rows():
    if time.time() - _MAIL_CACHE["at"] > 300:
        _MAIL_CACHE.update(at=time.time(), rows=mailbox.fetch())
    return _MAIL_CACHE["rows"]

def _mail_adapter(site):
    def run(query, spec=None):
        if not mailbox.configured():
            LAST_STATUS[site] = ("login", "alertes e-mail non configurées")
            return []
        terms = [t for t in re.sub(r"[^\w\s]", " ", query.lower()).split() if len(t) > 1]
        rows = [r for r in _mail_rows() if r["source"] == site
                and all(t in (r["title"] or "").lower() for t in terms)]
        if not rows:
            LAST_STATUS[site] = ("empty", mailbox.LAST_ERROR[0] or
                                 "aucune alerte ne correspond")
        return rows
    return run

for _site in mailbox.SITES:
    ADAPTERS[_site] = _mail_adapter(_site)


DENIED_BY_OPERATOR = {
    # site -> raison, affichée telle quelle sur la page Crawler
    "leboncoin": ("robots.txt interdit l'accès automatisé en toutes lettres et "
                  "n'autorise que des robots nommés — jamais crawlé ; ses annonces "
                  "arrivent par les alertes e-mail qu'il envoie lui-même"),
}

