"""Geocoding + travel time in MINUTES by mode.

Geocoding is offline-first: GeoNames postcode centroids (see seed_places.py)
cover essentially every marketplace listing. Nominatim is reserved for the
handful of named origins the user types ("EPFL"), because it rate-limits hard.

Travel time is two-stage on purpose: a crow-fly estimate is free and rejects
most listings; only survivors near the threshold cost an OSRM round-trip.
"""
import math, time, re, datetime
from seconde_main import db, config, net

_last_nominatim = [0.0]

def haversine_km(a, b):
    (la1, lo1), (la2, lo2) = a, b
    p1, p2 = math.radians(la1), math.radians(la2)
    dp, dl = math.radians(la2 - la1), math.radians(lo2 - lo1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))

# --- offline lookups ---------------------------------------------------
def by_postcode(postcode, country=None):
    if not postcode:
        return None
    pc = str(postcode).strip()
    sql = "SELECT lat,lon FROM places WHERE postcode=?"
    args = [pc]
    if country:
        sql += " AND country=?"; args.append(country)
    r = db.q(sql + " LIMIT 1", tuple(args), one=True)
    return (r["lat"], r["lon"]) if r else None

def by_place_name(name, country=None):
    if not name:
        return None
    sql = "SELECT lat,lon FROM places WHERE name=? COLLATE NOCASE"
    args = [name.strip()]
    if country:
        sql += " AND country=?"; args.append(country)
    r = db.q(sql + " LIMIT 1", tuple(args), one=True)
    return (r["lat"], r["lon"]) if r else None

def locate_listing(location_raw=None, postal_code=None, country=None):
    """Best-effort coords for a listing, offline. Returns (lat,lon) or None."""
    hit = by_postcode(postal_code, country)
    if hit:
        return hit
    if location_raw:
        # "1201 Genève" / "Genève, GE" / "75011 Paris"
        m = re.search(r"\b(\d{4,5})\b", location_raw)
        if m:
            hit = by_postcode(m.group(1), country)
            if hit:
                return hit
        # try each comma part, most specific first: "EPFL, Lausanne" -> Lausanne.
        # Keeps origins resolvable offline when Nominatim is unreachable.
        for part in [p.strip() for p in re.split(r"[,/]", location_raw) if p.strip()]:
            town = re.sub(r"[\d]", " ", part).strip()
            town = re.split(r"\s{2,}|\(", town)[0].strip()
            if not town:
                continue
            hit = by_place_name(town, country)
            if hit:
                return hit
    return None

# --- online geocode, for user-typed origins only -----------------------
def geocode(place, country=None):
    """Free-text place -> (lat, lon). Offline table first, then Nominatim."""
    if not place or not place.strip():
        return None
    key = place.strip().lower()
    row = db.cache_get("geo_cache", key)
    if row and (row["lat"] is not None or time.time() - row["created_at"] < 6 * 3600):
        # negative results expire: a transient 403 must not poison the cache
        return (row["lat"], row["lon"]) if row["lat"] is not None else None

    out = locate_listing(location_raw=place, country=country)
    if out is None:
        wait = 1.2 - (time.time() - _last_nominatim[0])
        if wait > 0:
            time.sleep(wait)
        _last_nominatim[0] = time.time()
        r = net.get("https://nominatim.openstreetmap.org/search",
                    params={"q": place, "format": "json", "limit": 1},
                    headers={"User-Agent": f"seconde-main/1.0 ({config.GEOCODER_EMAIL})"},
                    throttle=False)
        try:
            j = r.json() if r is not None else []
            out = (float(j[0]["lat"]), float(j[0]["lon"])) if j else None
        except Exception:
            out = None
    db.run("INSERT OR REPLACE INTO geo_cache(k,lat,lon,created_at) VALUES(?,?,?,?)",
           (key, out[0] if out else None, out[1] if out else None, time.time()))
    return out

# --- travel time -------------------------------------------------------
def estimate_minutes(origin, dest, mode):
    km = haversine_km(origin, dest) * config.DETOUR_FACTOR.get(mode, 1.3)
    return km / config.MODE_SPEED_KMH.get(mode, 30.0) * 60.0

CH_BOUNDS = (45.8, 47.9, 5.9, 10.6)          # lat_min, lat_max, lon_min, lon_max

def _in_ch(p):
    return CH_BOUNDS[0] <= p[0] <= CH_BOUNDS[1] and CH_BOUNDS[2] <= p[1] <= CH_BOUNDS[3]

def transit_minutes(origin, dest):
    """Real public-transport journey time via transport.opendata.ch (CH only).

    The crow-fly estimate is badly wrong for transit -- it put Lausanne->Grandson
    at ~100min when the train takes 42 -- so a speed model silently threw away
    reachable listings.
    """
    if not (_in_ch(origin) and _in_ch(dest)):
        return None
    key = f"transit:{origin[0]:.4f},{origin[1]:.4f}>{dest[0]:.4f},{dest[1]:.4f}"
    row = db.cache_get("route_cache", key)
    if row:
        return row["minutes"]
    # Ask for a representative weekday mid-morning, not "right now": the API
    # includes waiting time, so an overnight query makes every trip look far.
    # Take the best of a few connections for the same reason.
    d = datetime.date.today()
    while d.weekday() != 1:                 # next Tuesday
        d += datetime.timedelta(days=1)
    r = net.get("http://transport.opendata.ch/v1/connections",
                params={"from": f"{origin[0]},{origin[1]}",
                        "to": f"{dest[0]},{dest[1]}", "limit": 4,
                        "date": d.isoformat(), "time": "10:00"},
                throttle=False)
    try:
        conns = r.json().get("connections") or []
        best = None
        for c in conns:
            days, hms = c["duration"].split("d")      # "00d00:42:00"
            h, m, sec = (int(x) for x in hms.split(":"))
            mins = int(days) * 1440 + h * 60 + m + sec / 60.0
            best = mins if best is None else min(best, mins)
        if best is None:
            return None
        mins = best
    except Exception:
        return None
    db.run("INSERT OR REPLACE INTO route_cache(k,minutes,created_at) VALUES(?,?,?)",
           (key, mins, time.time()))
    return mins

def osrm_minutes(origin, dest, mode):
    """Real road routing. None for transit or on any failure."""
    prof = config.OSRM_PROFILE.get(mode)
    if not prof:
        return None
    key = f"{prof}:{origin[0]:.4f},{origin[1]:.4f}>{dest[0]:.4f},{dest[1]:.4f}"
    row = db.cache_get("route_cache", key)
    if row:
        return row["minutes"]
    url = (f"{config.OSRM_URL}/route/v1/{prof}/"
           f"{origin[1]},{origin[0]};{dest[1]},{dest[0]}?overview=false")
    r = net.get(url, throttle=False)
    try:
        j = r.json() if r is not None else {}
        if j.get("code") != "Ok" or not j.get("routes"):
            return None
        mins = j["routes"][0]["duration"] / 60.0
    except Exception:
        return None
    db.run("INSERT OR REPLACE INTO route_cache(k,minutes,created_at) VALUES(?,?,?)",
           (key, mins, time.time()))
    return mins

def travel_minutes(origin, dest, mode, limit=None):
    """Minutes origin->dest. Refines with OSRM only when near the limit."""
    est = estimate_minutes(origin, dest, mode)
    if mode == "transit":
        # never skip on the estimate here: it over-reads badly (trains are fast)
        real = transit_minutes(origin, dest)
        if real is None:
            return est
        # We only know the listing's postcode, not its street, so the API
        # sometimes routes to an awkward point and returns a worse time than
        # simply walking. Nobody does that, so cap it at the walk.
        return min(real, estimate_minutes(origin, dest, "foot"))
    if limit is not None and est > limit * config.REFINE_BAND:
        return est          # comfortably too far, don't spend a request
    real = osrm_minutes(origin, dest, mode)
    return real if real is not None else est

def best_origin(dest, origins):
    """Closest-in-time origin accepting this dest.

    origins: [{label, lat, lon, max_minutes, mode}]
    -> (ok, minutes, label, mode); ok=True if any origin is within its limit.
    """
    best = None
    for o in origins:
        if o.get("lat") is None or o.get("lon") is None:
            continue
        lim = float(o.get("max_minutes") or 30)
        mins = travel_minutes((o["lat"], o["lon"]), dest, o.get("mode", "car"), lim)
        cand = (mins <= lim, mins, o.get("label", "?"), o.get("mode", "car"))
        if best is None or (cand[0], -cand[1]) > (best[0], -best[1]):
            best = cand
    return best or (False, None, None, None)

def demo():
    lausanne = by_postcode("1015", "CH")          # EPFL's postcode
    grandson = by_postcode("1422", "CH")
    assert lausanne and grandson, "run: python -m seconde_main.seed_places"
    d = haversine_km(lausanne, grandson)
    assert 30 < d < 80, f"EPFL-Grandson ~50km crow-fly, got {d:.1f}"

    assert locate_listing("1201 Genève"), "postcode inside free text must resolve"
    assert locate_listing(None, "75011", "FR"), "FR postcode must resolve"

    walk = estimate_minutes(lausanne, (lausanne[0] + 0.009, lausanne[1]), "foot")
    assert 8 < walk < 25, f"~1km walk should be ~15min, got {walk:.1f}"

    far = travel_minutes(lausanne, grandson, "foot", limit=10)
    assert far > 10, "Grandson is not a 10min walk from EPFL"

    ok, mins, label, _ = best_origin(grandson, [
        {"label": "EPFL", "lat": lausanne[0], "lon": lausanne[1], "max_minutes": 10, "mode": "foot"},
        {"label": "Grandson", "lat": grandson[0], "lon": grandson[1], "max_minutes": 20, "mode": "car"}])
    assert ok and label == "Grandson", f"should match Grandson by car, got {label}"
    print(f"geo ok: EPFL-Grandson {d:.0f}km; matched via {label} in {mins:.0f}min")

if __name__ == "__main__":
    db.init(); demo()
