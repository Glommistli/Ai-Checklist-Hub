from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
import json


@dataclass
class SourceContent:
    source: str
    source_name: str
    content: str
    created_at: datetime = field(default_factory=datetime.utcnow)
    content_type: str = "text"
    external_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class ContentSource(ABC):
    @abstractmethod
    def fetch(self) -> list[SourceContent]: ...


class ManualTextSource(ContentSource):
    def __init__(self, content: str, name: str = "Manual Import"):
        self.content, self.name = content, name

    def fetch(self) -> list[SourceContent]:
        return [SourceContent(source="manual", source_name=self.name, content=self.content)]


class FileSource(ContentSource):
    def __init__(self, path: Path): self.path = path

    def fetch(self) -> list[SourceContent]:
        text = self.path.read_text(encoding="utf-8")
        if self.path.suffix.lower() == ".json":
            data = json.loads(text)
            text = data.get("content", json.dumps(data, ensure_ascii=False)) if isinstance(data, dict) else text
        return [SourceContent(source="file", source_name=self.path.name, content=text, content_type=self.path.suffix.lstrip(".") or "text")]


class ChatGPTAutomationSource(ContentSource):
    """Extension point for a future official ChatGPT Tasks API.

    No public, stable Tasks content API is assumed. This adapter deliberately does
    not scrape the ChatGPT website; configure an official connector here when one
    becomes available.
    """
    def fetch(self) -> list[SourceContent]:
        raise NotImplementedError("ChatGPT Tasks does not expose a configured official content API in this installation")

