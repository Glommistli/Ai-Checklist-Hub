from __future__ import annotations
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Checklist, Item, Plan, RawContent, Source
from app.schemas import ImportConfirmRequest
from .repository import fingerprint_for, set_tags
from .workflow import active, capture, record_action


def confirm_parsed_plan(data: ImportConfirmRequest, db: Session, commit: bool = True) -> dict:
    """Persist a validated import for UI, webhook and source-adapter callers."""
    before = capture(db)
    touched = set()
    reused_plan = None
    source = db.scalar(select(Source).where(Source.name == data.source_name))
    if not source:
        source = Source(
            name=data.source_name,
            kind="gmail" if data.source_name.startswith("Gmail:") else "manual",
        )
        db.add(source)
        db.flush()
    raw = RawContent(
        source_id=source.id,
        source_name=source.name,
        content=data.raw_content or data.plan.model_dump_json(),
        content_type="email" if source.kind == "gmail" else "text",
        external_id=data.external_id,
        metadata_=data.source_metadata,
    )
    db.add(raw)
    db.flush()
    plan = Plan(
        title=data.plan.title,
        description=data.plan.description,
        date=data.plan.date,
        content_type=data.plan.content_type,
        source_id=source.id,
        raw_content_id=raw.id,
        metadata_=data.source_metadata,
    )
    db.add(plan)
    db.flush()
    created = skipped = 0
    list_ids: list[int] = []
    item_ids: list[int] = []
    for list_pos, parsed_list in enumerate(data.plan.lists):
        lst = Checklist(name=parsed_list.name, description=parsed_list.description, plan_id=plan.id, position=list_pos)
        db.add(lst)
        db.flush()
        created_before_list = created

        def persist_item(parsed_item, item_pos: int, parent_id: int | None = None, path=(), target_list=None, target_plan=None):
            nonlocal created, skipped, reused_plan
            if not parsed_item.title.strip():
                from fastapi import HTTPException
                raise HTTPException(400,"任务标题不能为空")
            target_list = lst.id if target_list is None else target_list
            target_plan = plan.id if target_plan is None else target_plan
            fingerprint_date = parsed_item.due_date or parsed_item.start_date or plan.date
            node_path = (*path, parsed_item.title.strip().lower())
            identity = data.external_id if not path else (data.external_id or "") + "::" + json.dumps(path,ensure_ascii=False,separators=(",",":"))
            fp = fingerprint_for(parsed_item.title, lst.name, plan.title, identity, fingerprint_date)
            existing = db.scalar(select(Item).where(Item.fingerprint == fp, active(Item)))
            if existing is None and parent_id is not None:
                legacy_fp=fingerprint_for(parsed_item.title,lst.name,plan.title,data.external_id,fingerprint_date)
                old_path_fp=fingerprint_for(parsed_item.title,lst.name,plan.title,(data.external_id or "")+"::"+"/".join(path),fingerprint_date)
                existing=db.scalar(select(Item).where(Item.fingerprint.in_([legacy_fp,old_path_fp]),Item.parent_id==parent_id,active(Item)))
            exists = existing is not None
            if exists and data.duplicate_strategy == "skip":
                skipped += 1
                reused_plan = existing.plan_id
                for child_pos, child in enumerate(parsed_item.subtasks):
                    persist_item(child,child_pos,existing.id,node_path,existing.list_id,existing.plan_id)
                return
            safe_metadata={k:v for k,v in parsed_item.metadata.items() if k not in {"deleted_at","trash_group","trash_root","manual_fields","planned_date","planned_skip_dates","replan_proposal","evidence","result_note","delay_reason"}}
            structured = {**safe_metadata, **data.source_metadata,
                "node_key":"/".join(node_path),"raw_content_id":raw.id,
                "ai_baseline": {name: getattr(parsed_item,name).isoformat() if name=="due_date" and getattr(parsed_item,name) else getattr(parsed_item,name) for name in ("title","description","due_date","estimated_minutes","acceptance_criteria","notes")}}
            if parsed_item.steps:
                structured["steps"] = parsed_item.steps
            if parsed_item.conditional_actions:
                structured["conditional_actions"] = parsed_item.conditional_actions
            if parsed_item.dependencies:
                structured["dependencies"] = parsed_item.dependencies
            if parsed_item.source_excerpt:
                structured["source_excerpt"] = parsed_item.source_excerpt
            if parsed_item.assignee_role:
                structured["assignee_role"] = parsed_item.assignee_role
            item = Item(
                title=parsed_item.title,
                description=parsed_item.description,
                priority=parsed_item.priority,
                due_date=parsed_item.due_date,
                start_date=parsed_item.start_date,
                estimated_minutes=parsed_item.estimated_minutes,
                acceptance_criteria=parsed_item.acceptance_criteria,
                notes=parsed_item.notes,
                list_id=target_list,
                plan_id=target_plan,
                parent_id=parent_id,
                source=data.source_name,
                position=item_pos,
                fingerprint=fp,
                metadata_=structured,
            )
            db.add(item)
            db.flush()
            created += 1
            touched.add(target_list)
            set_tags(db, item, parsed_item.tags)
            item_ids.append(item.id)
            for child_pos, child in enumerate(parsed_item.subtasks):
                persist_item(child, child_pos, item.id, node_path, target_list, target_plan)

        for item_pos, parsed_item in enumerate(parsed_list.items):
            persist_item(parsed_item, item_pos)
        if lst.id in touched:
            list_ids.append(lst.id)
        else:
            db.delete(lst)
            db.flush()
    list_ids = sorted(touched)
    result_plan = plan.id
    if not plan.lists or not list(db.scalars(select(Checklist.id).where(Checklist.plan_id==plan.id))):
        db.delete(plan); result_plan = reused_plan
    if created:
        from .repository import _refresh_ancestors
        from datetime import UTC, datetime
        for item_id in item_ids:
            _refresh_ancestors(db,db.get(Item,item_id).parent_id,datetime.now(UTC).replace(tzinfo=None))
    if not created: db.delete(raw)
    else: record_action(db,"import",before,"导入任务："+data.plan.title)
    if commit: db.commit()
    else: db.flush()
    return {
        "plan_id": result_plan,
        "created": created,
        "skipped": skipped,
        "list_ids": list_ids,
        "item_ids": item_ids,
    }
