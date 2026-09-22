from __future__ import annotations
from datetime import date as DateType, datetime
from typing import Any, Literal
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator


PRIORITY_ALIASES = {
    "urgent": "urgent", "critical": "urgent", "p0": "urgent", "紧急": "urgent",
    "high": "high", "important": "high", "p1": "high", "高": "high", "重要": "high",
    "medium": "medium", "normal": "medium", "moderate": "medium", "p2": "medium", "中": "medium", "普通": "medium",
    "low": "low", "minor": "low", "p3": "low", "低": "low",
    "": None, "none": None, "null": None, "无": None,
}


def normalize_priority(value):
    if value is None:
        return None
    return PRIORITY_ALIASES.get(str(value).strip().lower())


class TagOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str


class ItemBase(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    description: str | None = None
    completed: bool = False
    list_id: int | None = None
    category_id: int | None = None
    plan_id: int | None = None
    parent_id: int | None = None
    priority: Literal["low", "medium", "high", "urgent"] | None = None
    due_date: DateType | None = None
    start_date: DateType | None = None
    estimated_minutes: int | None = Field(default=None, ge=0)
    acceptance_criteria: str | None = None
    notes: str | None = None
    source: str | None = None
    position: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict, validation_alias=AliasChoices("metadata_", "metadata"))
    tags: list[str] = Field(default_factory=list)

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str) -> str:
        return value.strip()

    @field_validator("tags", mode="before")
    @classmethod
    def tag_names(cls, value):
        return [getattr(tag, "name", tag) for tag in (value or [])]

    @field_validator("priority", mode="before")
    @classmethod
    def clean_priority(cls, value):
        return normalize_priority(value)


class ItemCreate(ItemBase):
    pass


class ItemUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    completed: bool | None = None
    list_id: int | None = None
    category_id: int | None = None
    plan_id: int | None = None
    parent_id: int | None = None
    priority: str | None = None
    due_date: DateType | None = None
    start_date: DateType | None = None
    estimated_minutes: int | None = None
    acceptance_criteria: str | None = None
    notes: str | None = None
    source: str | None = None
    position: int | None = None
    metadata: dict[str, Any] | None = Field(default=None, validation_alias=AliasChoices("metadata_", "metadata"))
    tags: list[str] | None = None

    @field_validator("priority", mode="before")
    @classmethod
    def clean_priority(cls, value):
        return normalize_priority(value)


class ItemOut(ItemBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ListBase(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    plan_id: int | None = None
    category_id: int | None = None
    position: int = 0
    color: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict, validation_alias=AliasChoices("metadata_", "metadata"))


class ListCreate(ListBase): pass


class ListUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    plan_id: int | None = None
    category_id: int | None = None
    position: int | None = None
    color: str | None = None
    metadata: dict[str, Any] | None = Field(default=None, validation_alias=AliasChoices("metadata_", "metadata"))


class ListOut(ListBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    created_at: datetime
    updated_at: datetime
    item_count: int = 0
    pending_count: int = 0


class PlanBase(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str | None = None
    date: DateType | None = None
    content_type: str = "generic"
    source_id: int | None = None
    raw_content_id: int | None = None
    status: str = "active"
    metadata: dict[str, Any] = Field(default_factory=dict, validation_alias=AliasChoices("metadata_", "metadata"))


class PlanCreate(PlanBase): pass


class PlanUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    date: DateType | None = None
    content_type: str | None = None
    status: str | None = None
    metadata: dict[str, Any] | None = Field(default=None, validation_alias=AliasChoices("metadata_", "metadata"))


class PlanOut(PlanBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    created_at: datetime
    updated_at: datetime


class ParsedItem(BaseModel):
    title: str
    description: str | None = None
    priority: str | None = None
    due_date: DateType | None = None
    estimated_minutes: int | None = None
    acceptance_criteria: str | None = None
    notes: str | None = None
    tags: list[str] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)
    subtasks: list["ParsedItem"] = Field(default_factory=list)
    conditional_actions: list[dict[str, Any]] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    source_excerpt: str | None = None
    assignee_role: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    duplicate: bool = False

    @field_validator("priority", mode="before")
    @classmethod
    def clean_priority(cls, value):
        return normalize_priority(value)


class ParsedList(BaseModel):
    name: str
    description: str | None = None
    items: list[ParsedItem] = Field(default_factory=list)


class ParsedPlan(BaseModel):
    title: str
    goal: str | None = None
    description: str | None = None
    content_type: str = "generic"
    date: DateType | None = None
    lists: list[ParsedList] = Field(default_factory=list)
    confidence: float = 0.7
    parser: str = "rule"
    provider: str | None = None
    model: str | None = None
    granularity: Literal["compact", "balanced", "detailed"] = "balanced"
    task_count: int = 0
    step_count: int = 0
    warnings: list[str] = Field(default_factory=list)
    duplicate_count: int = 0


class ActorOption(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    evidence: list[str] = Field(default_factory=list)
    estimated_task_count: int = Field(default=0, ge=0)
    is_general: bool = False


class ActorAnalysis(BaseModel):
    actors: list[ActorOption] = Field(default_factory=list)
    summary: str = ""
    warnings: list[str] = Field(default_factory=list)
    provider: str | None = None
    model: str | None = None


class ActorAnalyzeRequest(BaseModel):
    content: str = Field(min_length=1)


class UISettings(BaseModel):
    sidebar_width: int = Field(default=256, ge=220, le=420)


class ImportParseRequest(BaseModel):
    content: str = Field(min_length=1)
    source_name: str = "Manual Import"
    use_llm: bool = True
    llm_mode: Literal["off", "auto", "force"] | None = None
    granularity: Literal["compact", "balanced", "detailed"] = "balanced"
    max_top_level_tasks: int = Field(default=8, ge=1, le=30)
    preserve_source_structure: bool = True
    steps_as_subtasks: bool = False
    user_context: str = Field(default="", max_length=500)
    only_user_actions: bool = True


class ImportConfirmRequest(BaseModel):
    plan: ParsedPlan
    source_name: str = "Manual Import"
    raw_content: str = ""
    duplicate_strategy: Literal["skip", "import_anyway"] = "skip"
    external_id: str | None = None
    source_metadata: dict[str, Any] = Field(default_factory=dict)


class GmailClientRequest(BaseModel):
    client_id: str = Field(min_length=20, max_length=500)
    client_secret: str = Field(min_length=1, max_length=500)


class GmailSettingsUpdate(BaseModel):
    sync_interval_hours: int = Field(default=1, ge=1, le=168)
    lookback_days: int = Field(default=14, ge=1, le=90)
    auto_import: bool = True
    auto_import_threshold: float = Field(default=0.9, ge=0.7, le=1.0)
    exclude_promotions: bool = True


class GmailCandidateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    subject: str
    sender: str
    received_at: datetime | None = None
    status: str
    classification: str | None = None
    confidence: float | None = None
    reason: str | None = None
    preview_title: str | None = None
    body_excerpt: str | None = None
    created_at: datetime
