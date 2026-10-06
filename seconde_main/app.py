"""Entry point: the Flask app plus the background scan loop, in one process.

    python -m seconde_main        start the server and the scan loop

The web UI itself lives in web/, one module per area of the app.
"""
import os
from seconde_main import engine
from seconde_main.web import create_app

app = create_app()


def main():
    engine.start_loop()
    # 127.0.0.1 by default: nothing is exposed until you ask for it.
    # In a container HOST=0.0.0.0, and Docker decides what gets published.
    app.run(host=os.environ.get("HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", 5055)), debug=False)


if __name__ == "__main__":
    main()
