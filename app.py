"""Entry point for Vercel, which looks for a WSGI `app` in app.py.

Settings come from the Vercel project's environment variables. There's no
background scheduler here: Vercel's cron calls /tasks/sync-all instead.
"""

from webapp import create_app

app = create_app()
