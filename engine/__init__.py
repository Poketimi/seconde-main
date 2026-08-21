"""The background loop: scrape -> store -> filter -> match -> notify.

Filter order is deliberate and is the whole cost story: rules and distance are
free and reject most listings, so the AI batch only ever sees survivors.

Découpé par métier :

    match.py    correspondance : mots-clés, modèle ciblé, règles, distance
    store.py    écriture en base : annonce, vendeur, coordonnées, cycle de vie
    health.py   santé des sources, retraits, reconnexion, notifications
    scan.py     la boucle complète
    checks.py   auto-vérification

Tout est ré-exporté ici. Pour remplacer une fonction dans un test, viser son
module (`engine.scan.judge = ...`) : réassigner `engine.judge` ne change que
le nom ré-exporté, pas celui que les appelants résolvent.
"""
from .match import *      # noqa: F401,F403
from .store import *      # noqa: F401,F403
from .health import *     # noqa: F401,F403
from .scan import *       # noqa: F401,F403
from .checks import *     # noqa: F401,F403
from .match import _norm, _number_belongs_to           # noqa: F401
from .health import forget_stale_verdicts, BACKOFF_STATUSES           # noqa: F401
from .health import (_active, _active_lock, _NOTIFIER, _last_recovery,   # noqa: F401
                     _as_str)                                            # noqa: F401
from . import match, store, health, scan, checks       # noqa: F401
