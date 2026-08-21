"""Point d'entrée. L'interface elle-même vit dans web/, un fichier par domaine.

    python3 app.py        lance le serveur et la boucle de scan
"""
import os
import engine
from web import create_app

app = create_app()

if __name__ == "__main__":
    engine.start_loop()
    # 127.0.0.1 par défaut : rien n'est exposé tant que tu ne le demandes pas.
    # En conteneur, HOST=0.0.0.0 — c'est docker qui décide de la publication.
    app.run(host=os.environ.get("HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", 5055)), debug=False)
