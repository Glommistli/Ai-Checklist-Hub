from datetime import UTC, datetime, timedelta
import threading
import time
import httpx
from sqlalchemy import func, select
from app.models.workflow import ModelCall
from .workflow import preference

_reservation_lock=threading.Lock()
def model_post(db, url, *, provider, model, **kwargs):
    if db is None:return httpx.post(url,**kwargs)
    now=datetime.now(UTC).replace(tzinfo=None)
    today=(now+timedelta(hours=8)).date()
    start=datetime.combine(today,datetime.min.time())-timedelta(hours=8)
    with _reservation_lock:
        config=preference(db,"ai_controls",{"daily_call_limit":200})
        used=db.scalar(select(func.count(ModelCall.id)).where(ModelCall.created_at>=start)) or 0
        if config["daily_call_limit"] and used>=int(config["daily_call_limit"]):
            from .parser import LLMProviderError
            raise LLMProviderError("今日模型调用额度已用完，请调整额度或明天重试")
        entry=ModelCall(provider=provider,model=model,status="running")
        db.add(entry);db.commit();id=entry.id
    tick=time.monotonic()
    try:
        response=httpx.post(url,**kwargs)
        if hasattr(response,"extensions"): response.extensions["workflow_call_id"]=id
        row=db.get(ModelCall,id)
        row.status="failed" if getattr(response,"is_error",False) else "success"
        try:
            usage=response.json().get("usage") or {}
            row.input_tokens=max(0,int(usage.get("prompt_tokens",0)))
            row.output_tokens=max(0,int(usage.get("completion_tokens",0)))
        except (ValueError,TypeError,AttributeError):pass
        row.elapsed_ms=round((time.monotonic()-tick)*1000);db.commit()
        return response
    except Exception:
        db.rollback();row=db.get(ModelCall,id)
        if row:row.status="failed";row.elapsed_ms=round((time.monotonic()-tick)*1000);db.commit()
        raise
