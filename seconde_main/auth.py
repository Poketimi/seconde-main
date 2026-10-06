"""Un compte, un mot de passe. Rien de plus.

L'app tourne en local sur ta machine : il n'y a ni inscription publique, ni
e-mail, ni récupération. Tant qu'aucun compte n'existe, l'accès reste libre —
créer un compte est ce qui active le verrou.

Le mot de passe n'est pas stocké : seul son empreinte PBKDF2 l'est, avec un sel
par compte. Ce n'est pas de la haute sécurité, c'est le minimum pour qu'un mot
de passe réutilisé ailleurs ne soit pas lisible en clair dans market.db.
"""
import hashlib, hmac, secrets, time
from seconde_main import db

ROUNDS = 200_000

def _hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ROUNDS).hex()

def count():
    r = db.q("SELECT COUNT(*) c FROM users", one=True)
    return r["c"] if r else 0

def enabled():
    """Pas de compte = pas de verrou. C'est le comportement d'origine."""
    return count() > 0

def create(username, password, is_admin=None):
    """(ok, message). Refuse un doublon ou un mot de passe vide.

    Le premier compte créé est administrateur : c'est lui qui règle l'IA, les
    clés et les autres comptes. Les suivants ont leurs propres recherches et
    leur propre profil, mais pas la main sur les réglages partagés.
    """
    username = (username or "").strip()
    if not username:
        return False, "Il faut un nom d'utilisateur."
    if len(password or "") < 4:
        return False, "Mot de passe trop court (4 caractères minimum)."
    if db.q("SELECT 1 FROM users WHERE username=?", (username,), one=True):
        return False, f"« {username} » existe déjà."
    salt = secrets.token_bytes(16)
    if is_admin is None:
        is_admin = count() == 0          # le premier compte prend la main
    db.run("INSERT INTO users(username,pw,salt,is_admin,created_at) VALUES(?,?,?,?,?)",
           (username, _hash(password, salt), salt.hex(), 1 if is_admin else 0, time.time()))
    return True, (f"Compte « {username} » créé"
                  + (" — administrateur." if is_admin else "."))

def check(username, password):
    u = db.q("SELECT * FROM users WHERE username=?", ((username or "").strip(),), one=True)
    if not u:
        # comparer quand même : un nom inconnu ne doit pas répondre plus vite
        _hash(password or "", b"decoy-salt-000000")
        return False
    return hmac.compare_digest(_hash(password or "", bytes.fromhex(u["salt"])), u["pw"])

def get(username):
    return db.q("SELECT * FROM users WHERE username=?", ((username or "").strip(),), one=True)

def user_id(username):
    u = get(username)
    return u["id"] if u else None

def is_admin(username):
    """Sans aucun compte, l'app est ouverte : tout le monde est administrateur."""
    if not enabled():
        return True
    u = get(username)
    return bool(u and u["is_admin"])

def everyone():
    return db.q("SELECT id, username, is_admin, created_at FROM users ORDER BY id")

def set_password(username, password):
    if len(password or "") < 4:
        return False, "Mot de passe trop court (4 caractères minimum)."
    salt = secrets.token_bytes(16)
    n = db.run_count("UPDATE users SET pw=?, salt=? WHERE username=?",
                     (_hash(password, salt), salt.hex(), (username or "").strip()))
    return (True, "Mot de passe changé.") if n else (False, "Compte inconnu.")

def delete(username):
    """Supprime un compte. Refuse d'enlever le dernier administrateur."""
    u = get(username)
    if not u:
        return 0
    if u["is_admin"] and db.q("SELECT COUNT(*) c FROM users WHERE is_admin=1",
                              one=True)["c"] <= 1:
        return 0        # sinon plus personne ne peut régler quoi que ce soit
    return db.run_count("DELETE FROM users WHERE id=?", (u["id"],))

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
    # un second compte n'est pas administrateur d'office
    db.run("DELETE FROM users WHERE username='demo-two'")
    create("demo-two", "hunter2")
    assert not is_admin("demo-two"), "tout nouveau compte devenait administrateur"
    assert user_id("demo-two") != user_id("demo-user")
    db.run("DELETE FROM users WHERE username='demo-two'")
    assert create("demo-user", "autre")[0] is False, "doublon accepté"
    assert check("demo-user", "hunter2")
    assert not check("demo-user", "hunter3")
    assert not check("inconnu", "hunter2")
    row = db.q("SELECT pw FROM users WHERE username='demo-user'", one=True)
    assert "hunter2" not in row["pw"], "mot de passe stocké en clair"
    assert set_password("demo-user", "nouveau")[0] and check("demo-user", "nouveau")
    assert user_id("demo-user") and get("demo-user")["username"] == "demo-user"
    assert len(secret_key()) == 64 and secret_key() == secret_key()
    # le dernier administrateur ne peut pas être supprimé : sinon plus
    # personne ne peut régler l'IA ni créer de compte
    if is_admin("demo-user") and db.q("SELECT COUNT(*) c FROM users WHERE is_admin=1",
                                      one=True)["c"] == 1:
        assert delete("demo-user") == 0, "le dernier admin a été supprimé"
        db.run("UPDATE users SET is_admin=0 WHERE username='demo-user'")
    assert delete("demo-user") == 1
    print("auth ok")

if __name__ == "__main__":
    demo()
