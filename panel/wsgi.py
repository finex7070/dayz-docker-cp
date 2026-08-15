"""WSGI entry point (loaded by gunicorn as `wsgi:app`)."""

from app import create_app

app = create_app()


if __name__ == "__main__":
    # Local development outside Docker only.
    # Inside the container gunicorn takes over (see gunicorn.conf.py).
    import os

    app.run(host="0.0.0.0", port=int(os.environ.get("PANEL_PORT", "8080")), debug=True)
