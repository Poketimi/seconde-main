"""Un compte, un mot de passe. Rien de plus.

L'app tourne en local sur ta machine : il n'y a ni inscription publique, ni
e-mail, ni récupération. Tant qu'aucun compte n'existe, l'accès reste libre —
créer un compte est ce qui active le verrou.

Le mot de passe n'est pas stocké : seul son empreinte PBKDF2 l'est, avec un sel
par compte. Ce n'est pas de la haute sécurité, c'est le minimum pour qu'un mot
de passe réutilisé ailleurs ne soit pas lisible en clair dans market.db.
"""
import hashlib, hmac, secrets, time
import db

ROUNDS = 200_000

def _hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ROUNDS).hex()

def count():
    r = db.q("SELECT COUNT(*) c FROM users", one=True)
    return r["c"] if r else 0

def enabled():
    """Pas de compte = pas de verrou. C'est le comportement d'origine."""
    return count() > 0

def create(username, password):
    """(ok, message). Refuse un doublon ou un mot de passe vide."""
    username = (username or "").strip()
    if not username:
        return False, "Il faut un nom d'utilisateur."
    if len(password or "") < 4:
        return False, "Mot de passe trop court (4 caractères minimum)."
    if db.q("SELECT 1 FROM users WHERE username=?", (username,), one=True):
        return False, f"« {username} » existe déjà."
    salt = secrets.token_bytes(16)
    db.run("INSERT INTO users(username,pw,salt,created_at) VALUES(?,?,?,?)",
           (username, _hash(password, salt), salt.hex(), time.time()))
    return True, f"Compte « {username} » créé."

def check(username, password):
    u = db.q("SELECT * FROM users WHERE username=?", ((username or "").strip(),), one=True)
    if not u:
        # comparer quand même : un nom inconnu ne doit pas répondre plus vite
        _hash(password or "", b"decoy-salt-000000")
        return False
    return hmac.compare_digest(_hash(password or "", bytes.fromhex(u["salt"])), u["pw"])

def set_password(username, password):
    if len(password or "") < 4:
        return False, "Mot de passe trop court (4 caractères minimum)."
    salt = secrets.token_bytes(16)
    n = db.run_count("UPDATE users SET pw=?, salt=? WHERE username=?",
                     (_hash(password, salt), salt.hex(), (username or "").strip()))
    return (True, "Mot de passe changé.") if n else (False, "Compte inconnu.")

def delete(username):
    return db.run_count("DELETE FROM users WHERE username=?", ((username or "").strip(),))

def secret_key():
    """Clé de signature des cookies, tirée une fois et gardée.

    Elle était écrite en dur dans le code. Avec un mot de passe à la clé, une
    valeur publiée dans le dépôt laisserait fabriquer un cookie de session.
    """
    r = db.q("SELECT v FROM settings WHERE k='SECRET_KEY'", one=True)
    if r and r["v"]:
        return r["v"]
    v = secrets.token_hex(32)
    db.run("INSERT OR REPLACE INTO settings(k,v,updated_at) VALUES('SECRET_KEY',?,?)",
           (v, time.time()))
    return v

def demo():
    db.init()
    db.run("DELETE FROM users WHERE username='demo-user'")
    assert create("demo-user", "abc")[0] is False, "mot de passe court accepté"
    ok, _ = create("demo-user", "hunter2")
    assert ok
    assert create("demo-user", "autre")[0] is False, "doublon accepté"
    assert check("demo-user", "hunter2")
    assert not check("demo-user", "hunter3")
    assert not check("inconnu", "hunter2")
    row = db.q("SELECT pw FROM users WHERE username='demo-user'", one=True)
    assert "hunter2" not in row["pw"], "mot de passe stocké en clair"
    assert set_password("demo-user", "nouveau")[0] and check("demo-user", "nouveau")
    assert len(secret_key()) == 64 and secret_key() == secret_key()
    assert delete("demo-user") == 1
    print("auth ok")

if __name__ == "__main__":
    demo()
