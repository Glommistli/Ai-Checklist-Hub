from __future__ import annotations
from datetime import UTC, date, datetime
import hashlib
import re
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models import Item, Tag
from app.schemas import ItemCreate, ItemUpdate


def normalize(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def fingerprint_for(title: str, list_name: str | None, plan_title: str | None, source: str | None, day: date | None) -> str:
    raw = "|".join([normalize(title), normalize(list_name), normalize(plan_title), normalize(source), day.isoformat() if day else ""])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def set_tags(db: Session, item: Item, names: list[str]):
    tags = []
    for name in {normalize(n) for n in names if normalize(n)}:
        tag = db.scalar(select(Tag).where(Tag.name == name))
        if not tag:
            tag = Tag(name=name)
            db.add(tag)
            db.flush()
        tags.append(tag)
    item.tags = tags


def _validate_parent(db: Session, parent_id: int | None, item_id: int | None = None) -> None:
    seen = set()
    while parent_id is not None:
        if parent_id == item_id or parent_id in seen:
            raise HTTPException(400, "任务不能成为自己或后代的子任务")
        seen.add(parent_id)
        parent = db.get(Item, parent_id)
        if parent is None:
            raise HTTPException(400, "父任务不存在")
        parent_id = parent.parent_id


def _set_completed(item: Item, completed: bool, now: datetime) -> None:
    item.completed = completed
    if completed:
        item.completed_at = item.completed_at or now
    else:
        item.completed_at = None


def _complete_subtree(db: Session, item: Item, completed: bool, now: datetime) -> None:
    pending = [item]
    seen = set()
    while pending:
        current = pending.pop()
        if current.id in seen:
            continue
        seen.add(current.id)
        _set_completed(current, completed, now)
        pending.extend(db.scalars(select(Item).where(Item.parent_id == current.id)))


def _refresh_ancestors(db: Session, parent_id: int | None, now: datetime) -> None:
    seen = set()
    while parent_id is not None and parent_id not in seen:
        seen.add(parent_id)
        db.flush()
        parent = db.get(Item, parent_id)
        if parent is None:
            break
        children = list(db.scalars(select(Item).where(Item.parent_id == parent_id)))
        if children:
            _set_completed(parent, all(child.completed for child in children), now)
        parent_id = parent.parent_id


def create_item(db: Session, data: ItemCreate, list_name: str | None = None, plan_title: str | None = None) -> Item:
    _validate_parent(db, data.parent_id)
    values = data.model_dump(exclude={"tags", "metadata"})
    item = Item(**values, metadata_=data.metadata)
    item.fingerprint = fingerprint_for(item.title, list_name, plan_title, item.source, item.due_date)
    db.add(item)
    set_tags(db, item, data.tags)
    db.flush()
    now = datetime.now(UTC).replace(tzinfo=None)
    _set_completed(item, bool(item.completed), now)
    _refresh_ancestors(db, item.parent_id, now)
    db.commit(); db.refresh(item)
    return item


def update_item(db: Session, item: Item, data: ItemUpdate) -> Item:
    values = data.model_dump(exclude_unset=True, exclude={"tags", "metadata"})
    old_parent_id = item.parent_id
    if "parent_id" in values:
        _validate_parent(db, values["parent_id"], item.id)
    for key, value in values.items():
        if key != "completed":
            setattr(item, key, value)
    if data.metadata is not None: item.metadata_ = data.metadata
    if data.tags is not None: set_tags(db, item, data.tags)
    now = datetime.now(UTC).replace(tzinfo=None)
    if data.completed is not None:
        _complete_subtree(db, item, data.completed, now)
    _refresh_ancestors(db, old_parent_id, now)
    if old_parent_id != item.parent_id:
        _refresh_ancestors(db, item.parent_id, now)
    db.commit(); db.refresh(item)
    return item


def delete_item_tree(db: Session, item: Item) -> None:
    parent_id = item.parent_id
    db.delete(item)
    db.flush()
    _refresh_ancestors(db, parent_id, datetime.now(UTC).replace(tzinfo=None))
    db.commit()
