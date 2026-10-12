from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Checklist, Item, Plan, RawContent, Source
from app.schemas import ImportConfirmRequest
from .repository import fingerprint_for, set_tags


def confirm_parsed_plan(data: ImportConfirmRequest, db: Session) -> dict:
    """Persist a validated import for UI, webhook and source-adapter callers."""
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

        def persist_item(parsed_item, item_pos: int, parent_id: int | None = None):
            nonlocal created, skipped
            fingerprint_date = parsed_item.due_date or parsed_item.start_date or plan.date
            fp = fingerprint_for(parsed_item.title, lst.name, plan.title, data.external_id, fingerprint_date)
            exists = db.scalar(select(Item.id).where(Item.fingerprint == fp)) is not None
            if exists and data.duplicate_strategy == "skip":
                skipped += 1
                return
            structured = {**data.source_metadata, **dict(parsed_item.metadata)}
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
                list_id=lst.id,
                plan_id=plan.id,
                parent_id=parent_id,
                source=data.source_name,
                position=item_pos,
                fingerprint=fp,
                metadata_=structured,
            )
            db.add(item)
            db.flush()
            created += 1
            set_tags(db, item, parsed_item.tags)
            item_ids.append(item.id)
            for child_pos, child in enumerate(parsed_item.subtasks):
                persist_item(child, child_pos, item.id)

        for item_pos, parsed_item in enumerate(parsed_list.items):
            persist_item(parsed_item, item_pos)
        if created > created_before_list:
            list_ids.append(lst.id)
        else:
            db.delete(lst)
            db.flush()
    db.commit()
    return {
        "plan_id": plan.id,
        "created": created,
        "skipped": skipped,
        "list_ids": list_ids,
        "item_ids": item_ids,
    }
