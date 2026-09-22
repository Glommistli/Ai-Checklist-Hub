from __future__ import annotations
from datetime import UTC, date, datetime
import hashlib
import re
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


def create_item(db: Session, data: ItemCreate, list_name: str | None = None, plan_title: str | None = None) -> Item:
    values = data.model_dump(exclude={"tags", "metadata"})
    item = Item(**values, metadata_=data.metadata)
    item.fingerprint = fingerprint_for(item.title, list_name, plan_title, item.source, item.due_date)
    db.add(item)
    set_tags(db, item, data.tags)
    db.commit(); db.refresh(item)
    return item


def update_item(db: Session, item: Item, data: ItemUpdate) -> Item:
    values = data.model_dump(exclude_unset=True, exclude={"tags", "metadata"})
    for key, value in values.items(): setattr(item, key, value)
    if data.metadata is not None: item.metadata_ = data.metadata
    if data.tags is not None: set_tags(db, item, data.tags)
    if data.completed is not None: item.completed_at = datetime.now(UTC).replace(tzinfo=None) if data.completed else None
    db.commit(); db.refresh(item)
    return item
