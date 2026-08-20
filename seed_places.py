"""One-time: pull GeoNames postcode centroids so listing geocoding is offline.

Nominatim rate-limits hard and will ban you if you geocode every listing
through it. Postcodes cover ~all marketplace listings; run this once.
"""
import io, zipfile, time, sys
import db, net

COUNTRIES = ["CH", "FR", "DE", "IT", "AT"]

DDL = """
CREATE TABLE IF NOT EXISTS places (
  country TEXT, postcode TEXT, name TEXT, admin1 TEXT, lat REAL, lon REAL
);
CREATE INDEX IF NOT EXISTS idx_places_pc   ON places(country, postcode);
CREATE INDEX IF NOT EXISTS idx_places_name ON places(country, name);
"""

def seed(countries=None):
    db.init()
    with db.connect() as c:
        c.executescript(DDL)
    for cc in (countries or COUNTRIES):
        r = net.get(f"https://download.geonames.org/export/zip/{cc}.zip", throttle=False)
        if r is None:
            print(f"  {cc}: download failed"); continue
        rows = []
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            for line in z.read(f"{cc}.txt").decode("utf-8").splitlines():
                f = line.split("\t")
                if len(f) < 12 or not f[9] or not f[10]:
                    continue
                rows.append((f[0], f[1], f[2], f[3], float(f[9]), float(f[10])))
        with db.connect() as c:
            c.execute("DELETE FROM places WHERE country=?", (cc,))
            c.executemany("INSERT INTO places(country,postcode,name,admin1,lat,lon)"
                          " VALUES(?,?,?,?,?,?)", rows)
        print(f"  {cc}: {len(rows)} postcodes")

if __name__ == "__main__":
    seed(sys.argv[1:] or None)
    print("total:", db.q("SELECT COUNT(*) n FROM places", one=True)["n"])
