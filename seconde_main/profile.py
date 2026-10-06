"""Personal facts gathered during an assisted search.

Stored so later interviews don't re-ask your height every time. Reused
silently, per the user's choice -- which is exactly why every fact must stay
visible, correctable and erasable at /profil.

Nothing here ever reaches a marketplace. Facts go only to the interview model,
to turn "1m78, intermédiaire" into concrete search criteria.
"""
import time
from seconde_main import db

def get_all():
    return db.q("SELECT * FROM profile_facts ORDER BY scope, k")

def get_map(prefix=None):
    """{key: value}. With a prefix, only that domain's facts plus the globals."""
    rows = get_all()
    out = {}
    for r in rows:
        if r["scope"] == "global" or prefix is None or r["k"].startswith(f"{prefix}."):
            out[r["k"]] = r["value"]
    return out

def upsert(k, label, value, scope="global"):
    if not k or value in (None, ""):
        return
    db.run("""INSERT INTO profile_facts(k,label,value,scope,updated_at) VALUES(?,?,?,?,?)
              ON CONFLICT(k) DO UPDATE SET label=?, value=?, scope=?, updated_at=?""",
           (k, label, str(value), scope, time.time(),
            label, str(value), scope, time.time()))

def delete(k):
    db.run("DELETE FROM profile_facts WHERE k=?", (k,))

def wipe():
    db.run("DELETE FROM profile_facts")

def demo():
    db.init()
    wipe()
    upsert("height_cm", "Taille", "170-180", "global")
    upsert("ski.level", "Niveau ski", "Intermédiaire", "domain")
    upsert("tennis.level", "Niveau tennis", "Débutant", "domain")
    m = get_map("ski")
    assert m.get("height_cm") == "170-180", "les faits globaux suivent tous les domaines"
    assert m.get("ski.level") == "Intermédiaire"
    assert "tennis.level" not in m, "un autre domaine ne doit pas fuiter"
    upsert("height_cm", "Taille", "180-190", "global")
    assert get_map()["height_cm"] == "180-190", "correction non appliquée"
    delete("ski.level")
    assert "ski.level" not in get_map("ski")
    wipe()
    assert not get_all(), "wipe doit tout effacer"
    print("profile ok")

if __name__ == "__main__":
    demo()
