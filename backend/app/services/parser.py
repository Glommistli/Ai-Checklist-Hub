from __future__ import annotations
import json
import os
import re
from datetime import date, timedelta
import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.core.config import settings
from app.core.secrets import get_secret, provider_secret_name
from app.models import Item, ProviderConfig
from app.schemas import ActorAnalysis, ParsedItem, ParsedList, ParsedPlan
from .agent_parser import AgentOptions, build_actor_messages, build_agent_messages
from .granularity import normalize_plan
from .repository import fingerprint_for
from .model_calls import model_post

HEADER_RE = re.compile(r"^(?:#{1,6}\s*)?(.{1,80}?)[：:]\s*$")
BULLET_RE = re.compile(r"^\s*(?:[-*+•○◯☐□]|\d+[.)、])\s*(.+?)\s*$")
CHECK_RE = re.compile(r"^\s*[-*]?\s*\[[ xX]\]\s*(.+?)\s*$")
META_RE = re.compile(r"^(?:总时长|预计时长|备注|说明|验收标准|复盘)[：:]", re.I)
ROLE_ASSIGNMENT_RE = re.compile(
    r"(?:团支书|管理员|负责人|班主任|辅导员|组长|经理|审批人|工作人员|学生|团员|成员|申请人)"
    r".{0,12}(?:需|需要|负责|应当|必须|请|进行|完成|审核|提交|填写|转发)",
    re.I,
)


class LLMProviderError(RuntimeError):
    """A user-actionable provider configuration or request failure."""


def _clean(line: str) -> str:
    line = line.strip()
    checked = CHECK_RE.match(line)
    if checked: return checked.group(1).strip()
    bullet = BULLET_RE.match(line)
    if bullet: return bullet.group(1).strip()
    return line


def _detect_type(content: str) -> str:
    lower = content.lower()
    signals = [
        ("release_checklist", ["release", "上线", "部署", "changelog", "migration", "docker"]),
        ("travel_checklist", ["旅行", "出差", "护照", "酒店", "行李", "车票", "机票"]),
        ("shopping_list", ["购物", "买：", "买:", "采购", "牛奶", "鸡蛋"]),
        ("meeting_actions", ["会议", "action item", "行动项"]),
        ("study_plan", ["学习", "leetcode", "算法", "复盘", "threadlocal"]),
        ("project_plan", ["项目", "bug", "前端", "后端", "测试"]),
    ]
    scored = [(kind, sum(token in lower for token in tokens)) for kind, tokens in signals]
    kind, score = max(scored, key=lambda x: x[1])
    return kind if score else "generic"


def _default_title(content_type: str) -> str:
    names = {
        "shopping_list": "购物清单", "travel_checklist": "旅行准备",
        "release_checklist": "发布清单", "meeting_actions": "会议行动项",
        "study_plan": "学习计划", "project_plan": "项目计划", "generic": "导入的清单",
    }
    return names.get(content_type, "导入的清单")


def rule_parse(content: str) -> ParsedPlan:
    lines = [line.strip() for line in content.replace("\r\n", "\n").split("\n") if line.strip()]
    content_type = _detect_type(content)
    headers = [(i, HEADER_RE.match(line).group(1).strip()) for i, line in enumerate(lines) if HEADER_RE.match(line)]
    plan_title: str | None = None
    section_headers = headers[:]
    if headers:
        first_idx, first_name = headers[0]
        later_headers = [h for h in headers if h[0] > first_idx]
        title_markers = ("计划", "清单", "之前", "上线", "准备", "action items", "待办", "任务", "发布", "今晚买", "今天")
        if any(marker in first_name.lower() for marker in title_markers):
            plan_title = first_name
            section_headers = later_headers

    lists: list[ParsedList] = []
    header_positions = {idx: name for idx, name in section_headers}
    current: ParsedList | None = None
    preamble_items: list[ParsedItem] = []
    skip_title_index = headers[0][0] if plan_title and headers else -1

    def ensure_list(name: str = "Inbox") -> ParsedList:
        nonlocal current
        if current is None or current.name != name:
            current = ParsedList(name=name, items=[])
            lists.append(current)
        return current

    for idx, raw in enumerate(lines):
        if idx == skip_title_index: continue
        if idx in header_positions:
            current = ParsedList(name=header_positions[idx], items=[])
            lists.append(current)
            continue
        text = _clean(raw)
        if not text or META_RE.match(text):
            continue
        # Ignore sentence-like introductory lines before a detected section.
        item = ParsedItem(title=text)
        if current is None and section_headers:
            preamble_items.append(item)
        else:
            ensure_list("Inbox" if not section_headers else current.name if current else "Inbox").items.append(item)

    if preamble_items:
        lists.insert(0, ParsedList(name="Inbox", items=preamble_items))
    lists = [lst for lst in lists if lst.items]
    if not lists:
        lists = [ParsedList(name="Inbox", items=[ParsedItem(title=_clean(x)) for x in lines if _clean(x)])]
    # A single title header should not also become an item.
    title = plan_title or _default_title(content_type)
    return ParsedPlan(title=title, content_type=content_type, date=date.today() if "今天" in content else None, lists=lists, confidence=0.78, parser="rule")


class OpenAICompatibleProvider:
    def __init__(self, base_url: str, api_key: str, model: str, temperature: float = 0.1, timeout: int = 60, usage_db: Session | None = None, provider_name: str | None = None):
        self.base_url, self.api_key, self.model = base_url.rstrip("/"), api_key, model
        self.temperature, self.timeout = temperature, timeout
        self.usage_db,self.provider_name=usage_db,provider_name or base_url

    def parse(self, content: str, options: AgentOptions | None = None) -> ParsedPlan:
        options = options or AgentOptions()
        schema = ParsedPlan.model_json_schema()
        response = model_post(self.usage_db,
            f"{self.base_url}/chat/completions",
            provider=self.provider_name, model=self.model,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json={"model": self.model, "temperature": self.temperature,
                "messages": build_agent_messages(content, schema, options),
                "response_format": {"type": "json_object"}}, timeout=self.timeout,
        )
        response.raise_for_status()
        try:
            payload = json.loads(response.json()["choices"][0]["message"]["content"])
            parsed = ParsedPlan.model_validate(payload)
        except (ValueError,KeyError,TypeError):
            if self.usage_db is not None and getattr(response,"extensions",{}).get("workflow_call_id"):
                from app.models.workflow import ModelCall
                call=self.usage_db.get(ModelCall,response.extensions["workflow_call_id"])
                call.status="invalid";self.usage_db.commit()
            raise
        parsed.parser = "agent_v2"
        if not 0 <= parsed.confidence <= 1:
            raise ValueError("模型置信度必须在 0 到 1 之间")
        return parsed

    def analyze_actors(self, content: str) -> ActorAnalysis:
        schema = ActorAnalysis.model_json_schema()
        response = model_post(self.usage_db,
            f"{self.base_url}/chat/completions",
            provider=self.provider_name, model=self.model,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json={"model": self.model, "temperature": 0,
                "messages": build_actor_messages(content, schema),
                "response_format": {"type": "json_object"}}, timeout=self.timeout,
        )
        response.raise_for_status()
        payload = json.loads(response.json()["choices"][0]["message"]["content"])
        return ActorAnalysis.model_validate(payload)

    def test_connection(self) -> None:
        response = model_post(self.usage_db,
            f"{self.base_url}/chat/completions",
            provider=self.provider_name, model=self.model,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json={
                "model": self.model,
                "temperature": 0,
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "Reply with OK."}],
            },
            timeout=self.timeout,
        )
        response.raise_for_status()


def provider_api_key(config: ProviderConfig) -> str | None:
    """Resolve a provider key without ever persisting it in the main database."""
    return get_secret(provider_secret_name(config.id)) or os.getenv(config.api_key_env)


def analyze_actors(content: str, db: Session) -> ActorAnalysis:
    config = db.scalar(select(ProviderConfig).where(ProviderConfig.enabled.is_(True)))
    if config:
        key = provider_api_key(config)
        if not key:
            raise LLMProviderError(f"Provider“{config.name}”已启用，但尚未配置 API Key")
        try:
            analysis = OpenAICompatibleProvider(config.base_url, key, config.model, 0, config.timeout,usage_db=db,provider_name=config.name).analyze_actors(content)
            analysis.provider = config.name
            analysis.model = config.model
            return analysis
        except httpx.HTTPStatusError as exc:
            raise LLMProviderError(
                f"{config.name} 返回 HTTP {exc.response.status_code}。请检查 API Key、账户余额、Base URL 和模型名称"
            ) from exc
        except httpx.RequestError as exc:
            raise LLMProviderError(f"无法连接 {config.name}：{exc.__class__.__name__}") from exc
        except (ValueError, KeyError) as exc:
            raise LLMProviderError(f"{config.name} 返回的责任主体分析无效") from exc
    if settings.openai_api_key:
        try:
            analysis = OpenAICompatibleProvider(settings.openai_base_url, settings.openai_api_key, settings.openai_model, 0,usage_db=db,provider_name="Default").analyze_actors(content)
            analysis.provider = "Default OpenAI-compatible"
            analysis.model = settings.openai_model
            return analysis
        except httpx.HTTPStatusError as exc:
            raise LLMProviderError(f"默认模型服务返回 HTTP {exc.response.status_code}") from exc
        except httpx.RequestError as exc:
            raise LLMProviderError(f"无法连接默认模型服务：{exc.__class__.__name__}") from exc
        except (ValueError, KeyError) as exc:
            raise LLMProviderError("默认模型服务返回的责任主体分析无效") from exc
    raise LLMProviderError("没有可用的大模型。请先在“设置 → LLM Provider”中启用 Provider、保存 API Key，并测试连接")


def _parse_with_llm(content: str, db: Session, options: AgentOptions) -> ParsedPlan:
    config = db.scalar(select(ProviderConfig).where(ProviderConfig.enabled.is_(True)))
    if config:
        key = provider_api_key(config)
        if not key:
            raise LLMProviderError(f"Provider“{config.name}”已启用，但尚未配置 API Key")
        try:
            parsed = OpenAICompatibleProvider(config.base_url, key, config.model, config.temperature, config.timeout,usage_db=db,provider_name=config.name).parse(content, options)
            parsed.provider = config.name
            parsed.model = config.model
            return parsed
        except httpx.HTTPStatusError as exc:
            raise LLMProviderError(
                f"{config.name} 返回 HTTP {exc.response.status_code}。请检查 API Key、账户余额、Base URL 和模型名称"
            ) from exc
        except httpx.RequestError as exc:
            raise LLMProviderError(f"无法连接 {config.name}：{exc.__class__.__name__}") from exc
        except (ValueError, KeyError) as exc:
            raise LLMProviderError(f"{config.name} 返回的内容不是有效任务结构") from exc
    if settings.openai_api_key:
        try:
            parsed = OpenAICompatibleProvider(settings.openai_base_url, settings.openai_api_key, settings.openai_model,usage_db=db,provider_name="Default").parse(content, options)
            parsed.provider = "Default OpenAI-compatible"
            parsed.model = settings.openai_model
            return parsed
        except httpx.HTTPStatusError as exc:
            raise LLMProviderError(f"默认模型服务返回 HTTP {exc.response.status_code}") from exc
        except httpx.RequestError as exc:
            raise LLMProviderError(f"无法连接默认模型服务：{exc.__class__.__name__}") from exc
        except (ValueError, KeyError) as exc:
            raise LLMProviderError("默认模型服务返回的内容不是有效任务结构") from exc
    raise LLMProviderError("没有可用的大模型。请先在“设置 → LLM Provider”中启用 Provider、保存 API Key，并测试连接")


def parse_content(
    content: str, db: Session, use_llm: bool = True, force_llm: bool = False,
    agent_options: AgentOptions | None = None,
) -> ParsedPlan:
    options = agent_options or AgentOptions()
    parsed = rule_parse(content)
    item_count = sum(len(lst.items) for lst in parsed.lists)
    # Auto mode saves cost; force mode guarantees an LLM request or returns a visible error.
    role_sensitive = options.only_user_actions and ROLE_ASSIGNMENT_RE.search(content) is not None
    auto_wants_llm = use_llm and (item_count <= 1 or parsed.confidence < 0.6 or role_sensitive)
    configured_provider = db.scalar(select(ProviderConfig.id).where(ProviderConfig.enabled.is_(True))) is not None
    if force_llm or (auto_wants_llm and (configured_provider or settings.openai_api_key)):
        parsed = _parse_with_llm(content, db, options)
    parsed = normalize_plan(parsed, options)

    duplicates = 0
    for lst in parsed.lists:
        for item in lst.items:
            fp = fingerprint_for(item.title, lst.name, parsed.title, None, parsed.date)
            item.duplicate = db.scalar(select(Item.id).where(Item.fingerprint == fp)) is not None
            duplicates += int(item.duplicate)
    parsed.duplicate_count = duplicates
    return parsed
