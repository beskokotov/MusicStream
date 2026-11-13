"""Serve the buildless web interface from paths relative to this module."""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

STATIC_DIR = Path(__file__).resolve().parent / "static"
router = APIRouter()


@router.get("/app", response_class=FileResponse, include_in_schema=False)
async def web_app():
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")
