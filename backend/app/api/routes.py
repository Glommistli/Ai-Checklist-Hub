from __future__ import annotations
import hashlib
import json
import os
import shutil
from html import escape
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated
from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool
import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload
from app.core.database import get_db
from app.core.config import settings
from app.core.secrets import delete_secret, get_secret, provider_secret_name, set_secret
from app.models import AppPreference, Category, Checklist, EmailIngestion, ImportBatch, ImportDocument, Item, Plan, ProviderConfig
from app.schemas import (
    ActorAnalyzeRequest, GmailCandidateOut, GmailClientRequest, GmailSettingsUpdate,
    ImportConfirmRequest, ImportParseRequest, ItemCreate, ItemOut, ItemUpdate,
    ChunkedUploadInit, ImportBatchOut, PlanningConfirmRequest, PlanningOptions, ParsedItem, ParsedList, ParsedPlan,
    ListCreate, ListOut, ListUpdate, PlanCreate, PlanOut, PlanUpdate, ProviderCreate, UISettings,
)
from app.services.parser import LLMProviderError, OpenAICompatibleProvider, analyze_actors, parse_content, provider_api_key
from app.services.agent_parser import AgentOptions
from app.services.repository import create_item, update_item, delete_item_tree
from app.services.importer import confirm_parsed_plan
from app.services.document_import import (
    MAX_FILES, MAX_FILE_BYTES, MAX_TOTAL_BYTES, MEDIA_EXTENSIONS, SUPPORTED_EXTENSIONS,
    IgnoredRelativePathError, extract_document, extract_document_path, safe_relative_path,
    sha256_bytes, sha256_path,
)
from app.services.planning import analyze_learning_units, build_schedule
from app.services.gmail_service import (
    GmailError, accept_candidate, begin_oauth, client_configured, disconnect_gmail,
    finish_oauth, gmail_state, save_gmail_state, save_oauth_client, sync_gmail,
    token_configured,
)

router = APIRouter(prefix="/api")
Db = Annotated[Session, Depends(get_db)]


def get_or_404(db: Session, model, obj_id: int):
    obj = db.get(model, obj_id)
    if not obj: raise HTTPException(404, f"{model.__name__} not found")
    return obj


@router.get("/settings/ui", response_model=UISettings)
def get_ui_settings(db: Db):
    preference = db.get(AppPreference, "ui")
    return UISettings.model_validate(preference.value if preference else {})


@router.put("/settings/ui", response_model=UISettings)
def update_ui_settings(data: UISettings, db: Db):
    preference = db.get(AppPreference, "ui")
    if preference:
        preference.value = data.model_dump()
    else:
        db.add(AppPreference(key="ui", value=data.model_dump()))
    db.commit()
    return data


@router.get("/health")
def health(): return {"status": "ok", "time": datetime.utcnow()}


@router.get("/integrations/gmail/status")
def gmail_status(db: Db):
    state = gmail_state(db)
    pending = db.scalar(select(func.count(EmailIngestion.id)).where(EmailIngestion.status == "candidate")) or 0
    processed = db.scalar(select(func.count(EmailIngestion.id))) or 0
    return {
        **state,
        "client_configured": client_configured(),
        "connected": bool(state.get("connected") and token_configured()),
        "candidate_count": pending,
        "processed_count": processed,
    }


@router.post("/integrations/gmail/client")
def configure_gmail_client(data: GmailClientRequest):
    try:
        save_oauth_client(data.client_id.strip(), data.client_secret.strip())
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


@router.put("/integrations/gmail/settings")
def update_gmail_settings(data: GmailSettingsUpdate, db: Db):
    return save_gmail_state(db, data.model_dump())


@router.post("/integrations/gmail/oauth/start")
def start_gmail_oauth(request: Request, db: Db):
    redirect_uri = str(request.url_for("gmail_oauth_callback"))
    try:
        return {"authorization_url": begin_oauth(db, redirect_uri)}
    except GmailError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/integrations/gmail/oauth/callback", response_class=HTMLResponse, name="gmail_oauth_callback")
def gmail_oauth_callback(db: Db, code: str | None = None, state: str | None = None, error: str | None = None):
    if error:
        return HTMLResponse(f"<h2>Gmail 授权未完成</h2><p>{escape(error)}</p>", status_code=400)
    if not code or not state:
        return HTMLResponse("<h2>Gmail 授权参数不完整</h2>", status_code=400)
    try:
        result = finish_oauth(db, code, state)
    except GmailError as exc:
        return HTMLResponse(f"<h2>Gmail 连接失败</h2><p>{escape(str(exc))}</p>", status_code=400)
    email = escape(str(result.get("email") or "Gmail"))
    return HTMLResponse(
        f"""<!doctype html><meta charset="utf-8"><title>Gmail 已连接</title>
        <style>body{{font:16px system-ui;padding:48px;max-width:600px;margin:auto}}small{{color:#777}}</style>
        <h2>Gmail 已连接</h2><p>{email}</p><small>现在可以关闭此窗口，返回 AI Checklist Hub。</small>
        <script>if(window.opener){{window.opener.postMessage({{type:'gmail-connected'}},location.origin);setTimeout(()=>window.close(),900)}}</script>"""
    )


@router.post("/integrations/gmail/sync")
def run_gmail_sync(db: Db):
    try:
        return sync_gmail(db)
    except GmailError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/integrations/gmail/candidates", response_model=list[GmailCandidateOut])
def gmail_candidates(db: Db, limit: int = Query(100, ge=1, le=500)):
    return list(db.scalars(
        select(EmailIngestion).where(EmailIngestion.status == "candidate")
        .order_by(EmailIngestion.received_at.desc(), EmailIngestion.id.desc()).limit(limit)
    ))


@router.post("/integrations/gmail/candidates/{candidate_id}/accept")
def accept_gmail_candidate(candidate_id: int, db: Db):
    candidate = get_or_404(db, EmailIngestion, candidate_id)
    try:
        return accept_candidate(db, candidate)
    except GmailError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/integrations/gmail/candidates/{candidate_id}/ignore")
def ignore_gmail_candidate(candidate_id: int, db: Db):
    candidate = get_or_404(db, EmailIngestion, candidate_id)
    if candidate.status != "candidate":
        raise HTTPException(400, "这封邮件已处理")
    candidate.status = "ignored"
    candidate.reason = "用户在候选任务中选择忽略"
    candidate.processed_at = datetime.utcnow()
    db.commit()
    return {"ok": True}


@router.delete("/integrations/gmail")
def remove_gmail_connection(db: Db):
    disconnect_gmail(db)


@router.get("/items", response_model=list[ItemOut])
def list_items(
    db: Db, completed: bool | None = None, list_id: int | None = None,
    category_id: int | None = None, plan_id: int | None = None, priority: str | None = None,
    tag: str | None = None, due_from: date | None = None, due_to: date | None = None,
    q: str | None = Query(default=None, max_length=200), limit: int = Query(500, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    stmt = (
        select(Item).options(selectinload(Item.tags))
        .order_by(Item.completed, Item.position, Item.created_at.desc(), Item.id.desc())
        .offset(offset).limit(limit)
    )
    if completed is not None: stmt = stmt.where(Item.completed == completed)
    if list_id is not None: stmt = stmt.where(Item.list_id == list_id)
    if category_id is not None: stmt = stmt.where(Item.category_id == category_id)
    if plan_id is not None: stmt = stmt.where(Item.plan_id == plan_id)
    if priority: stmt = stmt.where(Item.priority == priority)
    if due_from: stmt = stmt.where(Item.due_date >= due_from)
    if due_to: stmt = stmt.where(Item.due_date <= due_to)
    if tag: stmt = stmt.where(Item.tags.any(name=tag.lower()))
    if q:
        term = f"%{q.strip()}%"
        stmt = stmt.where(or_(Item.title.ilike(term), Item.description.ilike(term), Item.notes.ilike(term), Item.tags.any(name=q.lower())))
    return list(db.scalars(stmt).unique())


@router.post("/items", response_model=ItemOut, status_code=201)
def add_item(data: ItemCreate, db: Db):
    lst = db.get(Checklist, data.list_id) if data.list_id else None
    plan = db.get(Plan, data.plan_id) if data.plan_id else (lst.plan if lst else None)
    return create_item(db, data, lst.name if lst else None, plan.title if plan else None)


@router.put("/items/{item_id}", response_model=ItemOut)
def edit_item(item_id: int, data: ItemUpdate, db: Db): return update_item(db, get_or_404(db, Item, item_id), data)


@router.delete("/items/{item_id}", status_code=204)
def delete_item(item_id: int, db: Db): delete_item_tree(db, get_or_404(db, Item, item_id))


@router.post("/items/{item_id}/complete", response_model=ItemOut)
def toggle_complete(item_id: int, db: Db):
    item = get_or_404(db, Item, item_id)
    return update_item(db, item, ItemUpdate(completed=not item.completed))


@router.get("/lists", response_model=list[ListOut])
def list_lists(db: Db):
    lists = list(db.scalars(select(Checklist).order_by(Checklist.position, Checklist.created_at)))
    result = []
    for lst in lists:
        data = ListOut.model_validate(lst)
        data.item_count = db.scalar(select(func.count(Item.id)).where(Item.list_id == lst.id)) or 0
        data.pending_count = db.scalar(select(func.count(Item.id)).where(Item.list_id == lst.id, Item.completed.is_(False))) or 0
        result.append(data)
    return result


@router.post("/lists", response_model=ListOut, status_code=201)
def add_list(data: ListCreate, db: Db):
    obj = Checklist(**data.model_dump(exclude={"metadata"}), metadata_=data.metadata)
    db.add(obj); db.commit(); db.refresh(obj)
    return ListOut.model_validate(obj)


@router.put("/lists/{list_id}", response_model=ListOut)
def edit_list(list_id: int, data: ListUpdate, db: Db):
    obj = get_or_404(db, Checklist, list_id)
    for k, v in data.model_dump(exclude_unset=True, exclude={"metadata"}).items(): setattr(obj, k, v)
    if data.metadata is not None: obj.metadata_ = data.metadata
    db.commit(); db.refresh(obj)
    return ListOut.model_validate(obj)


@router.delete("/lists/{list_id}", status_code=204)
def delete_list(list_id: int, db: Db): db.delete(get_or_404(db, Checklist, list_id)); db.commit()


@router.get("/plans", response_model=list[PlanOut])
def list_plans(db: Db): return list(db.scalars(select(Plan).order_by(Plan.created_at.desc())))


@router.post("/plans", response_model=PlanOut, status_code=201)
def add_plan(data: PlanCreate, db: Db):
    obj = Plan(**data.model_dump(exclude={"metadata"}), metadata_=data.metadata)
    db.add(obj); db.commit(); db.refresh(obj); return obj


@router.put("/plans/{plan_id}", response_model=PlanOut)
def edit_plan(plan_id: int, data: PlanUpdate, db: Db):
    obj = get_or_404(db, Plan, plan_id)
    for k, v in data.model_dump(exclude_unset=True, exclude={"metadata"}).items(): setattr(obj, k, v)
    if data.metadata is not None: obj.metadata_ = data.metadata
    db.commit(); db.refresh(obj); return obj


@router.delete("/plans/{plan_id}", status_code=204)
def delete_plan(plan_id: int, db: Db): db.delete(get_or_404(db, Plan, plan_id)); db.commit()


@router.get("/categories")
def list_categories(db: Db): return list(db.scalars(select(Category).order_by(Category.name)))


@router.post("/import/parse")
def parse_import(data: ImportParseRequest, db: Db):
    mode = data.llm_mode or ("auto" if data.use_llm else "off")
    options = AgentOptions(
        granularity=data.granularity,
        max_top_level_tasks=data.max_top_level_tasks,
        preserve_source_structure=data.preserve_source_structure,
        steps_as_subtasks=data.steps_as_subtasks,
        user_context=data.user_context,
        only_user_actions=data.only_user_actions,
    )
    try:
        return parse_content(data.content, db, use_llm=mode != "off", force_llm=mode == "force", agent_options=options)
    except LLMProviderError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/import/actors")
def analyze_import_actors(data: ActorAnalyzeRequest, db: Db):
    try:
        return analyze_actors(data.content, db)
    except LLMProviderError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/import/file")
async def parse_file(db: Db, file: UploadFile = File(...)):
    if Path(file.filename or "").suffix.lower() not in {".txt", ".md", ".json"}: raise HTTPException(400, "Only TXT, Markdown and JSON files are supported")
    content = (await file.read()).decode("utf-8-sig")
    return parse_content(content, db)


def _batch_payload(batch: ImportBatch, db: Session) -> dict:
    documents = list(db.scalars(
        select(ImportDocument).where(ImportDocument.batch_id == batch.id).order_by(ImportDocument.sequence_hint)
    ))
    return {
        "id": batch.id, "source_kind": batch.source_kind, "root_name": batch.root_name,
        "status": batch.status, "file_count": batch.file_count, "supported_count": batch.supported_count,
        "skipped_count": batch.skipped_count, "error_count": batch.error_count,
        "total_size": batch.total_size, "total_media_seconds": batch.total_media_seconds,
        "documents": documents,
    }


LARGE_IMPORT_MAX_BYTES = 256 * 1024 * 1024 * 1024
UPLOAD_CHUNK_MAX_BYTES = 16 * 1024 * 1024


def _upload_root() -> Path:
    local = os.getenv("LOCALAPPDATA")
    root = Path(local) / "AI Checklist Hub" / "upload-cache" if local else Path(settings.database_url.removeprefix("sqlite:///" )).resolve().parent / "upload-cache"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _batch_upload_dir(batch_id: int) -> Path:
    return _upload_root() / str(batch_id)


def _upload_part_path(batch_id: int, document_id: int) -> Path:
    return _batch_upload_dir(batch_id) / f"{document_id}.part"


def _remove_upload_cache(batch_id: int) -> None:
    shutil.rmtree(_batch_upload_dir(batch_id), ignore_errors=True)


@router.post("/import/uploads", response_model=ImportBatchOut, status_code=201)
def start_chunked_upload(data: ChunkedUploadInit, db: Db):
    """Create a resumable, bounded-memory import session.

    Media with browser-readable duration is indexed by metadata and never copied.
    Other supported files are marked for fixed-size chunk transfer.
    """
    prepared_files = []
    for index, file in enumerate(data.files):
        try:
            relative_path = safe_relative_path(file.relative_path)
            ignored = False
        except IgnoredRelativePathError as exc:
            relative_path = exc.relative_path
            ignored = True
        except ValueError as exc:
            raise HTTPException(400, f"第 {index + 1} 个文件路径无效：{exc}") from exc
        prepared_files.append((index, file, relative_path, ignored))

    total_size = sum(file.size for _, file, _, _ in prepared_files)
    if total_size > LARGE_IMPORT_MAX_BYTES:
        raise HTTPException(413, "单次导入资料总大小不能超过 256 GB")
    transfer_size = sum(
        file.size for _, file, relative_path, ignored in prepared_files
        if not ignored
        and Path(relative_path).suffix.lower() in SUPPORTED_EXTENSIONS
        and not (Path(relative_path).suffix.lower() in MEDIA_EXTENSIONS and file.duration_seconds)
    )
    free_space = shutil.disk_usage(_upload_root()).free
    if transfer_size + 512 * 1024 * 1024 > free_space:
        raise HTTPException(507, f"临时空间不足：需约 {transfer_size / 1024**3:.1f} GB，并保留 0.5 GB 安全空间")
    batch = ImportBatch(
        source_kind=data.source_kind, root_name=data.root_name.strip() or "导入资料",
        status="uploading", file_count=len(data.files), total_size=total_size,
    )
    db.add(batch); db.flush()
    seen_paths: set[str] = set()
    supported = skipped = errors = media_seconds = 0
    for index, file, relative_path, ignored in prepared_files:
        suffix = Path(relative_path).suffix.lower()
        if ignored:
            skipped += 1
            db.add(ImportDocument(
                batch_id=batch.id, relative_path=relative_path, filename=Path(relative_path).name,
                extension=suffix, mime_type=file.mime_type or None, size=file.size,
                sha256=hashlib.sha256(f"ignored\0{relative_path}\0{file.size}".encode()).hexdigest(),
                extraction_status="skipped", extracted_text="", duration_seconds=None,
                sequence_hint=index, error_message="已自动跳过隐藏目录或开发目录中的文件",
                metadata_={"upload_required": False, "ignored_path": True},
            ))
            continue
        key = relative_path.casefold()
        if key in seen_paths:
            db.rollback()
            raise HTTPException(409, f"目录中存在重复路径：{relative_path}")
        seen_paths.add(key)
        upload_required = suffix in SUPPORTED_EXTENSIONS
        status, error, duration = "pending", None, file.duration_seconds
        metadata = {
            "upload_required": upload_required, "received": 0,
            "declared_size": file.size, "last_modified": file.last_modified,
        }
        if suffix not in SUPPORTED_EXTENSIONS:
            status, error, upload_required = "skipped", f"暂不支持 {suffix or '无扩展名'} 文件", False
            metadata["upload_required"] = False
            skipped += 1
        elif suffix in MEDIA_EXTENSIONS and duration:
            status, upload_required = "ready", False
            metadata.update({"upload_required": False, "metadata_only": True, "media_type": "audio" if suffix in {".mp3", ".m4a", ".wav", ".flac", ".aac", ".ogg"} else "video"})
            supported += 1
            media_seconds += duration
        db.add(ImportDocument(
            batch_id=batch.id, relative_path=relative_path, filename=Path(relative_path).name,
            extension=suffix, mime_type=file.mime_type or None, size=file.size,
            sha256=hashlib.sha256(f"{relative_path}\0{file.size}\0{file.last_modified or 0}".encode()).hexdigest() if not upload_required else "",
            extraction_status=status, extracted_text="", duration_seconds=duration,
            sequence_hint=index, error_message=error, metadata_=metadata,
        ))
    batch.supported_count, batch.skipped_count, batch.error_count = supported, skipped, errors
    batch.total_media_seconds = media_seconds
    db.commit(); db.refresh(batch)
    return _batch_payload(batch, db)


@router.put("/import/uploads/{batch_id}/{document_id}/chunk")
async def upload_chunk(batch_id: int, document_id: int, request: Request, db: Db, offset: int = Query(ge=0)):
    batch = get_or_404(db, ImportBatch, batch_id)
    document = get_or_404(db, ImportDocument, document_id)
    if document.batch_id != batch.id:
        raise HTTPException(404, "上传文件不存在")
    metadata = dict(document.metadata_ or {})
    if not metadata.get("upload_required") or document.extraction_status != "pending":
        raise HTTPException(409, "该文件不需要上传或已经处理")
    part_path = _upload_part_path(batch.id, document.id)
    part_path.parent.mkdir(parents=True, exist_ok=True)
    received = part_path.stat().st_size if part_path.exists() else 0
    content_length = request.headers.get("content-length")
    declared_chunk_size = int(content_length) if content_length else None
    if offset < received and declared_chunk_size and offset + declared_chunk_size <= received:
        return {"received": received, "size": int(metadata.get("declared_size", document.size)), "replayed": True}
    if offset != received:
        raise HTTPException(409, f"上传偏移不一致，服务端已接收 {received} 字节")
    declared_size = int(metadata.get("declared_size", document.size))
    if received >= declared_size:
        raise HTTPException(409, "文件内容已经上传完毕")
    if declared_chunk_size and declared_chunk_size > UPLOAD_CHUNK_MAX_BYTES:
        raise HTTPException(413, "单个分块不能超过 16 MB")
    chunk_path = part_path.with_name(f"{document.id}.{offset}.chunk")
    chunk_size = 0
    try:
        with chunk_path.open("wb") as chunk_file:
            async for chunk in request.stream():
                chunk_size += len(chunk)
                if chunk_size > UPLOAD_CHUNK_MAX_BYTES:
                    raise HTTPException(413, "单个分块不能超过 16 MB")
                chunk_file.write(chunk)
        if chunk_size <= 0 or received + chunk_size > declared_size:
            raise HTTPException(400, "上传分块为空或超过声明的文件大小")
        with part_path.open("ab") as destination, chunk_path.open("rb") as source:
            shutil.copyfileobj(source, destination, length=1024 * 1024)
    finally:
        chunk_path.unlink(missing_ok=True)
    received += chunk_size
    metadata["received"] = received
    document.metadata_ = metadata
    db.commit()
    return {"received": received, "size": declared_size}


@router.post("/import/uploads/{batch_id}/{document_id}/complete")
async def complete_uploaded_document(batch_id: int, document_id: int, db: Db):
    batch = get_or_404(db, ImportBatch, batch_id)
    document = get_or_404(db, ImportDocument, document_id)
    if document.batch_id != batch.id:
        raise HTTPException(404, "上传文件不存在")
    metadata = dict(document.metadata_ or {})
    part_path = _upload_part_path(batch.id, document.id)
    expected = int(metadata.get("declared_size", document.size))
    actual = part_path.stat().st_size if part_path.exists() else 0
    if actual != expected:
        raise HTTPException(409, f"文件尚未完整上传：{actual} / {expected} 字节")
    try:
        digest = await run_in_threadpool(sha256_path, part_path)
        extracted = await run_in_threadpool(extract_document_path, document.relative_path, part_path)
        document.sha256 = digest
        document.extraction_status = extracted.status
        document.extracted_text = extracted.text
        document.page_count = extracted.page_count
        document.slide_count = extracted.slide_count
        document.sheet_count = extracted.sheet_count
        document.duration_seconds = extracted.duration_seconds
        document.error_message = extracted.error
        metadata.update(extracted.metadata)
        metadata.update({"upload_required": False, "received": actual})
        document.metadata_ = metadata
        db.commit()
    finally:
        part_path.unlink(missing_ok=True)
    return {"id": document.id, "status": document.extraction_status}


@router.post("/import/uploads/{batch_id}/complete", response_model=ImportBatchOut)
def complete_chunked_upload(batch_id: int, db: Db):
    batch = get_or_404(db, ImportBatch, batch_id)
    documents = list(db.scalars(select(ImportDocument).where(ImportDocument.batch_id == batch.id)))
    pending = [doc for doc in documents if doc.extraction_status == "pending"]
    if pending:
        raise HTTPException(409, f"仍有 {len(pending)} 个文件尚未处理完成")
    batch.supported_count = sum(doc.extraction_status in {"ready", "warning"} for doc in documents)
    batch.skipped_count = sum(doc.extraction_status == "skipped" for doc in documents)
    batch.error_count = sum(doc.extraction_status == "error" for doc in documents)
    batch.total_media_seconds = sum(doc.duration_seconds or 0 for doc in documents)
    batch.content_hash = hashlib.sha256("".join(doc.sha256 for doc in documents).encode()).hexdigest()
    batch.status = "ready" if batch.supported_count else "failed"
    db.commit(); db.refresh(batch)
    _remove_upload_cache(batch.id)
    return _batch_payload(batch, db)


@router.post("/import/batches", response_model=ImportBatchOut)
async def create_import_batch(
    db: Db,
    files: list[UploadFile] = File(...),
    root_name: str = Form("导入资料"),
    source_kind: str = Form("files"),
    relative_paths: str = Form("[]"),
):
    if not files or len(files) > MAX_FILES:
        raise HTTPException(400, f"一次必须选择 1～{MAX_FILES} 个文件")
    try:
        paths = json.loads(relative_paths)
        if not isinstance(paths, list):
            raise ValueError
    except (ValueError, json.JSONDecodeError):
        raise HTTPException(400, "relative_paths 必须是 JSON 数组")
    if paths and len(paths) != len(files):
        raise HTTPException(400, "文件和相对路径数量不一致")
    batch = ImportBatch(source_kind=source_kind[:30], root_name=(root_name.strip() or "导入资料")[:500], status="extracting")
    db.add(batch); db.flush()
    total_size = supported = skipped = errors = media_seconds = 0
    hashes = []
    for index, upload in enumerate(files):
        raw_path = str(paths[index]) if paths else (upload.filename or f"file-{index + 1}")
        try:
            relative_path = safe_relative_path(raw_path)
        except ValueError as exc:
            skipped += 1
            continue
        data = await upload.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            extracted_status, extracted_error = "error", "文件超过 100 MB 限制"
            extracted = None
        else:
            extracted = extract_document(relative_path, data)
            extracted_status, extracted_error = extracted.status, extracted.error
        total_size += len(data)
        if total_size > MAX_TOTAL_BYTES:
            db.rollback()
            raise HTTPException(413, "一次导入的文件总大小不能超过 2 GB")
        digest = sha256_bytes(data)
        hashes.append(digest)
        if extracted_status == "skipped": skipped += 1
        elif extracted_status == "error": errors += 1
        else: supported += 1
        if extracted and extracted.duration_seconds:
            media_seconds += extracted.duration_seconds
        db.add(ImportDocument(
            batch_id=batch.id, relative_path=relative_path, filename=Path(relative_path).name,
            extension=Path(relative_path).suffix.lower(), mime_type=upload.content_type, size=len(data), sha256=digest,
            extraction_status=extracted_status, extracted_text=extracted.text if extracted else "",
            page_count=extracted.page_count if extracted else None, slide_count=extracted.slide_count if extracted else None,
            sheet_count=extracted.sheet_count if extracted else None, duration_seconds=extracted.duration_seconds if extracted else None,
            sequence_hint=index, error_message=extracted_error, metadata_=extracted.metadata if extracted else {},
        ))
    import hashlib
    batch.file_count, batch.supported_count, batch.skipped_count, batch.error_count = len(files), supported, skipped, errors
    batch.total_size, batch.total_media_seconds = total_size, media_seconds
    batch.content_hash = hashlib.sha256("".join(hashes).encode()).hexdigest()
    batch.status = "ready" if supported else "failed"
    db.commit(); db.refresh(batch)
    return _batch_payload(batch, db)


@router.get("/import/batches/{batch_id}", response_model=ImportBatchOut)
def get_import_batch(batch_id: int, db: Db):
    return _batch_payload(get_or_404(db, ImportBatch, batch_id), db)


@router.delete("/import/batches/{batch_id}", status_code=204)
def delete_import_batch(batch_id: int, db: Db):
    db.delete(get_or_404(db, ImportBatch, batch_id)); db.commit()
    _remove_upload_cache(batch_id)


@router.post("/planning/schedule")
def create_schedule(data: PlanningOptions, db: Db):
    try:
        units, provider, model = analyze_learning_units(db, data)
        schedule = build_schedule(units, data, provider, model)
        batch = get_or_404(db, ImportBatch, data.batch_id)
        batch.status = "planned"; db.commit()
        return schedule
    except LLMProviderError as exc:
        raise HTTPException(400, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/planning/confirm")
def confirm_schedule(data: PlanningConfirmRequest, db: Db):
    batch = get_or_404(db, ImportBatch, data.batch_id)
    task_list = ParsedList(name=data.schedule.title, description=data.schedule.goal, items=[])
    for index, day in enumerate(data.schedule.days):
        children = []
        for unit in day.units:
            source_bits = [unit.source_path]
            if unit.page_from:
                source_bits.append(f"第 {unit.page_from}～{unit.page_to or unit.page_from} 页")
            if unit.media_start is not None:
                source_bits.append(f"{unit.media_start // 60:02d}:{unit.media_start % 60:02d}～{(unit.media_end or 0) // 60:02d}:{(unit.media_end or 0) % 60:02d}")
            children.append(ParsedItem(
                title=unit.title, description=unit.summary or " · ".join(source_bits), start_date=day.date,
                due_date=day.date, estimated_minutes=unit.estimated_minutes, tags=[unit.topic],
                metadata={"learning_unit_id": unit.id, "source_document_id": unit.document_id,
                    "source_path": unit.source_path, "page_from": unit.page_from, "page_to": unit.page_to,
                    "media_start": unit.media_start, "media_end": unit.media_end, "unit_type": unit.unit_type},
            ))
        task_list.items.append(ParsedItem(
            title=f"第 {index + 1} 天：{day.title}", description=f"预计 {day.estimated_minutes} 分钟",
            start_date=day.date, due_date=day.date, estimated_minutes=day.estimated_minutes,
            subtasks=children, tags=["智能规划"], metadata={"planned_day": index + 1, "capacity_minutes": day.capacity_minutes},
        ))
    parsed = ParsedPlan(
        title=data.schedule.title, goal=data.schedule.goal, description=f"{data.schedule.day_count} 天智能规划",
        content_type="scheduled_plan", date=data.schedule.start_date, lists=[task_list], confidence=0.9,
        parser="planner_v2", provider=data.schedule.provider, model=data.schedule.model, granularity="detailed",
        task_count=len(task_list.items), step_count=sum(len(item.subtasks) for item in task_list.items),
        warnings=data.schedule.warnings,
    )
    result = confirm_parsed_plan(ImportConfirmRequest(
        plan=parsed, source_name=f"Directory:{batch.root_name}", raw_content=f"导入批次 {batch.id}",
        duplicate_strategy=data.duplicate_strategy, external_id=f"import-batch:{batch.id}",
        source_metadata={"import_batch_id": batch.id, "planner": "v2", "provider": data.schedule.provider, "model": data.schedule.model},
    ), db)
    batch.status = "confirmed"; db.commit()
    return result


@router.post("/import/confirm")
def confirm_import(data: ImportConfirmRequest, db: Db):
    return confirm_parsed_plan(data, db)


@router.post("/webhooks/import")
def webhook_import(data: ImportParseRequest, db: Db, x_webhook_token: str | None = Header(default=None)):
    """Generic machine-to-machine ingestion endpoint for automations and future adapters."""
    if settings.webhook_token and x_webhook_token != settings.webhook_token:
        raise HTTPException(401, "Invalid webhook token")
    mode = data.llm_mode or ("auto" if data.use_llm else "off")
    options = AgentOptions(
        granularity=data.granularity,
        max_top_level_tasks=data.max_top_level_tasks,
        preserve_source_structure=data.preserve_source_structure,
        steps_as_subtasks=data.steps_as_subtasks,
        user_context=data.user_context,
        only_user_actions=data.only_user_actions,
    )
    try:
        parsed = parse_content(data.content, db, use_llm=mode != "off", force_llm=mode == "force", agent_options=options)
    except LLMProviderError as exc:
        raise HTTPException(400, str(exc)) from exc
    return confirm_import(ImportConfirmRequest(
        plan=parsed, source_name=data.source_name, raw_content=data.content, duplicate_strategy="skip"
    ), db)


@router.get("/stats")
def stats(db: Db):
    today = date.today(); week_end = today + timedelta(days=6-today.weekday())
    def bucket(start, end):
        total = db.scalar(select(func.count(Item.id)).where(Item.due_date >= start, Item.due_date <= end)) or 0
        done = db.scalar(select(func.count(Item.id)).where(Item.due_date >= start, Item.due_date <= end, Item.completed.is_(True))) or 0
        return {"total": total, "completed": done, "percent": round(done / total * 100) if total else 0}
    return {"today": bucket(today, today), "week": bucket(today - timedelta(days=today.weekday()), week_end),
        "all": {"total": db.scalar(select(func.count(Item.id))) or 0, "completed": db.scalar(select(func.count(Item.id)).where(Item.completed.is_(True))) or 0}}


@router.get("/settings/providers")
def providers(db: Db):
    return [{"id": x.id, "name": x.name, "provider_type": x.provider_type, "base_url": x.base_url, "model": x.model,
        "temperature": x.temperature, "timeout": x.timeout, "api_key_env": x.api_key_env, "enabled": x.enabled,
        "has_api_key": bool(get_secret(provider_secret_name(x.id)) or os.getenv(x.api_key_env))}
        for x in db.scalars(select(ProviderConfig).order_by(ProviderConfig.name))]


@router.post("/settings/providers", status_code=201)
def add_provider(data: ProviderCreate, db: Db):
    duplicate = db.scalar(select(ProviderConfig).where(func.lower(ProviderConfig.name) == data.name.lower()))
    if duplicate:
        raise HTTPException(409, f'已存在名为“{duplicate.name}”的 Provider，请直接在上方更新其 API Key 或配置')
    allowed = data.model_dump(exclude={"api_key"})
    if data.enabled:
        for configured in db.scalars(select(ProviderConfig).where(ProviderConfig.enabled.is_(True))): configured.enabled = False
    obj = ProviderConfig(**allowed); db.add(obj)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, f'已存在名为“{data.name}”的 Provider') from exc
    secret_name = provider_secret_name(obj.id)
    try:
        if data.api_key: set_secret(secret_name, data.api_key)
        db.commit(); db.refresh(obj)
    except (OSError, RuntimeError) as exc:
        db.rollback()
        raise HTTPException(500, f"无法在本机安全保存 API Key：{exc}") from exc
    return {"id": obj.id, **allowed, "has_api_key": bool(data.api_key or os.getenv(obj.api_key_env))}


@router.put("/settings/providers/{provider_id}")
def update_provider(provider_id: int, data: dict, db: Db):
    obj = get_or_404(db, ProviderConfig, provider_id)
    if data.get("enabled"):
        for configured in db.scalars(select(ProviderConfig).where(ProviderConfig.enabled.is_(True), ProviderConfig.id != provider_id)): configured.enabled = False
    for key in ("name", "provider_type", "base_url", "model", "temperature", "timeout", "api_key_env", "enabled"):
        if key in data: setattr(obj, key, data[key])
    if not obj.base_url.startswith(("https://", "http://")): raise HTTPException(422, "base_url must start with http:// or https://")
    secret_name = provider_secret_name(obj.id)
    api_key = str(data.get("api_key", "")).strip()
    if api_key: set_secret(secret_name, api_key)
    if data.get("clear_api_key"): delete_secret(secret_name)
    db.commit(); db.refresh(obj)
    return {"id": obj.id, "has_api_key": bool(get_secret(secret_name) or os.getenv(obj.api_key_env))}


@router.delete("/settings/providers/{provider_id}", status_code=204)
def delete_provider(provider_id: int, db: Db):
    obj = get_or_404(db, ProviderConfig, provider_id)
    delete_secret(provider_secret_name(obj.id))
    db.delete(obj); db.commit()


@router.post("/settings/providers/{provider_id}/test")
def test_provider(provider_id: int, db: Db):
    obj = get_or_404(db, ProviderConfig, provider_id)
    key = provider_api_key(obj)
    if not key: raise HTTPException(400, "请先输入并保存 API Key")
    try:
        OpenAICompatibleProvider(obj.base_url, key, obj.model, obj.temperature, obj.timeout).test_connection()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(400, f"服务返回 HTTP {exc.response.status_code}，请检查 API Key、余额、地址和模型名称") from exc
    except httpx.RequestError as exc:
        raise HTTPException(400, f"无法连接服务：{exc.__class__.__name__}") from exc
    return {"ok": True, "message": "连接成功"}
