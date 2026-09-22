from __future__ import annotations
import os
from html import escape
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated
from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse
import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload
from app.core.database import get_db
from app.core.config import settings
from app.core.secrets import delete_secret, get_secret, provider_secret_name, set_secret
from app.models import AppPreference, Category, Checklist, EmailIngestion, Item, Plan, ProviderConfig
from app.schemas import (
    ActorAnalyzeRequest, GmailCandidateOut, GmailClientRequest, GmailSettingsUpdate,
    ImportConfirmRequest, ImportParseRequest, ItemCreate, ItemOut, ItemUpdate,
    ListCreate, ListOut, ListUpdate, PlanCreate, PlanOut, PlanUpdate, UISettings,
)
from app.services.parser import LLMProviderError, OpenAICompatibleProvider, analyze_actors, parse_content, provider_api_key
from app.services.agent_parser import AgentOptions
from app.services.repository import create_item, update_item
from app.services.importer import confirm_parsed_plan
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
    q: str | None = Query(default=None, max_length=200), limit: int = Query(500, le=1000),
):
    stmt = select(Item).options(selectinload(Item.tags)).order_by(Item.completed, Item.position, Item.created_at.desc()).limit(limit)
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
def delete_item(item_id: int, db: Db): db.delete(get_or_404(db, Item, item_id)); db.commit()


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
def add_provider(data: dict, db: Db):
    allowed = {k: data[k] for k in ("name", "provider_type", "base_url", "model", "temperature", "timeout", "api_key_env", "enabled") if k in data}
    if not all(k in allowed for k in ("name", "base_url", "model")): raise HTTPException(422, "name, base_url and model are required")
    if not str(allowed["base_url"]).startswith(("https://", "http://")): raise HTTPException(422, "base_url must start with http:// or https://")
    if allowed.get("enabled"):
        for configured in db.scalars(select(ProviderConfig).where(ProviderConfig.enabled.is_(True))): configured.enabled = False
    obj = ProviderConfig(**allowed); db.add(obj); db.flush()
    api_key = str(data.get("api_key", "")).strip()
    if api_key: set_secret(provider_secret_name(obj.id), api_key)
    db.commit(); db.refresh(obj)
    return {"id": obj.id, **allowed, "has_api_key": bool(api_key or os.getenv(obj.api_key_env))}


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
