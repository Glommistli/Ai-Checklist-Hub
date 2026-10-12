from datetime import UTC, date, datetime, timedelta
import threading
from typing import Annotated, Literal
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from app.core.database import get_db, SessionLocal
from app.core.utc_json import UTCJSONResponse
from app.models import Item, Checklist, EmailIngestion
from app.models.workflow import ModelCall, WorkflowAction, ReplanProposal
from app.schemas import ItemOut
from app.services import workflow as wf
from app.services import gmail_service as gmail
from app.services import replanning
from app.services.parser import LLMProviderError

router=APIRouter(prefix="/api/workspace",default_response_class=UTCJSONResponse)
Db=Annotated[object,Depends(get_db)]
def item(db,id):
    obj=db.get(Item,id)
    if not obj or not wf.is_active(obj):raise HTTPException(404,"任务不存在")
    return obj

class TaskFolds(BaseModel):
    folds:dict[str,bool]=Field(default_factory=dict,max_length=10000)

@router.get("/task-folds")
def task_folds(db:Db):return wf.preference(db,"task_folds",{"folds":{}})

@router.put("/task-folds")
def save_task_folds(body:TaskFolds,db:Db):
    if any(not key.isdigit() for key in body.folds):raise HTTPException(400,"折叠记录的任务编号无效")
    return wf.put_preference(db,"task_folds",body.model_dump())

@router.get("/pending-count")
def pending_count(db:Db):
    state=gmail.gmail_state(db)
    query=select(func.count(EmailIngestion.id)).where(EmailIngestion.status.in_(["candidate","failed","processing"]))
    if state.get("email"):query=query.where(EmailIngestion.account_email==state["email"])
    return {"count":db.scalar(query) or 0}

@router.get("/pending")
def pending(db:Db):
    state=gmail.gmail_state(db)
    query=select(EmailIngestion).where(EmailIngestion.status.in_(["candidate","failed","processing"]))
    if state.get("email"):query=query.where(EmailIngestion.account_email==state["email"])
    rows=db.scalars(query.order_by(EmailIngestion.id.desc()))
    return [{"id":r.id,"subject":r.subject,"sender":r.sender,"status":r.status,"reason":r.error_message or r.reason,
        "preview_title":r.preview_title,"received_at":r.received_at,"confidence":r.confidence,
        "gmail_url":f"https://mail.google.com/mail/u/0/#all/{r.message_id}",
        "plan":(r.metadata_ or {}).get("llm_plan"),"attempt_count":r.attempt_count} for r in rows]

@router.post("/pending/{id}/retry")
def retry(id:int,db:Db):
    record=db.get(EmailIngestion,id)
    if not record:raise HTTPException(404,"邮件不存在")
    try:return gmail.retry_ingestion(db,record)
    except gmail.GmailError as exc:raise HTTPException(400,str(exc)) from exc

@router.get("/trash")
def trash(db:Db):
    lists={l.id:l for l in db.scalars(select(Checklist))}
    result=[{"id":l.id,"kind":"list","title":l.name,"deleted_at":l.metadata_.get("deleted_at")} for l in lists.values() if not wf.is_active(l)]
    for i in db.scalars(select(Item)):
        md=i.metadata_ or {}
        if md.get("deleted_at") and md.get("trash_root")==i.id and (i.list_id not in lists or wf.is_active(lists[i.list_id])):
            result.append({"id":i.id,"kind":"item","title":i.title,"deleted_at":md["deleted_at"]})
    return result

@router.post("/trash/{kind}/{id}/restore")
def restore(kind:Literal["item","list"],id:int,db:Db):
    wf.restore(db,kind,id);return {"restored":True}

@router.post("/undo")
def undo(db:Db):return wf.undo(db)

@router.get("/actions")
def actions(db:Db):
    return [{"id":a.id,"summary":a.summary,"kind":a.kind,"undone":a.undone,"created_at":a.created_at}
        for a in db.scalars(select(WorkflowAction).order_by(WorkflowAction.id.desc()).limit(30))]

@router.get("/source/{id}")
def source(id:int,db:Db):return wf.source_for(db,item(db,id))

@router.get("/daily")
def daily(db:Db,day:date):return wf.daily(db,day)

class DailyInput(BaseModel):
    day:date|None=None
    exclude_day:date|None=None
    capacity_minutes:int|None=Field(default=None,ge=15,le=1440)
@router.put("/daily/{id}")
def schedule(id:int,body:DailyInput,db:Db):
    target=item(db,id)
    while target.parent_id:
        parent=db.get(Item,target.parent_id)
        if not parent:break
        target=parent
    before=wf.capture(db)
    md=dict(target.metadata_ or {})
    excluded=set(md.get("planned_skip_dates",[]))
    if body.day:excluded.discard(body.day.isoformat())
    elif body.exclude_day:excluded.add(body.exclude_day.isoformat())
    target.metadata_={**md,"planned_date":body.day.isoformat() if body.day else None,"planned_skip_dates":sorted(excluded)[-60:]}
    wf.record_action(db,"schedule",before,"调整今日安排："+target.title);db.commit()
    return ItemOut.model_validate(target)

class Capacity(BaseModel):
    minutes:int=Field(ge=15,le=1440)
@router.put("/capacity")
def capacity(body:Capacity,db:Db):return wf.put_preference(db,"daily_capacity",body.model_dump())

class EvidenceInput(BaseModel):
    links:list[str]=Field(default_factory=list,max_length=20)
    result_note:str=Field(default="",max_length=4000)
    delay_reason:str=Field(default="",max_length=4000)
@router.put("/evidence/{id}")
def evidence(id:int,body:EvidenceInput,db:Db):
    if any(not link.startswith(("https://","http://")) for link in body.links):raise HTTPException(400,"成果链接仅接受 http/https 地址")
    target=item(db,id);before=wf.capture(db)
    target.metadata_={**(target.metadata_ or {}),"evidence":body.links,"result_note":body.result_note,"delay_reason":body.delay_reason}
    wf.record_action(db,"evidence",before,"保存成果："+target.title);db.commit()
    return ItemOut.model_validate(target)

@router.get("/review")
def review(db:Db,start:date):return wf.weekly(db,start)

@router.get("/review/export",response_class=PlainTextResponse)
def export(db:Db,start:date):
    return PlainTextResponse(wf.weekly(db,start)["markdown"],headers={"Content-Disposition":"attachment; filename=weekly-review.md"})

@router.post("/replan/{id}")
def replan(id:int,db:Db):
    try:p=replanning.propose(db,item(db,id))
    except LLMProviderError as exc:raise HTTPException(400,str(exc)) from exc
    return {"id":p.id,"operations":p.operations,"provider":p.provider,"model":p.model,"status":p.status}

class ApplyInput(BaseModel):
    selected:list[int]=Field(default_factory=list)
    overrides:dict[str,list[str]]=Field(default_factory=dict)
@router.post("/replan/{id}/apply")
def apply(id:int,body:ApplyInput,db:Db):
    proposal=db.get(ReplanProposal,id)
    if not proposal:raise HTTPException(404,"提议不存在")
    return replanning.apply(db,proposal,body.selected,body.overrides)

class AIControls(BaseModel):
    daily_call_limit:int=Field(default=200,ge=0,le=10000)
    sender_allowlist:list[str]=Field(default_factory=list,max_length=100)
    sender_blocklist:list[str]=Field(default_factory=list,max_length=100)
@router.get("/ai-controls")
def controls(db:Db):return wf.preference(db,"ai_controls",AIControls().model_dump())
@router.put("/ai-controls")
def save_controls(body:AIControls,db:Db):
    return wf.put_preference(db,"ai_controls",{**body.model_dump(),"sender_allowlist":[s.strip().lower() for s in body.sender_allowlist if s.strip()],"sender_blocklist":[s.strip().lower() for s in body.sender_blocklist if s.strip()]})

@router.get("/usage")
def usage(db:Db):
    now=datetime.now(UTC).replace(tzinfo=None);start=datetime.combine((now+timedelta(hours=8)).date(),datetime.min.time())-timedelta(hours=8)
    rows=list(db.scalars(select(ModelCall).where(ModelCall.created_at>=start).order_by(ModelCall.id.desc())))
    return {"calls":len(rows),"input_tokens":sum(r.input_tokens for r in rows),"output_tokens":sum(r.output_tokens for r in rows),
        "daily_call_limit":controls(db)["daily_call_limit"],
        "recent":[{"id":r.id,"provider":r.provider,"model":r.model,"status":r.status,"elapsed_ms":r.elapsed_ms,"created_at":r.created_at} for r in rows[:20]]}

_job_lock=threading.Lock()
def run_job():
    try:
        with SessionLocal() as db:
            try:
                wf.put_preference(db,"gmail_job",{"status":"running","stage":"reading","processed":0,"total":0,"started_at":datetime.now(UTC).isoformat()})
                result=gmail.sync_gmail(db)
                state=wf.preference(db,"gmail_job")
                wf.put_preference(db,"gmail_job",{**state,"status":"paused" if gmail.gmail_state(db).get("paused") else ("partial" if result.get("failed") else "completed"),"stage":"finished","result":result,"error":f"{result['failed']} 封处理失败，请查看待处理列表" if result.get("failed") else None})
            except Exception as exc:
                wf.put_preference(db,"gmail_job",{**wf.preference(db,"gmail_job"),"status":"failed","stage":"failed","error":str(exc)[:500]})
    finally:_job_lock.release()

@router.get("/gmail-job")
def job(db:Db):return wf.preference(db,"gmail_job",{"status":"idle","stage":"idle","processed":0,"total":0})
@router.post("/gmail-job")
def start(db:Db):
    if not _job_lock.acquire(blocking=False):raise HTTPException(409,"已有处理任务在运行")
    gmail.save_gmail_state(db,{"paused":False})
    threading.Thread(target=run_job,name="gmail-ui-job",daemon=True).start()
    return {"started":True}
@router.post("/gmail-job/pause")
def pause(db:Db):
    gmail.save_gmail_state(db,{"paused":True})
    return {"paused":True,"message":"当前模型请求完成后暂停，不再启动下一封邮件"}
@router.post("/gmail-job/resume")
def resume(db:Db):return start(db)
