"""ggbot.web：控制面板（FastAPI 后端 + 静态前端）。"""
from .server import WEB_DIR, create_app, start_web

__all__ = ["create_app", "start_web", "WEB_DIR"]
