"""Point d'entrée. L'interface elle-même vit dans web/, un fichier par domaine.

    python3 app.py        lance le serveur et la boucle de scan
"""
import engine
from web import create_app

app = create_app()

if __name__ == "__main__":
    engine.start_loop()
    app.run(host="127.0.0.1", port=5055, debug=False)
