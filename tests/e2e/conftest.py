"""Fixtures for the browser flow test.

By default this starts the gates service and the viewer dev server as local
subprocesses on free ports.  Set E2E_BASE_URL to instead target an already
running deployment (e.g. `docker compose up` → http://localhost:8080).
"""

import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Chromium may need locally extracted shared libraries (no-root environments).
_LOCAL_LIBS = Path.home() / ".local/chromium-libs"
if _LOCAL_LIBS.exists():
    _paths = [
        str(_LOCAL_LIBS / "usr/lib/aarch64-linux-gnu"),
        str(_LOCAL_LIBS / "lib/aarch64-linux-gnu"),
    ]
    if os.environ.get("LD_LIBRARY_PATH"):
        _paths.append(os.environ["LD_LIBRARY_PATH"])
    os.environ["LD_LIBRARY_PATH"] = ":".join(_paths)

playwright = pytest.importorskip("playwright", reason="playwright not installed")


def _free_port():
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_http(url, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError(f"service at {url} did not come up")


@pytest.fixture(scope="session")
def services():
    override = os.environ.get("E2E_BASE_URL")
    if override:
        yield override.rstrip("/")
        return
    gates_port = _free_port()
    viewer_port = _free_port()
    env = dict(os.environ)
    gates = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, {!r}); "
            "from app import create_app; "
            "create_app().run(port={!r}, use_reloader=False)".format(
                str(REPO_ROOT / "gates"), gates_port
            ),
        ],
        env=env,
    )
    viewer_env = dict(env)
    viewer_env["PORT"] = str(viewer_port)
    viewer_env["GATES_URL"] = f"http://127.0.0.1:{gates_port}"
    viewer = subprocess.Popen(
        [sys.executable, str(REPO_ROOT / "viewer" / "dev_server.py")],
        env=viewer_env,
    )
    try:
        _wait_http(f"http://127.0.0.1:{gates_port}/api/health")
        _wait_http(f"http://127.0.0.1:{viewer_port}/")
        yield f"http://127.0.0.1:{viewer_port}"
    finally:
        for proc in (viewer, gates):
            proc.terminate()
        for proc in (viewer, gates):
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


@pytest.fixture(scope="session")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"chromium cannot launch: {exc}")
        yield browser
        browser.close()


@pytest.fixture()
def page(browser):
    page = browser.new_page()
    yield page
    page.close()
