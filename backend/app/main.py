from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from app.api import router
from app.api.workspace import router as workspace_router
from app.core.config import settings
from app.core.database import Base, SessionLocal, engine
from app.models import Checklist
from app.services.gmail_service import start_gmail_worker, stop_gmail_worker

Base.metadata.create_all(engine)

with SessionLocal() as db:
    if not db.query(Checklist).first():
        db.add(Checklist(name="Inbox", description="Unsorted and quick-capture items", color="#6b7280"))
        db.commit()

@asynccontextmanager
async def lifespan(_: FastAPI):
    start_gmail_worker()
    try:
        yield
    finally:
        stop_gmail_worker()


app = FastAPI(title=settings.app_name, version="2.2.0", docs_url="/api/docs", redoc_url=None, lifespan=lifespan)
app.include_router(router)
app.include_router(workspace_router)

frontend = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if frontend.exists():
    assets = frontend / "assets"
    if assets.exists(): app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        requested = frontend / path
        if path and requested.is_file(): return FileResponse(requested)
        return FileResponse(frontend / "index.html")
else:
    @app.get("/", include_in_schema=False)
    def root(): return {"message": "Frontend not built. Run setup.bat", "docs": "/api/docs"}
