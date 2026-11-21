"""Vercel entrypoint: re-export the FastAPI app from server.py.

Vercel's Python runtime serves this file as the /api/* function;
vercel.json rewrites /api/:path* (and /) here, and the request keeps its
original path, so the app's / and /api/chat routes match unchanged.
Local dev is untouched: uvicorn server:app.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from server import app  # noqa: E402,F401
