from __future__ import annotations
from datetime import date as DateType, datetime
from typing import Any, Literal
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator


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
    start_date: DateType | None = None
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
    granularity: Literal["auto", "compact", "balanced", "detailed"] = "auto"
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


class ProviderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    provider_type: str = Field(default="openai_compatible", min_length=1, max_length=50)
    base_url: str = Field(min_length=8, max_length=500)
    model: str = Field(min_length=1, max_length=200)
    temperature: float = Field(default=0.1, ge=0, le=2)
    timeout: int = Field(default=60, ge=1, le=300)
    api_key_env: str = Field(default="", max_length=100)
    enabled: bool = False
    api_key: str = Field(default="", max_length=10000)

    @field_validator("name", "provider_type", "base_url", "model", "api_key_env", "api_key", mode="before")
    @classmethod
    def trim_provider_text(cls, value):
        return str(value or "").strip()

    @field_validator("base_url")
    @classmethod
    def validate_provider_url(cls, value: str):
        if not value.startswith(("https://", "http://")):
            raise ValueError("API Base URL 必须以 http:// 或 https:// 开头")
        return value.rstrip("/")


class ImportParseRequest(BaseModel):
    content: str = Field(min_length=1)
    source_name: str = "Manual Import"
    use_llm: bool = True
    llm_mode: Literal["off", "auto", "force"] | None = None
    granularity: Literal["auto", "compact", "balanced", "detailed"] = "auto"
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


class ImportDocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    relative_path: str
    filename: str
    extension: str
    size: int
    extraction_status: str
    page_count: int | None = None
    slide_count: int | None = None
    sheet_count: int | None = None
    duration_seconds: int | None = None
    error_message: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict, validation_alias=AliasChoices("metadata_", "metadata"))


class ImportBatchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    source_kind: str
    root_name: str
    status: str
    file_count: int
    supported_count: int
    skipped_count: int
    error_count: int
    total_size: int
    total_media_seconds: int
    documents: list[ImportDocumentOut] = Field(default_factory=list)


class ChunkedFileSpec(BaseModel):
    relative_path: str = Field(min_length=1, max_length=1000)
    size: int = Field(ge=0, le=128 * 1024 * 1024 * 1024)
    mime_type: str = Field(default="", max_length=200)
    last_modified: int | None = Field(default=None, ge=0)
    duration_seconds: int | None = Field(default=None, ge=1, le=10 * 365 * 24 * 60 * 60)


class ChunkedUploadInit(BaseModel):
    root_name: str = Field(default="导入资料", min_length=1, max_length=500)
    source_kind: Literal["files", "directory"] = "files"
    files: list[ChunkedFileSpec] = Field(min_length=1, max_length=5000)


class PlanningOptions(BaseModel):
    batch_id: int
    start_date: DateType
    schedule_mode: Literal["days", "end_date"] = "days"
    day_count: int = Field(default=7, ge=1, le=365)
    end_date: DateType | None = None
    include_weekends: bool = True
    excluded_dates: list[DateType] = Field(default_factory=list)
    daily_available_minutes: int | None = Field(default=None, ge=15, le=1440)
    intensity: Literal["light", "standard", "intensive"] = "standard"
    review_ratio: float = Field(default=0.1, ge=0, le=0.5)
    buffer_ratio: float = Field(default=0.05, ge=0, le=0.5)
    allow_media_split: bool = True
    allow_section_split: bool = True
    goal: str = Field(default="", max_length=1000)
    selected_document_ids: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_schedule_range(self):
        if self.schedule_mode == "end_date":
            if self.end_date is None:
                raise ValueError("按结束日期规划时必须选择结束日期")
            if self.end_date < self.start_date:
                raise ValueError("结束日期不能早于开始日期")
            if (self.end_date - self.start_date).days > 365:
                raise ValueError("开始日期与结束日期之间最多允许 366 个自然日")
        return self


class LearningUnit(BaseModel):
    id: str
    title: str
    document_id: int
    source_path: str
    unit_type: Literal["reading", "video", "audio", "exercise", "project", "review", "other"] = "other"
    topic: str = "通用"
    sequence: int = 0
    estimated_minutes: int = Field(default=30, ge=1, le=10080)
    splittable: bool = True
    prerequisites: list[str] = Field(default_factory=list)
    summary: str = ""
    page_from: int | None = None
    page_to: int | None = None
    media_start: int | None = None
    media_end: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class LearningUnitSet(BaseModel):
    title: str
    goal: str = ""
    units: list[LearningUnit]
    warnings: list[str] = Field(default_factory=list)


class PlannedDay(BaseModel):
    date: DateType
    title: str
    estimated_minutes: int
    capacity_minutes: int
    load_ratio: float
    units: list[LearningUnit]
    warnings: list[str] = Field(default_factory=list)
    locked: bool = False


class SchedulePreview(BaseModel):
    title: str
    goal: str
    start_date: DateType
    end_date: DateType
    day_count: int
    total_estimated_minutes: int
    average_daily_minutes: int
    days: list[PlannedDay]
    unscheduled_units: list[LearningUnit] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    provider: str
    model: str


class PlanningConfirmRequest(BaseModel):
    schedule: SchedulePreview
    batch_id: int
    duplicate_strategy: Literal["skip", "import_anyway"] = "skip"
