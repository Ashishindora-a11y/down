"""WSGI entrypoint for gunicorn: `gunicorn wsgi:app`."""

from app import app

__all__ = ["app"]
