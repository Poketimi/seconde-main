"""SQLite schema + tiny helpers. No ORM: the queries here are all short."""
import sqlite3, json, re, time
from config import DB_PATH

SCHEMA = """
PRAGMA journal_mode=WAL;

-- What the user is hunting for -----------------------------------------
CREATE TABLE IF NOT EXISTS searches (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  query TEXT NOT NULL,             -- free text: "iphone 13 pro 256"
  reference TEXT,                  -- exact model/ref if known
  category TEXT,                   -- goods|car|moto|realestate|...
  price_min REAL, price_max REAL,
  condition_min TEXT,              -- new|like_new|good|fair|parts
  seller_type TEXT,                -- any|private|pro
  shipping_ok INTEGER DEFAULT 1,   -- accept shipped items (ignores distance)
  exclude_kw TEXT,                 -- comma separated
  origins TEXT NOT NULL DEFAULT '[]',  -- [{label,lat,lon,max_minutes,mode}]
  sources TEXT NOT NULL DEFAULT '[]',  -- [] = all enabled adapters
  active INTEGER DEFAULT 1,
  last_run REAL, created_at REAL
);

-- Canonical product the AI generates/reuses ("iPhone 13 Pro 256GB") -----
CREATE TABLE IF NOT EXISTS products (
  id INTEGER PRIMARY KEY,
  norm_key TEXT UNIQUE NOT NULL,   -- slug: lookup hits DB, not the AI
  canonical_name TEXT NOT NULL,
  brand TEXT, model TEXT, variant TEXT,
  category TEXT,
  release_year INTEGER,
  msrp REAL,
  specs TEXT DEFAULT '{}',         -- category-specific json
  -- rolling price stats, recomputed from listings: tells you if it's a deal
  n_listings INTEGER DEFAULT 0,
  price_p25 REAL, price_median REAL, price_p75 REAL,
  last_computed REAL, created_at REAL
);

-- Every listing ever seen, matched or not -------------------------------
CREATE TABLE IF NOT EXISTS listings (
  id INTEGER PRIMARY KEY,
  url TEXT UNIQUE NOT NULL,
  source TEXT NOT NULL, source_id TEXT,
  title TEXT, description TEXT,
  price REAL, currency TEXT DEFAULT 'CHF',
  price_type TEXT,                 -- fixed|auction|negotiable|free
  category TEXT, condition TEXT,
  seller_type TEXT, seller_name TEXT,
  location_raw TEXT, postal_code TEXT, country TEXT,
  lat REAL, lon REAL,
  shipping INTEGER DEFAULT 0, shipping_cost REAL,
  image TEXT, images TEXT DEFAULT '[]',
  posted_at REAL, first_seen REAL, last_seen REAL,
  active INTEGER DEFAULT 1,
  product_id INTEGER REFERENCES products(id),
  attrs TEXT DEFAULT '{}',         -- year/mileage/rooms/storage/... per category
  ai_enriched INTEGER DEFAULT 0,
  auction_end REAL,                -- when the auction closes (ricardo)
  bids INTEGER,                    -- number of bids placed
  raw TEXT                         -- original payload, cheap insurance
);
CREATE INDEX IF NOT EXISTS idx_listings_product ON listings(product_id);
CREATE INDEX IF NOT EXISTS idx_listings_seen ON listings(first_seen DESC);

-- Concrete models to hunt for one search. "des skis all-mountain" becomes
-- Salomon QST 99, Head Kore 99, Nordica Enforcer 100... each searched by name,
-- which is far more precise than one fuzzy query.
CREATE TABLE IF NOT EXISTS targets (
  id INTEGER PRIMARY KEY,
  search_id INTEGER NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
  name TEXT NOT NULL,          -- "Salomon QST 99"
  query TEXT NOT NULL,         -- what to type in a site's search box
  why TEXT,
  active INTEGER DEFAULT 1,
  created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_targets_search ON targets(search_id);

-- listing x search ------------------------------------------------------
CREATE TABLE IF NOT EXISTS matches (
  id INTEGER PRIMARY KEY,
  search_id INTEGER NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
  listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
  score REAL, reason TEXT,
  travel_minutes REAL, travel_origin TEXT, travel_mode TEXT,
  deal_delta REAL,                 -- % vs product median, negative = bargain
  notified INTEGER DEFAULT 0, seen INTEGER DEFAULT 0, starred INTEGER DEFAULT 0,
  created_at REAL,
  UNIQUE(search_id, listing_id)
);

-- assisted search -------------------------------------------------------
-- Facts the user gave during an interview (height, level, ...). Reused
-- silently on later interviews; fully visible and erasable at /profil.
CREATE TABLE IF NOT EXISTS profile_facts (
  k TEXT PRIMARY KEY,
  label TEXT, value TEXT,
  scope TEXT,               -- 'global' (taille, sexe) | 'domain' (ski.niveau)
  updated_at REAL
);

-- Generated question sets, cached per category: a second ski search reuses
-- them and costs nothing.
CREATE TABLE IF NOT EXISTS interview_templates (
  cat_key TEXT PRIMARY KEY,
  sample_query TEXT, questions TEXT, model TEXT, created_at REAL
);

-- Ledger for the expensive model, so the yearly cap is enforced on measured
-- spend rather than on an estimate.
CREATE TABLE IF NOT EXISTS ai_spend (
  id INTEGER PRIMARY KEY, ts REAL, model TEXT, purpose TEXT,
  tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL
);
CREATE INDEX IF NOT EXISTS idx_spend_ts ON ai_spend(ts);

-- Assistant state. Deliberately not in the session cookie: it is capped at
-- 4 KB and Flask drops anything larger without a word, which loses the
-- criteria and makes the assistant look like it produced nothing.
CREATE TABLE IF NOT EXISTS assist_state (
  token TEXT PRIMARY KEY, data TEXT, updated_at REAL
);

-- Price history. Nothing is ever deleted: a listing that disappears is marked,
-- not removed, and a daily snapshot per product makes trends visible.
CREATE TABLE IF NOT EXISTS price_points (
  product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
  day TEXT NOT NULL,                 -- YYYY-MM-DD
  n INTEGER, p25 REAL, median REAL, p75 REAL, lo REAL, hi REAL,
  PRIMARY KEY (product_id, day)
);

-- Sellers drop their price; each change is worth keeping.
CREATE TABLE IF NOT EXISTS listing_prices (
  listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
  ts REAL NOT NULL, price REAL,
  PRIMARY KEY (listing_id, ts)
);

-- Seller reputation, one row per seller per site, reused by all their ads.
-- Mainly an anti-scam signal on facebook: a brand-new account with no history
-- is the common thread in marketplace scams.
CREATE TABLE IF NOT EXISTS sellers (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL, seller_key TEXT NOT NULL,
  name TEXT, profile_url TEXT,
  member_since INTEGER,          -- year the account was created
  listings_count INTEGER,
  rating REAL, rating_count INTEGER,
  location TEXT,
  checked_at REAL,
  UNIQUE(source, seller_key)
);

-- Listing text in another language. The original is never overwritten: it
-- stays in listings.title/description and these are additions beside it.
CREATE TABLE IF NOT EXISTS listing_i18n (
  listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
  lang TEXT NOT NULL,
  title TEXT, description TEXT, created_at REAL,
  PRIMARY KEY (listing_id, lang)
);

-- User-editable knobs that would otherwise force an .env edit + restart:
-- AI provider, key, models. Read at startup, applied onto `config`.
CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT, updated_at REAL);

-- Un seul utilisateur en pratique. Tant que la table est vide l'app reste
-- ouverte : créer un compte est ce qui active le verrou.
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  username TEXT UNIQUE NOT NULL,
  pw TEXT NOT NULL, salt TEXT NOT NULL,     -- PBKDF2-SHA256, sel par compte
  created_at REAL
);

-- caches ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ai_cache  (k TEXT PRIMARY KEY, v TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS geo_cache (k TEXT PRIMARY KEY, lat REAL, lon REAL, created_at REAL);
CREATE TABLE IF NOT EXISTS route_cache (k TEXT PRIMARY KEY, minutes REAL, created_at REAL);
-- per-source health, so the UI can say WHY a site returned nothing --------
CREATE TABLE IF NOT EXISTS source_health (
  source TEXT PRIMARY KEY,
  status TEXT,              -- ok|empty|blocked|login|error
  detail TEXT,
  fail_streak INTEGER DEFAULT 0,
  last_ok REAL, last_run REAL, changed_at REAL
);

CREATE TABLE IF NOT EXISTS runlog (
  id INTEGER PRIMARY KEY, ts REAL, source TEXT, search_id INTEGER,
  found INTEGER, new INTEGER, ok INTEGER, note TEXT
);
"""

def connect():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c

# columns added after the first release; sqlite has no ADD COLUMN IF NOT EXISTS
MIGRATIONS = [("listings", "auction_end", "REAL"),
              ("listings", "bids", "INTEGER"),
              ("listings", "dup_key", "TEXT"),
              ("matches", "target_id", "INTEGER"),
              ("listings", "gone_at", "REAL"),
              ("listings", "status", "TEXT"),
              ("listings", "seller_ref", "INTEGER"),
              ("products", "ref_price", "REAL"),
              ("products", "ref_url", "TEXT"),
              ("products", "ref_source", "TEXT"),
              ("products", "ref_checked", "REAL"),
              ("listings", "lang", "TEXT")]      # source language, detected once

def init():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as c:
        c.executescript(SCHEMA)
        for table, col, typ in MIGRATIONS:
            have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
            if col not in have:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        # indexes on migrated columns must come after the ALTER, not in SCHEMA
        c.execute("CREATE INDEX IF NOT EXISTS idx_listings_dup ON listings(dup_key)")

def q(sql, args=(), one=False):
    with connect() as c:
        rows = c.execute(sql, args).fetchall()
    return (rows[0] if rows else None) if one else rows

# Guard against the mistake that has now wiped this database twice: an ad-hoc
# script running "DELETE FROM searches" against market.db. Bulk deletes on the
# user's tables must be deliberate, not a stray line in a verification script.
PROTECTED = ("searches", "listings", "matches", "targets")
_BULK = re.compile(r"^\s*(DELETE\s+FROM|DROP\s+TABLE)\s+[\"'`]?(\w+)", re.I)

def _guard(sql):
    import os
    # only the live database is protected; the test suite runs on a scratch
    # copy and is entitled to clear its own tables
    if DB_PATH.name != "market.db":
        return
    m = _BULK.match(sql or "")
    if not m:
        return
    table = m.group(2).lower()
    if table not in PROTECTED:
        return
    if " where " in (sql or "").lower():
        return                       # targeted delete: fine
    if os.environ.get("ALLOW_BULK_DELETE") == "1":
        return
    raise RuntimeError(
        f"refus: DELETE global sur '{table}' (données utilisateur). "
        f"Utilise une clause WHERE, ou ALLOW_BULK_DELETE=1 si c'est voulu.")

def run(sql, args=()):
    _guard(sql)
    with connect() as c:
        cur = c.execute(sql, args)
        return cur.lastrowid

def run_count(sql, args=()):
    _guard(sql)
    """Rows affected. `run` returns lastrowid, which is meaningless for UPDATE
    and silently reported 0 rows changed when 41 had been."""
    with connect() as c:
        return c.execute(sql, args).rowcount

def cache_get(table, k):
    r = q(f"SELECT * FROM {table} WHERE k=?", (k,), one=True)
    return r

def recompute_product_stats(product_id):
    """Median/quartiles from active listings -> instant 'is this a deal?'."""
    rows = q("SELECT price FROM listings WHERE product_id=? AND price>0 AND active=1"
             " ORDER BY price", (product_id,))
    prices = [r["price"] for r in rows]
    if not prices:
        return
    def pct(p):
        i = max(0, min(len(prices) - 1, int(round(p * (len(prices) - 1)))))
        return prices[i]
    run("UPDATE products SET n_listings=?, price_p25=?, price_median=?, price_p75=?,"
        " last_computed=? WHERE id=?",
        (len(prices), pct(.25), pct(.5), pct(.75), time.time(), product_id))
    # one point per product per day: enough to draw a trend, cheap to keep
    day = time.strftime("%Y-%m-%d")
    run("""INSERT INTO price_points(product_id,day,n,p25,median,p75,lo,hi)
           VALUES(?,?,?,?,?,?,?,?)
           ON CONFLICT(product_id,day) DO UPDATE SET
             n=excluded.n, p25=excluded.p25, median=excluded.median,
             p75=excluded.p75, lo=excluded.lo, hi=excluded.hi""",
        (product_id, day, len(prices), pct(.25), pct(.5), pct(.75),
         prices[0], prices[-1]))

if __name__ == "__main__":
    init(); print("initialised", DB_PATH)
