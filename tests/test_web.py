import pytest
from fastapi.testclient import TestClient

from musicstream.main import app


@pytest.mark.parametrize("different_cwd", [False, True])
@pytest.mark.parametrize(("path", "content_type", "marker"), [
    ("/app", "text/html", '<html lang="ru">'),
    ("/static/style.css", "text/css", "prefers-reduced-motion"),
    ("/static/app.js", "javascript", "AbortController"),
])
def test_web_resources(path, content_type, marker, different_cwd, monkeypatch, tmp_path):
    if different_cwd:
        monkeypatch.chdir(tmp_path)
    with TestClient(app) as client:
        response = client.get(path)
    assert response.status_code == 200
    assert content_type in response.headers["content-type"]
    assert marker in response.text


def test_app_links_to_static_resources_and_retains_docs():
    with TestClient(app) as client:
        page = client.get("/app").text
        assert 'href="/static/style.css"' in page
        assert 'src="/static/app.js"' in page
        assert client.get("/docs").status_code == 200
        assert client.get("/static/missing.css").status_code == 404
