import json
import runpy
import shutil
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_pages.py"


@pytest.fixture
def build():
    return runpy.run_path(str(SCRIPT))["build_pages"]


def read_config(output):
    text = (output / "config.js").read_text(encoding="utf-8")
    return json.loads(text.split("Object.freeze(", 1)[1].removesuffix(");\n"))


def test_demo_artifact_contains_only_public_files(build, tmp_path):
    output = build(tmp_path / "pages")
    assert {p.name for p in output.iterdir()} == {
        "index.html", "style.css", "app.js", "config.js", ".nojekyll",
    }
    assert read_config(output) == {"apiBaseUrl": "", "demoMode": True}
    assert 'id="demo-banner"' in (output / "index.html").read_text(encoding="utf-8")
    assert 'role="status" hidden' not in (output / "index.html").read_text(encoding="utf-8")


def test_extra_source_files_are_never_published(build, tmp_path):
    source = tmp_path / "source"
    shutil.copytree(ROOT / "src/musicstream/static", source)
    (source / ".env").write_text("PRIVATE_TEST_VALUE=must-not-be-published")
    (source / "server.py").write_text("# not a public file")
    build.__globals__["STATIC_DIR"] = source
    output = build(tmp_path / "pages")
    assert not (output / ".env").exists()
    assert not (output / "server.py").exists()
    assert all("must-not-be-published" not in p.read_text(encoding="utf-8") for p in output.iterdir())


class ResourceParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.resources = []
        self.home = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script":
            self.resources.append(attrs["src"])
        if tag == "link" and attrs.get("rel") == "stylesheet":
            self.resources.append(attrs["href"])
        if tag == "a" and attrs.get("class") == "brand":
            self.home = attrs["href"]


def test_resources_resolve_under_repository_base_path(build, tmp_path):
    output = build(tmp_path / "pages")
    app = FastAPI()
    app.mount("/MusicStream", StaticFiles(directory=output, html=True))
    with TestClient(app) as client:
        response = client.get("/MusicStream/")
        assert response.status_code == 200
        parser = ResourceParser()
        parser.feed(response.text)
        assert parser.home == "./"
        assert parser.resources == ["./style.css", "./config.js", "./app.js"]
        for resource in parser.resources:
            path = urlsplit(urljoin("https://example.invalid/MusicStream/", resource)).path
            assert path.startswith("/MusicStream/")
            assert client.get(path).status_code == 200
        assert client.get("/MusicStream/.env").status_code == 404


def test_https_backend_configuration(build, tmp_path):
    output = build(tmp_path / "pages", "https://backend.example.invalid/prefix/")
    assert read_config(output) == {"apiBaseUrl": "https://backend.example.invalid/prefix", "demoMode": False}


@pytest.mark.parametrize("url", [
    "http://backend.example.invalid", "/api", "https://", "https://user:secret@example.invalid",
    "https://backend.example.invalid?token=secret", "https://backend.example.invalid/#secret",
    "https://api.deezer.com", "https://musicbrainz.org", "https://invalid port.example",
    "https://example.invalid:bad", "https://example.invalid\\path",
])
def test_invalid_api_configuration_rejected(build, tmp_path, url):
    with pytest.raises(ValueError):
        build(tmp_path / "pages", url)


def test_existing_output_is_not_overwritten(build, tmp_path):
    output = build(tmp_path / "pages")
    with pytest.raises(ValueError, match="empty"):
        build(output)
    assert (output / "index.html").exists()


def test_builder_cli_works_outside_repository(tmp_path):
    output = tmp_path / "pages"
    result = subprocess.run([sys.executable, str(SCRIPT), "--output", str(output)],
                            cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert read_config(output)["demoMode"] is True


def test_fastapi_keeps_local_config_and_app():
    from musicstream.main import app

    with TestClient(app) as client:
        page = client.get("/app")
        assert page.status_code == 200
        assert 'src="/static/config.js"' in page.text
        response = client.get("/static/config.js")
        assert response.status_code == 200
        assert 'apiBaseUrl: ""' in response.text
        assert "demoMode: false" in response.text
