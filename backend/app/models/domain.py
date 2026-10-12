from __future__ import annotations
from datetime import UTC, date, datetime
from typing import Any, List as TList
from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, JSON, String, Table, Text, Column, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base

item_tags = Table(
    "item_tags", Base.metadata,
    Column("item_id", ForeignKey("items.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class AppPreference(Base, TimestampMixin):
    __tablename__ = "app_preferences"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Source(Base, TimestampMixin):
    __tablename__ = "sources"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    kind: Mapped[str] = mapped_column(String(50), default="manual")
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class RawContent(Base, TimestampMixin):
    __tablename__ = "raw_contents"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), nullable=True)
    source_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    content: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(String(50), default="text")
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)


class ImportBatch(Base, TimestampMixin):
    __tablename__ = "import_batches"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_kind: Mapped[str] = mapped_column(String(30), default="files")
    root_name: Mapped[str] = mapped_column(String(500), default="导入资料")
    status: Mapped[str] = mapped_column(String(30), default="ready", index=True)
    file_count: Mapped[int] = mapped_column(Integer, default=0)
    supported_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    total_size: Mapped[int] = mapped_column(Integer, default=0)
    total_media_seconds: Mapped[int] = mapped_column(Integer, default=0)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    documents: Mapped[list["ImportDocument"]] = relationship(back_populates="batch", cascade="all, delete-orphan")


class ImportDocument(Base, TimestampMixin):
    __tablename__ = "import_documents"
    __table_args__ = (UniqueConstraint("batch_id", "relative_path", name="uq_import_document_batch_path"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id", ondelete="CASCADE"), index=True)
    relative_path: Mapped[str] = mapped_column(String(1000))
    filename: Mapped[str] = mapped_column(String(500))
    extension: Mapped[str] = mapped_column(String(30), default="")
    mime_type: Mapped[str | None] = mapped_column(String(200), nullable=True)
    size: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    extraction_status: Mapped[str] = mapped_column(String(30), default="ready")
    extracted_text: Mapped[str] = mapped_column(Text, default="")
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    slide_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sheet_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sequence_hint: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    batch: Mapped[ImportBatch] = relationship(back_populates="documents")


class EmailIngestion(Base, TimestampMixin):
    """Durable Gmail processing ledger.

    A row is claimed before a message body is downloaded.  The unique account /
    message id pair is the last line of defence against duplicate scans when the
    user clicks sync repeatedly, the process restarts, or Gmail history overlaps.
    """

    __tablename__ = "email_ingestions"
    __table_args__ = (UniqueConstraint("account_email", "message_id", name="uq_email_ingestion_account_message"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_email: Mapped[str] = mapped_column(String(320), index=True)
    message_id: Mapped[str] = mapped_column(String(255))
    thread_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    subject: Mapped[str] = mapped_column(String(500), default="")
    sender: Mapped[str] = mapped_column(String(500), default="")
    received_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="processing", index=True)
    classification: Mapped[str | None] = mapped_column(String(50), nullable=True)
    confidence: Mapped[float | None] = mapped_column(nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    preview_title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    body_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    item_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)


class Category(Base, TimestampMixin):
    __tablename__ = "categories"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    color: Mapped[str | None] = mapped_column(String(20), nullable=True)


class Plan(Base, TimestampMixin):
    __tablename__ = "plans"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    date: Mapped[date | None] = mapped_column(Date, nullable=True)
    content_type: Mapped[str] = mapped_column(String(50), default="generic")
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), nullable=True)
    raw_content_id: Mapped[int | None] = mapped_column(ForeignKey("raw_contents.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="active")
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    lists: Mapped[list[Checklist]] = relationship(back_populates="plan", cascade="all, delete-orphan")


class Checklist(Base, TimestampMixin):
    __tablename__ = "lists"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id", ondelete="CASCADE"), nullable=True)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    color: Mapped[str | None] = mapped_column(String(20), nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    plan: Mapped[Plan | None] = relationship(back_populates="lists")
    items: Mapped[list[Item]] = relationship(back_populates="list", cascade="all, delete-orphan", foreign_keys="Item.list_id")


class Tag(Base, TimestampMixin):
    __tablename__ = "tags"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)


class Item(Base, TimestampMixin):
    __tablename__ = "items"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed: Mapped[bool] = mapped_column(Boolean, default=False)
    list_id: Mapped[int | None] = mapped_column(ForeignKey("lists.id", ondelete="CASCADE"), nullable=True)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), nullable=True)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id", ondelete="CASCADE"), nullable=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), nullable=True)
    priority: Mapped[str | None] = mapped_column(String(20), nullable=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    estimated_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    acceptance_criteria: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str | None] = mapped_column(String(100), nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    fingerprint: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    list: Mapped[Checklist | None] = relationship(back_populates="items", foreign_keys=[list_id])
    tags: Mapped[TList[Tag]] = relationship(secondary=item_tags, lazy="selectin")
    parent: Mapped[Item | None] = relationship(remote_side="Item.id", back_populates="children", foreign_keys=[parent_id])
    children: Mapped[TList[Item]] = relationship(back_populates="parent", cascade="all, delete-orphan", foreign_keys=[parent_id])


class ProviderConfig(Base, TimestampMixin):
    __tablename__ = "provider_configs"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    provider_type: Mapped[str] = mapped_column(String(50), default="openai_compatible")
    base_url: Mapped[str] = mapped_column(String(500))
    model: Mapped[str] = mapped_column(String(200))
    temperature: Mapped[float] = mapped_column(default=0.1)
    timeout: Mapped[int] = mapped_column(default=60)
    api_key_env: Mapped[str] = mapped_column(String(100), default="OPENAI_API_KEY")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
