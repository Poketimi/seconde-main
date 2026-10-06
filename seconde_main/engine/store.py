"""Écrire en base : insertion, vendeur, coordonnées, cycle de vie."""
import json, re, time, threading, subprocess, shutil, traceback
from concurrent.futures import ThreadPoolExecutor
from seconde_main import db, ai, geo, sources, config, i18n
from .match import dup_key

COLS = ("url source source_id title description price currency price_type category "
        "condition seller_type seller_name location_raw postal_code country lat lon "
        "shipping shipping_cost image images posted_at attrs auction_end bids dup_key raw").split()

def upsert_listing(d):
    """Insert or refresh. Returns (listing_id, is_new)."""
    now = time.time()
    row = db.q("SELECT id, price FROM listings WHERE url=?", (d["url"],), one=True)
    if row:
        # a price change is worth keeping: sellers drop prices over time
        if d.get("price") is not None and row["price"] != d["price"]:
            db.run("INSERT OR IGNORE INTO listing_prices(listing_id,ts,price) VALUES(?,?,?)",
                   (row["id"], now, d["price"]))
        # auctions move: refresh the live bid data every time we see them
        # `shipping` se rafraîchit à chaque passage plutôt que COALESCE : un
        # vendeur active ou coupe l'envoi en cours de route, et les annonces
        # ingérées avant qu'on sache le lire portaient toutes 0.
        db.run("""UPDATE listings SET last_seen=?, active=1, status='active', gone_at=NULL,
                  price=COALESCE(?,price), bids=COALESCE(?,bids),
                  shipping=COALESCE(?,shipping),
                  auction_end=COALESCE(?,auction_end) WHERE id=?""",
               (now, d.get("price"), d.get("bids"), d.get("shipping"),
                d.get("auction_end"), row["id"]))
        return row["id"], False
    # one guard here beats a guard in every adapter: sqlite binds scalars only
    d = {**d, "dup_key": dup_key(d)}
    vals = [json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
            for v in (d.get(c) for c in COLS)]
    lid = db.run(f"INSERT INTO listings({','.join(COLS)},first_seen,last_seen,status) "
                 f"VALUES({','.join('?' * len(COLS))},?,?,'active')", (*vals, now, now))
    if d.get("price") is not None:
        db.run("INSERT OR IGNORE INTO listing_prices(listing_id,ts,price) VALUES(?,?,?)",
               (lid, now, d["price"]))
    return lid, True

def link_seller(lid, d):
    """Create/refresh the seller record from what the scan already returned.

    986 listings carried a seller name that was going nowhere: the record was
    only built when someone opened a facebook listing. This costs no request.
    """
    from seconde_main import sellers
    key = d.get("seller_key") or d.get("seller_name")
    if not key:
        return None
    sid = sellers.upsert(d["source"], key, name=d.get("seller_name"))
    if sid:
        db.run("UPDATE listings SET seller_ref=? WHERE id=?", (sid, lid))
    return sid

def ensure_coords(lid, d):
    """Offline postcode lookup for sources that don't ship coordinates."""
    if d.get("lat") is not None and d.get("lon") is not None:
        return (d["lat"], d["lon"])
    hit = geo.locate_listing(d.get("location_raw"), d.get("postal_code"), d.get("country"))
    if hit:
        db.run("UPDATE listings SET lat=?, lon=? WHERE id=?", (hit[0], hit[1], lid))
    return hit

def sweep_lifecycle():
    """Mark what has disappeared, keep everything.

    Two different signals, and they must not be confused:
      ended  an auction whose end date has passed -- a fact
      gone   not seen in a while -- could be sold, could be withdrawn, and we
             genuinely do not know which
    """
    now = time.time()
    ended = db.run_count("""UPDATE listings SET status='ended', active=0,
                        gone_at=COALESCE(gone_at, auction_end)
                      WHERE auction_end IS NOT NULL AND auction_end < ?
                        AND COALESCE(status,'active') = 'active'""", (now,))
    cutoff = now - config.GONE_AFTER_HOURS * 3600
    gone = db.run_count("""UPDATE listings SET status='gone', active=0, gone_at=?
                     WHERE last_seen < ? AND COALESCE(status,'active') = 'active'""",
                  (now, cutoff))
    # a listing seen again is back: sellers relist, auctions get extended
    back = db.run_count("""UPDATE listings SET status='active', active=1, gone_at=NULL
                     WHERE last_seen >= ? AND status = 'gone'""", (cutoff,))
    return ended, gone, back

