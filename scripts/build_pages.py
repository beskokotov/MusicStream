"""Build an allowlisted GitHub Pages artifact with Python's standard library."""

import argparse
import json
import shutil
from pathlib import Path
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = PROJECT_ROOT / "src" / "musicstream" / "static"


def validate_api_base_url(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value:
        return ""
    url = urlsplit(value)
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.query or url.fragment or "?" in value or "#" in value
            or any(c.isspace() or ord(c) < 32 for c in value) or "\\" in value
            or url.hostname in {"api.deezer.com", "musicbrainz.org"}):
        raise ValueError("API base URL must be a public HTTPS backend URL without credentials, query or fragment")
    # Accessing port also validates malformed port numbers.
    _ = url.port
    return value


def build_pages(output_dir: Path, api_base_url: str = "") -> Path:
    api_base_url = validate_api_base_url(api_base_url)
    output_dir = output_dir.resolve()
    # Never clean arbitrary directories or copy the whole repository.
    output_dir.mkdir(parents=True, exist_ok=True)
    if any(output_dir.iterdir()):
        raise ValueError("Pages output directory must be empty; choose a new directory")
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for filename in ("style.css", "config.js", "app.js"):
        html = html.replace(f'"/static/{filename}"', f'"./{filename}"')
    html = html.replace('href="/app"', 'href="./"')
    if not api_base_url:
        # Keep the demo notice visible even when JavaScript is disabled.
        html = html.replace('id="demo-banner" class="demo-banner" role="status" hidden',
                            'id="demo-banner" class="demo-banner" role="status"')
    (output_dir / "index.html").write_text(html, encoding="utf-8")
    for filename in ("style.css", "app.js"):
        shutil.copyfile(STATIC_DIR / filename, output_dir / filename)
    config = {"apiBaseUrl": api_base_url, "demoMode": not bool(api_base_url)}
    (output_dir / "config.js").write_text(
        "// Public frontend configuration. No secrets.\n"
        f"window.MUSICSTREAM_CONFIG = Object.freeze({json.dumps(config)});\n",
        encoding="utf-8",
    )
    (output_dir / ".nojekyll").touch()
    return output_dir


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "dist" / "pages")
    parser.add_argument("--api-base-url", default="", help="Public HTTPS backend base URL; empty means demo mode")
    args = parser.parse_args()
    build_pages(args.output, args.api_base_url)
    print(f"Pages artifact ready: {args.output} ({'API' if args.api_base_url else 'demo'} mode)")
