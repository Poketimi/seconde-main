"""Auto-vérifications : `python3 -m ai.checks` et `python3 ai.py test`."""
import json, hashlib, re, time, shutil, subprocess
from concurrent.futures import ThreadPoolExecutor
import db, config, net
from .client import available, chat, list_models, tiers, _parse_json, _slug
from .budget import credit, spend_since
from .classify import (analyse, upsert_product, dedupe_products, norm_category,
                       _listing_brief)
from .assistant import ask_questions, build_criteria

def selftest():
    """python3 ai.py test -- verify the key, then classify real problem listings."""
    print(f"provider : {config.AI_PROVIDER}\nendpoint : {config.AI_BASE_URL}"
          f"\nmodel    : {config.AI_MODEL}\nkey      : "
          f"{'set (' + config.AI_API_KEY[:6] + '...)' if config.AI_API_KEY else 'MISSING'}")
    if not available():
        print("\nNo key. Put one in .env:  AI_PROVIDER=openrouter  AI_API_KEY=sk-or-...")
        return False

    ids = list_models()
    if ids is None:
        print("\n/models unreachable -- key rejected, or wrong AI_BASE_URL.")
        return False
    try:
        print(f"\nkey accepted, {len(ids)} models available")
        if config.AI_MODEL not in ids and ids:
            free = [i for i in ids if ":free" in str(i)][:8]
            print(f"  ! '{config.AI_MODEL}' is not in the list. Try AI_MODEL= one of:")
            for i in (free or ids[:6]):
                print(f"      {i}")
    except Exception:
        print("  (could not list models; continuing)")

    # the exact listings the keyword fallback gets wrong
    # exactly the model asked for -- a Pro Max would be a fair 0, which made
    # this probe ambiguous and the test unreliable
    probes = [{"title": "Apple iPhone 13 Pro 256GB Graphite", "description": "très bon état",
               "price": 499, "currency": "CHF"},
              {"title": "Reparation iPhone 13 Pro Max avec écran INCELL", "description":
               "service de réparation, déplacement possible", "price": 150, "currency": "CHF"},
              {"title": "Coque iPhone 13 Pro Max", "description": "silicone noir",
               "price": 15, "currency": "CHF"}]
    # Two different jobs, so check both:
    #  - cataloguing (no request): an accessory IS a product worth a fiche
    #  - matching (with a request): an accessory must score 0 for that buyer
    print("\ncataloguing (no buyer request)...")
    cat = analyse(probes)
    if not cat:
        print("  no usable response -- see the HTTP error above.")
        return False
    ok = True
    for i, want in [(0, True), (1, False), (2, True)]:   # repair service is not a product
        got = cat.get(i, {})
        is_item = got.get("is_item")
        ok &= (is_item == want)
        print(f"  {'OK ' if is_item == want else 'BAD'} {probes[i]['title'][:36]:38} "
              f"is_item={is_item!s:5} -> {(got.get('product') or {}).get('canonical_name', '-')[:34]}")

    print("\nmatching against 'iPhone 13 Pro, 100-700 CHF'...")
    match = analyse(probes, "iPhone 13 Pro en bon état, 100-700 CHF")
    for i, want_hit in [(0, True), (1, False), (2, False)]:
        sc = float((match.get(i) or {}).get("score") or 0)
        good = (sc >= 50) if want_hit else (sc < 50)
        ok &= good
        print(f"  {'OK ' if good else 'BAD'} {probes[i]['title'][:36]:38} score={sc:5.0f}  "
              f"{(match.get(i) or {}).get('reason','')[:34]}")

    print("\n" + ("AI wired up and classifying correctly." if ok else
                   "Model responded but misclassified; try a stronger AI_MODEL."))
    return ok

def demo():
    assert _slug("Apple", "iPhone 13 Pro", "256GB") == "apple-iphone-13-pro-256gb"
    assert _parse_json('junk {"results":[{"i":0}]} tail')["results"][0]["i"] == 0
    assert analyse([], "x") == {}
    b = _listing_brief({"title": "T" * 400, "description": "D" * 900, "price": 5}, 3)
    assert b["i"] == 3 and len(b["title"]) == 200 and len(b["desc"]) == 500
    db.init()
    pid = upsert_product({"canonical_name": "Apple iPhone 13 Pro 256GB", "brand": "Apple",
                          "model": "iPhone 13 Pro", "variant": "256GB", "category": "phone"})
    assert pid and upsert_product({"canonical_name": "different wording", "brand": "Apple",
                                   "model": "iPhone 13 Pro", "variant": "256GB"}) == pid, \
        "same product must dedupe to one row"
    print("ai ok (key present)" if available() else "ai ok (no key: rules-only fallback)")

