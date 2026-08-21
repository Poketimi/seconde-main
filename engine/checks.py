"""Auto-vérification : `python3 -m engine.checks`."""
import json, re, time, threading, subprocess, shutil, traceback
from concurrent.futures import ThreadPoolExecutor
import db, ai, geo, sources, config, i18n
from .match import passes_rules, keyword_score, distance_ok, matches_target, dup_key
from .store import upsert_listing

def demo():
    db.init()
    s = {"id": -1, "query": "iphone 13 pro", "reference": None, "price_min": 100,
         "price_max": 500, "seller_type": "any", "exclude_kw": "cassé,broken",
         "origins": "[]", "shipping_ok": 1, "sources": "[]", "name": "t"}
    assert passes_rules({"price": 300, "seller_type": "private", "title": "iPhone 13 Pro"}, s)
    assert not passes_rules({"price": 900, "title": "x"}, s), "price ceiling ignored"
    assert not passes_rules({"price": 300, "title": "iPhone cassé"}, s), "exclude kw ignored"
    s2 = dict(s, seller_type="private")
    assert not passes_rules({"price": 300, "seller_type": "pro", "title": "x"}, s2)

    assert keyword_score({"title": "iPhone 13 Pro 256Go"}, s) > 90
    assert keyword_score({"title": "Coque iPhone 13 Pro"}, s) < 40, "accessory not penalised"

    lausanne = geo.by_postcode("1015", "CH")
    orig = [{"label": "EPFL", "lat": lausanne[0], "lon": lausanne[1],
             "max_minutes": 10, "mode": "foot"}]
    s3 = dict(s, origins=json.dumps(orig), shipping_ok=1)
    ok, *_ = distance_ok({"shipping": 0}, s3, geo.by_postcode("1422", "CH"))
    assert not ok, "Grandson must fail a 10min walk from EPFL"
    ok, _, label, _ = distance_ok({"shipping": 1}, s3, None)
    assert ok and label == "livraison", "shipped items must bypass distance"
    print("engine ok")

