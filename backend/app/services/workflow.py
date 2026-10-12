from __future__ import annotations
from datetime import UTC, date, datetime, timedelta
import hashlib
import json
import uuid
from sqlalchemy import select
from fastapi import HTTPException
from app.models import AppPreference, Checklist, Item, Plan, RawContent
from app.models.workflow import WorkflowAction

def active(model):
    return model.metadata_["deleted_at"].as_string().is_(None)

def is_active(item):
    return not (item.metadata_ or {}).get("deleted_at")

def preference(db, key, defaults=None):
    p = db.get(AppPreference, key)
    return {**(defaults or {}), **(p.value if p else {})}

def put_preference(db, key, value):
    p = db.get(AppPreference, key)
    if p: p.value = value
    else: db.add(AppPreference(key=key, value=value))
    db.commit()
    return value

FIELDS = ["id","title","description","completed","completed_at","list_id","plan_id","parent_id","priority","due_date","start_date","estimated_minutes","acceptance_criteria","notes","source","position","fingerprint"]
def snapshot_item(item):
    data = {name: getattr(item, name) for name in FIELDS}
    data = {k: v.isoformat() if isinstance(v,(date,datetime)) else v for k,v in data.items()}
    data["metadata"] = dict(item.metadata_ or {})
    data["tags"] = sorted(t.name for t in item.tags)
    return data

LIST_FIELDS=["id","name","description","color","position","plan_id"]
def snapshot_list(listing):
    return {**{key:getattr(listing,key) for key in LIST_FIELDS},"metadata":dict(listing.metadata_ or {})}

def capture(db):
    return {"items": [snapshot_item(i) for i in db.scalars(select(Item))],
            "lists": [snapshot_list(l) for l in db.scalars(select(Checklist))]}

def record_action(db, kind, before, summary):
    db.flush()
    after = capture(db)
    before_items = {r["id"]:r for r in before["items"]}; after_items = {r["id"]:r for r in after["items"]}
    ids = {i for i in set(before_items)|set(after_items) if before_items.get(i)!=after_items.get(i)}
    before_lists = {r["id"]:r for r in before["lists"]}; after_lists = {r["id"]:r for r in after["lists"]}
    lids = {i for i in set(before_lists)|set(after_lists) if before_lists.get(i)!=after_lists.get(i)}
    if not ids and not lids: return None
    action = WorkflowAction(kind=kind,summary=summary,
        before={"items":[before_items[i] for i in sorted(ids) if i in before_items],"lists":[before_lists[i] for i in sorted(lids) if i in before_lists]},
        after={"items":[after_items[i] for i in sorted(ids) if i in after_items],"lists":[after_lists[i] for i in sorted(lids) if i in after_lists]})
    db.add(action);db.flush()
    return action

def tree(db, item):
    result=[];pending=[item];seen=set()
    while pending:
        node=pending.pop()
        if node.id in seen:continue
        seen.add(node.id);result.append(node)
        pending.extend(db.scalars(select(Item).where(Item.parent_id==node.id)))
    return result

def soft_delete(db, item):
    from .repository import _refresh_ancestors
    before=capture(db);group=uuid.uuid4().hex;stamp=datetime.now(UTC).isoformat()
    for node in tree(db,item):
        if is_active(node):
            node.metadata_={**(node.metadata_ or {}),"deleted_at":stamp,"trash_group":group,"trash_root":item.id}
    db.flush();_refresh_ancestors(db,item.parent_id,datetime.now(UTC).replace(tzinfo=None))
    record_action(db,"delete",before,"移入回收站："+item.title);db.commit()

def soft_delete_list(db, listing):
    if listing.name=="Inbox":raise HTTPException(400,"Inbox 不可删除")
    before=capture(db);group=uuid.uuid4().hex;stamp=datetime.now(UTC).isoformat()
    listing.metadata_={**(listing.metadata_ or {}),"deleted_at":stamp,"trash_group":group}
    for node in db.scalars(select(Item).where(Item.list_id==listing.id)):
        if is_active(node):node.metadata_={**(node.metadata_ or {}),"deleted_at":stamp,"trash_group":group,"trash_root":node.id}
    record_action(db,"delete_list",before,"清单移入回收站："+listing.name);db.commit()

def restore(db, kind, id):
    from .repository import _refresh_ancestors
    before=capture(db)
    obj=db.get(Checklist if kind=="list" else Item,id)
    if obj is None or is_active(obj):raise HTTPException(404,"回收站记录不存在")
    group=obj.metadata_.get("trash_group")
    nodes=list(db.scalars(select(Item).where(Item.metadata_["trash_group"].as_string()==group)))
    if kind=="item":
        for node in nodes:
            parent=db.get(Item,node.parent_id) if node.parent_id else None
            if parent and not is_active(parent) and parent not in nodes:
                raise HTTPException(409,"请先恢复父任务或所属清单")
        listing=db.get(Checklist,obj.list_id) if obj.list_id else None
        if listing and not is_active(listing):raise HTTPException(409,"请先恢复所属清单")
    for node in [obj,*nodes]:
        node.metadata_={k:v for k,v in node.metadata_.items() if k not in {"deleted_at","trash_group","trash_root"}}
    db.flush()
    for node in nodes:_refresh_ancestors(db,node.parent_id,datetime.now(UTC).replace(tzinfo=None))
    record_action(db,"restore",before,"恢复回收站记录");db.commit()

def undo(db):
    from .repository import set_tags
    action=db.scalar(select(WorkflowAction).where(WorkflowAction.undone.is_(False)).order_by(WorkflowAction.id.desc()))
    if not action:raise HTTPException(404,"没有可撤销操作")
    for expected in action.after.get("items",[]):
        item=db.get(Item,expected["id"])
        if not item or snapshot_item(item)!=expected:raise HTTPException(409,"任务已发生其他修改，不能静默撤销；请先处理最新操作")
    for expected in action.after.get("lists",[]):
        listing=db.get(Checklist,expected["id"])
        if not listing or any(snapshot_list(listing).get(key)!=value for key,value in expected.items()):raise HTTPException(409,"清单已发生其他修改，不能撤销")
    before_ids={r["id"] for r in action.before.get("items",[])}
    stamp=datetime.now(UTC).isoformat();group=uuid.uuid4().hex
    for expected in action.after.get("items",[]):
        if expected["id"] not in before_ids:
            item=db.get(Item,expected["id"]);item.metadata_={**(item.metadata_ or {}),"deleted_at":stamp,"trash_group":group,"trash_root":item.id}
    for row in action.before.get("items",[]):
        item=db.get(Item,row["id"])
        for name in FIELDS:
            if name=="id":continue
            value=row[name]
            if value and name in {"due_date","start_date"}:value=date.fromisoformat(value)
            if value and name=="completed_at":value=datetime.fromisoformat(value).replace(tzinfo=None)
            setattr(item,name,value)
        item.metadata_=row["metadata"];set_tags(db,item,row["tags"])
    before_lids={r["id"] for r in action.before.get("lists",[])}
    for row in action.after.get("lists",[]):
        listing=db.get(Checklist,row["id"])
        if row["id"] not in before_lids:listing.metadata_={**(listing.metadata_ or {}),"deleted_at":stamp,"trash_group":group}
    for row in action.before.get("lists",[]):
        listing=db.get(Checklist,row["id"])
        for key,value in row.items():
            if key!="id":setattr(listing,"metadata_" if key=="metadata" else key,value)
    action.undone=True;db.commit()
    return {"summary":action.summary,"action_id":action.id}

def source_for(db,item):
    plan=db.get(Plan,item.plan_id) if item.plan_id else None
    raw_id=(item.metadata_ or {}).get("raw_content_id") or (plan.raw_content_id if plan else None)
    raw=db.get(RawContent,raw_id) if raw_id else None
    return {"item_id":item.id,"source_name":raw.source_name if raw else item.source,
        "content":raw.content if raw else item.metadata_.get("source_excerpt",item.description or ""),
        "excerpt":item.metadata_.get("source_excerpt"),"gmail_url":item.metadata_.get("gmail_url"),
        "provider":item.metadata_.get("llm_provider"),"model":item.metadata_.get("llm_model")}

def daily(db,day):
    config=preference(db,"daily_capacity",{"minutes":240})
    items=list(db.scalars(select(Item).where(active(Item),Item.parent_id.is_(None))))
    selected=[i for i in items if not i.completed and ((i.metadata_ or {}).get("planned_date")==day.isoformat() or (i.due_date==day and day.isoformat() not in (i.metadata_ or {}).get("planned_skip_dates",[]) and not (i.metadata_ or {}).get("planned_date")))]
    overdue=[i for i in items if not i.completed and i.due_date and i.due_date<day]
    done=[i for i in items if i.completed and (((i.metadata_ or {}).get("planned_date")==day.isoformat()) or i.due_date==day or (i.completed_at and (i.completed_at+timedelta(hours=8)).date()==day))]
    def minutes_for(i):
        if i.estimated_minutes is not None:return i.estimated_minutes
        return sum(minutes_for(child) for child in db.scalars(select(Item).where(Item.parent_id==i.id,active(Item))))
    minutes=sum(minutes_for(i) for i in selected)
    return {"date":day,"capacity_minutes":config["minutes"],"estimated_minutes":minutes,
        "over_capacity":minutes>config["minutes"],"overdue_ids":[i.id for i in overdue],
        "planned_ids":[i.id for i in selected],"completed_ids":[i.id for i in done]}

def weekly(db,start):
    end=start+timedelta(days=7)
    nodes=list(db.scalars(select(Item).where(active(Item),Item.parent_id.is_(None))))
    done=[i for i in nodes if i.completed_at and start<= (i.completed_at+timedelta(hours=8)).date()<end]
    pending=[i for i in nodes if not i.completed and ((i.due_date and i.due_date<end) or ((i.metadata_ or {}).get("planned_date") and start.isoformat()<=(i.metadata_ or {})["planned_date"]<end.isoformat()))]
    result={"start":start,"end":end-timedelta(days=1),
        "completed":[{"id":i.id,"title":i.title,"evidence":list(dict.fromkeys(link for node in tree(db,i) if is_active(node) for link in (node.metadata_ or {}).get("evidence",[]) if str(link).startswith(("http://","https://")))),"result":(i.metadata_ or {}).get("result_note","")} for i in done],
        "unfinished":[{"id":i.id,"title":i.title,"reason":(i.metadata_ or {}).get("delay_reason","")} for i in pending]}
    lines=[f"# 周复盘 {start} — {end-timedelta(days=1)}","",f"已完成目标：{len(done)}",""]
    for row in result["completed"]:
        lines+=["- "+row["title"]+("："+row["result"] if row["result"] else "")]
        for evidence in row["evidence"]:lines+=["  - "+evidence]
    lines+=["","## 未完成与下一步",""]
    lines+=["- "+row["title"]+("："+row["reason"] if row["reason"] else "") for row in result["unfinished"]]
    result["markdown"]="\n".join(lines)
    return result

def checksum(rows):
    return hashlib.sha256(json.dumps(rows,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
