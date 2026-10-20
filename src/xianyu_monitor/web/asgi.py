"""ASGI entry point for ``uvicorn xianyu_monitor.web.asgi:app`` (multi-worker safe)."""

from xianyu_monitor.settings import load_settings
from xianyu_monitor.web.app import create_app

app = create_app(load_settings())
