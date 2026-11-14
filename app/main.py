"""
FastAPI application entry point.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.services.image_service import ImageGeneratorService
import config

# ─── Logging ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


# ─── Lifespan (startup / shutdown) ───────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the image service when the app boots; shut it down on exit."""
    service = ImageGeneratorService(provider="google_flow")
    app.state.image_service = service
    await service.startup()
    logger.info("✅ Image generation service is ready.")
    yield
    logger.info("Shutting down …")
    await service.shutdown()


# ─── App ─────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Image Generation Service",
    description=(
        "Accepts one or more text prompts and returns generated images. "
        "Currently backed by flow.google.com via browser automation."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

# CORS middleware for other web apps / frontends to call freely
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static file serving: cho phép các ứng dụng khác tải trực tiếp ảnh qua URL HTTP
app.mount("/outputs", StaticFiles(directory=str(config.OUTPUT_DIR)), name="outputs")

# Giao diện Cài đặt (Settings UI)
STATIC_DIR = config.OUTPUT_DIR.parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    @app.get("/settings", include_in_schema=False)
    async def serve_settings_page():
        return FileResponse(str(STATIC_DIR / "index.html"))

app.include_router(router, prefix="/api/v1")


# ─── Dev runner ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=config.API_HOST,
        port=config.API_PORT,
        reload=False,       # reload=True breaks Playwright; keep False
    )
