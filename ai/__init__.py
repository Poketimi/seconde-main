"""DeepSeek (or any OpenAI-compatible endpoint) for the parts rules can't do.

Where the AI actually earns its tokens:
  - telling a real "iPhone 13 Pro" from "Batterie iPhone 13 Pro" / a case / a
    broken-for-parts unit. Keyword matching cannot.
  - normalising free-text titles into canonical products so the DB accumulates
    and the *next* search is answered from SQLite instead of the network.

Cost control: one batched call per (search, page) instead of one per listing,
every response cached by content hash, and a hard per-cycle call cap.

SECURITY: listing text is untrusted scraped input. It is only ever classified;
model output is parsed as JSON and written to DB columns, never executed, and
never used to build a URL or a shell command.

Découpé par métier :

    client.py     comptes, routage par travail, appel au modèle, cache
    budget.py     crédit réel, registre de dépense, plafond
    classify.py   tri et normalisation des annonces (le travail en masse)
    assistant.py  entretien : questions fermées, critères, modèles concrets
    checks.py     auto-vérifications

Tout est ré-exporté ici, donc `import ai; ai.analyse(...)` marche comme avant.
Attention : réassigner `ai.chat` ne change que ce nom-ci. Pour remplacer une
fonction (tests), viser son module : `ai.client.chat = ...`.
"""
from .client import *          # noqa: F401,F403
from .budget import *          # noqa: F401,F403
from .classify import *        # noqa: F401,F403
from .assistant import *       # noqa: F401,F403
from .checks import *          # noqa: F401,F403
# Les noms privés ne traversent pas `import *` ; ceux-ci sont utilisés par les
# tests et par d'autres modules.
from .client import _cli_chat, _parse_json, _slug, _tier_down, _is_local  # noqa: F401
from .classify import _listing_brief, _analyse_chunk                     # noqa: F401
from .assistant import _valid_questions                                  # noqa: F401
from . import client, budget, classify, assistant, checks                # noqa: F401
