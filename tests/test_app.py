import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import server  # noqa: E402


@pytest.fixture()
def base_url(monkeypatch):
    monkeypatch.setenv("PREVIEW_PR", "12")
    monkeypatch.setenv("GIT_SHA", "<script>x")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def get(url):
    with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310
        return resp.status, resp.read().decode()


def test_health_version_and_index(base_url):
    assert get(base_url + "/healthz") == (200, "ok")
    status, body = get(base_url + "/version")
    assert status == 200 and json.loads(body)["pr"] == "12"
    status, body = get(base_url + "/")
    assert "PR #12" in body and "<script>" not in body and "&lt;script&gt;" in body


def test_unknown_path_is_404(base_url):
    with pytest.raises(urllib.error.HTTPError) as err:
        get(base_url + "/nope")
    assert err.value.code == 404
