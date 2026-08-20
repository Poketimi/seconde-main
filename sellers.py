"""Seller reputation, to catch the classic marketplace scam: a brand-new
account with no history.

Deliberately one-sided. A long history is weak evidence of honesty, but a
week-old account with no listings is a real warning, so this flags RISK and
never certifies anyone as safe.

Data is fetched lazily (opening a listing) and cached per seller, because on
facebook it costs a browser page view and those must stay rare.
"""
import re, time, datetime
import db

REFRESH_AFTER = 30 * 86400          # reputation moves slowly

def get(source, key):
    return db.q("SELECT * FROM sellers WHERE source=? AND seller_key=?",
                (source, str(key)), one=True) if key else None

def upsert(source, key, **f):
    if not key:
        return None
    key = str(key)
    row = get(source, key)
    cols = ("name", "profile_url", "member_since", "listings_count",
            "rating", "rating_count", "location")
    if row:
        sets = ", ".join(f"{c}=COALESCE(?,{c})" for c in cols)
        db.run(f"UPDATE sellers SET {sets}, checked_at=? WHERE id=?",
               (*[f.get(c) for c in cols], time.time(), row["id"]))
        return row["id"]
    return db.run(
        f"INSERT INTO sellers(source,seller_key,{','.join(cols)},checked_at)"
        f" VALUES(?,?,{','.join('?' * len(cols))},?)",
        (source, key, *[f.get(c) for c in cols], time.time()))

def stale(row):
    return not row or not row["checked_at"] or (time.time() - row["checked_at"]) > REFRESH_AFTER

# --- facebook ------------------------------------------------------------
PROFILE_JS = """() => {
  const t = document.body.innerText;
  const g = re => { const m = t.match(re); return m ? m[0] : null; };
  // the display name sits on the line just before the join date
  const lines = t.split('\\n').map(x => x.trim()).filter(Boolean);
  let nm = null;
  for (let i = 1; i < lines.length; i++) {
    if (/^(A rejoint Facebook|Membre de Facebook|Joined Facebook)/i.test(lines[i])) {
      nm = lines[i - 1]; break;
    }
  }
  return {
    name: nm,
    joined: g(/(A rejoint Facebook en|Membre de Facebook depuis|Joined Facebook in)\\s*(\\d{4})/i),
    count:  g(/(\\d+\\+?)\\s*annonces? en ligne|(\\d+\\+?)\\s*listings?/i),
    rating: g(/([\\d.,]+)\\s*\\(\\s*(\\d+)\\s*(évaluation|avis|rating)/i),
    place:  g(/Habite à[^\\n]{0,40}|Lives in[^\\n]{0,40}/i)
  };
}"""

def _int(s):
    m = re.search(r"\d+", s or "")
    return int(m.group(0)) if m else None

def fetch_facebook(profile_url):
    """Read a facebook seller profile. Returns {} when unreachable."""
    import browser
    data = browser.eval_page(profile_url, PROFILE_JS, wait_ms=6000, wait_for="body")
    if not data:
        return {}
    rating = rating_n = None
    if data.get("rating"):
        m = re.search(r"([\d.,]+)\s*\(\s*(\d+)", data["rating"])
        if m:
            rating, rating_n = float(m.group(1).replace(",", ".")), int(m.group(2))
    name = (data.get("name") or "").strip() or None
    if name and (len(name) > 60 or any(c.isdigit() for c in name)):
        name = None                    # picked up a stray line, not a name
    return {"name": name,
            "member_since": _int(data.get("joined")),
            "listings_count": _int(data.get("count")),
            "rating": rating, "rating_count": rating_n,
            "location": (data.get("place") or "").replace("Habite à", "").strip() or None,
            "profile_url": profile_url}

# --- the verdict ---------------------------------------------------------
def seen_count(seller_id):
    """How many of this seller's listings we have observed ourselves.

    Our own history is real evidence, and for ricardo/anibis/tutti/leboncoin it
    is the only evidence we have: they expose no join date.
    """
    if not seller_id:
        return 0
    r = db.q("SELECT COUNT(*) n FROM listings WHERE seller_ref=?", (seller_id,), one=True)
    return r["n"] if r else 0

def display_name(row, fallback=None):
    """A bare numeric id is not a name. Ricardo's search payload has no
    nickname, so show it as an id rather than pretending "405151536" is one."""
    n = (row["name"] if row else None) or fallback
    if n and str(n).isdigit():
        return f"vendeur #{n}"
    return n or "Vendeur"

def assess(row, observed=None):
    """-> (level, label, why). level: risk | caution | ok | unknown."""
    if not row:
        return "unknown", "vendeur inconnu", "aucune information récupérée"
    if observed is None:
        observed = seen_count(row["id"])
    year = datetime.date.today().year
    since, n = row["member_since"], row["listings_count"] or 0
    rc = row["rating_count"] or 0
    if since is None:
        # no join date on this site: fall back to what we have watched ourselves
        if observed >= 5:
            return "ok", f"{observed} annonces observées", \
                   "vendeur récurrent dans nos scans — pas un compte jetable"
        if observed > 1:
            return "unknown", f"{observed} annonces observées", \
                   "ce site ne publie pas l'ancienneté du compte"
        return "unknown", "vendeur peu vu", \
               "une seule annonce observée, et ce site ne publie pas l'ancienneté"
    age = year - since
    if age <= 0:
        return "risk", f"compte créé en {since}", \
               "compte de l'année : le profil type des arnaques"
    if age == 1 and n <= 1 and rc == 0:
        return "caution", f"compte de {since}, peu actif", \
               "récent et sans historique — prudence sur le paiement"
    bits = [f"compte depuis {since} ({age} ans)"]
    if observed > 1:
        bits.append(f"{observed} annonces observées")
    if n:
        bits.append(f"{n} annonces")
    if rc:
        bits.append(f"{row['rating']}/5 sur {rc} avis")
    return "ok", f"compte depuis {since}", " · ".join(bits)

def demo():
    db.init()
    db.run("DELETE FROM sellers WHERE source='demo'")
    y = datetime.date.today().year
    sid = upsert("demo", "a", name="Neuf", member_since=y, listings_count=1)
    assert assess(get("demo", "a"))[0] == "risk"
    upsert("demo", "b", name="Vieux", member_since=y - 12, listings_count=30)
    assert assess(get("demo", "b"))[0] == "ok"
    upsert("demo", "c", name="Récent", member_since=y - 1, listings_count=1)
    assert assess(get("demo", "c"))[0] == "caution"
    assert assess(None)[0] == "unknown"
    assert assess(get("demo", "a"))[0] != "ok", "un compte neuf ne doit jamais passer pour sûr"
    # COALESCE keeps known values when a later fetch returns nothing
    upsert("demo", "b", name=None, member_since=None)
    assert get("demo", "b")["member_since"] == y - 12, "une lecture vide ne doit rien écraser"
    db.run("DELETE FROM sellers WHERE source='demo'")
    print("sellers ok")

if __name__ == "__main__":
    demo()
