#!/usr/bin/env python3
"""Dev server for the viewer: serves the static files and proxies /api to
the gates service, mirroring what nginx does in the Docker deployment.

Usage:
    GATES_URL=http://127.0.0.1:5001 PORT=8080 python3 dev_server.py
"""

import os
import urllib.error
import urllib.request
from pathlib import Path

from flask import Flask, Response, request, send_from_directory

ROOT = Path(__file__).resolve().parent
GATES_URL = os.environ.get("GATES_URL", "http://127.0.0.1:5001").rstrip("/")

app = Flask(__name__, static_folder=None)


@app.route("/api/<path:path>", methods=["GET", "POST"])
def proxy(path):
    upstream = urllib.request.Request(
        f"{GATES_URL}/api/{path}",
        data=request.get_data() if request.method == "POST" else None,
        method=request.method,
        headers={"Content-Type": request.content_type or "application/json"},
    )
    try:
        with urllib.request.urlopen(upstream) as resp:
            return Response(
                resp.read(),
                status=resp.status,
                content_type=resp.headers.get("Content-Type", "application/json"),
            )
    except urllib.error.HTTPError as err:  # upstream 4xx/5xx still proxied through
        return Response(
            err.read(),
            status=err.code,
            content_type=err.headers.get("Content-Type", "application/json"),
        )


@app.route("/")
def index():
    return send_from_directory(ROOT, "index.html")


@app.route("/<path:path>")
def statics(path):
    return send_from_directory(ROOT, path)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
