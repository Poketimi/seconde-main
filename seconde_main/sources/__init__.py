"""Adaptateurs de sites — un fichier par source.

    registry.py   le registre, `search()`, les statuts. Ne connaît aucun site.
    util.py       extraction partagée (JSON embarqué, galeries, nombres)
    smg.py        anibis + tutti
    ricardo.py    ricardo, par son sitemap
    leboncoin.py  leboncoin, par le client d'API `lbc`
    facebook.py   facebook marketplace, via le navigateur connecté
    mail.py       sites refusant le crawl, lus dans leurs alertes e-mail

Ajouter un site : voir SOURCES.md. Ce module ré-exporte toute la surface
publique, donc `import sources` continue de marcher tel quel.
"""
from .registry import (ADAPTERS, LAST_STATUS, LAST_STRATEGY, NEEDS_BROWSER,
                       adapter, search, try_strategies, probe_market, verify_markets)
from .util import (_next_data, _flight_blob, json_objects_with, find_lists,
                   jsonld_listings, hires, gallery, fb_item_details,
                   _thumb, _num, _ts, _THOUSANDS,
                   _gallery_fb, _gallery_smg, _gallery_ricardo)
from .smg import anibis, tutti, _smg_search
from .ricardo import ricardo
# `from . import` et non `from .leboncoin import leboncoin` : la seconde
# forme masque le module derrière la fonction, et `sources.leboncoin`
# ne désigne plus le fichier. L'import suffit à inscrire l'adaptateur.
from . import leboncoin
from .facebook import fb_marketplace, generic, BROWSER_SEARCH
from .mail import DENIED_BY_OPERATOR

from seconde_main import mailbox      # pour demo() ci-dessous

def demo():
    """Parser check that does not depend on the network."""
    html = ('<script id="__NEXT_DATA__" type="application/json">'
            '{"props":{"pageProps":{"searchData":{"ads":[{"list_id":1,"subject":"iPhone 13",'
            '"url":"https://x/ad/1","price":[300],"location":{"zipcode":"1000","city":"Lausanne",'
            '"lat":46.5,"lng":6.6},"owner":{"type":"private","name":"bob"},"images":{}}]}}}}'
            '</script>')
    nd = _next_data(html)
    ads = find_lists(nd, ["ads"])
    assert ads and ads[0]["list_id"] == 1, "next_data walk broken"

    blob = 'x{"id":42,"title":"iPhone 13 Pro","buyNowPrice":450,"sellerNickname":"al"}y'
    objs = list(json_objects_with(blob, "buyNowPrice"))
    assert objs and objs[0]["id"] == 42, f"flight object scan broken: {objs}"

    assert _num("CHF 1'250.00") == 1250.0, _num("CHF 1'250.00")
    assert _num("450") == 450.0
    assert _ts("2026-06-28 17:16:45") is not None
    ld = jsonld_listings('<script type="application/ld+json">{"@type":"Product","name":"X",'
                         '"url":"/p/1","offers":{"price":"99","priceCurrency":"CHF"}}</script>',
                         "https://s.ch/")
    assert ld and ld[0]["price"] == 99.0 and ld[0]["url"] == "https://s.ch/p/1", ld
    assert set(NEEDS_BROWSER) <= set(ADAPTERS), "browser adapters not registered"
    # Un site qui refuse le crawl peut avoir un adaptateur, à condition qu'il
    # n'aille jamais chez lui : leboncoin & co passent par les alertes e-mail
    # que le site nous envoie. L'invariant n'est plus « pas d'adaptateur »,
    # c'est « aucun adaptateur qui crawle ».
    for _s in set(DENIED_BY_OPERATOR) & set(ADAPTERS):
        assert _s in mailbox.SITES, f"{_s} refuse le crawl et a pourtant un adaptateur web"
    print("sources ok: crawler =", ["anibis", "tutti", "ricardo(sitemap)"],
          "| session utilisateur =", sorted(NEEDS_BROWSER),
          "| par alerte e-mail =", sorted(mailbox.SITES),
          "| jamais crawlés =", sorted(DENIED_BY_OPERATOR))

if __name__ == "__main__":
    demo()
