from __future__ import annotations

import json
import math
from datetime import date, timedelta
from uuid import uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import ImportBatch, ImportDocument, ProviderConfig
from app.schemas import LearningUnit, LearningUnitSet, PlannedDay, PlanningOptions, SchedulePreview
from .parser import LLMProviderError, provider_api_key
from .model_calls import model_post


def _provider(db: Session) -> tuple[ProviderConfig | None, str, str, str, float, int]:
    config = db.scalar(select(ProviderConfig).where(ProviderConfig.enabled.is_(True)))
    if config:
        key = provider_api_key(config)
        if not key:
            raise LLMProviderError(f"Provider“{config.name}”已启用，但尚未配置 API Key")
        return config, config.name, config.model, key, config.temperature, config.timeout
    if settings.openai_api_key:
        return None, "Default OpenAI-compatible", settings.openai_model, settings.openai_api_key, 0.1, 90
    raise LLMProviderError("智能规划强制使用 LLM。请先在“设置 → LLM Provider”中启用 Provider、保存 API Key，并测试连接")


def _representative_excerpt(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    windows = 6
    window_size = max(80, limit // windows)
    last_start = max(0, len(text) - window_size)
    pieces = []
    for index in range(windows):
        start = round(last_start * index / max(1, windows - 1))
        pieces.append(text[start:start + window_size])
    return "\n[…跨区段采样…]\n".join(pieces)[:limit]


def _document_manifest(documents: list[ImportDocument]) -> str:
    parts = []
    remaining = 80_000
    for index, doc in enumerate(documents):
        meta = {
            "document_id": doc.id, "path": doc.relative_path, "extension": doc.extension,
            "size_bytes": doc.size,
            "pages": doc.page_count, "slides": doc.slide_count, "sheets": doc.sheet_count,
            "duration_seconds": doc.duration_seconds,
            "sampled": bool((doc.metadata_ or {}).get("text_sampled")),
        }
        documents_left = max(1, len(documents) - index)
        excerpt_budget = min(12_000, max(500, remaining // documents_left))
        excerpt = _representative_excerpt(doc.extracted_text, min(excerpt_budget, remaining))
        remaining -= len(excerpt)
        parts.append(f"文件元数据：{json.dumps(meta, ensure_ascii=False)}\n正文摘录：\n{excerpt or '[没有正文；仅使用文件名和媒体时长]'}")
        if remaining <= 0:
            break
    return "\n\n---\n\n".join(parts)


def analyze_learning_units(db: Session, options: PlanningOptions) -> tuple[LearningUnitSet, str, str]:
    batch = db.get(ImportBatch, options.batch_id)
    if not batch:
        raise ValueError("导入批次不存在")
    docs = list(db.scalars(select(ImportDocument).where(ImportDocument.batch_id == batch.id).order_by(ImportDocument.sequence_hint)))
    if options.selected_document_ids:
        selected = set(options.selected_document_ids)
        docs = [doc for doc in docs if doc.id in selected]
    docs = [doc for doc in docs if doc.extraction_status in {"ready", "warning"}]
    if not docs:
        raise ValueError("没有可用于规划的资料")
    config, provider_name, model, key, temperature, timeout = _provider(db)
    schema = LearningUnitSet.model_json_schema()
    intensity = {"light": "轻量理解", "standard": "理解并适度练习", "intensive": "深入学习、练习和复盘"}[options.intensity]
    system = f"""你是资料与任务规划分析器。必须把每一份输入资料转换为一个或多个连续、可执行的任务或学习单元，并严格返回 JSON。

要求：
1. 用户填写的任务/学习目标：{options.goal or '[未填写，请根据资料内容自动概括一个简洁、准确的总体目标]'}；处理强度：{intensity}。当用户未填写目标时，必须在输出的 goal 字段中给出根据资料推断的目标；不得要求用户补填。
2. 每个资料至少对应一个 learning unit，不得遗漏 document_id。
3. 依据目录、文件名、章节标题、页码、字幕时间和媒体时长保持自然顺序。
4. 视频或音频的 estimated_minutes 以真实 duration_seconds 为基础；standard 可乘 1.25，intensive 可乘 1.5。
5. 文档按章节或主题形成单元；无法判断时按整份文件形成一个单元。不要虚构不存在的章节。
6. prerequisites 只填写确有逻辑依赖的单元标题或主题。
7. source_path 必须使用原始相对路径，document_id 必须使用提供的整数。
8. id 使用简短且唯一的字符串；sequence 表示全局建议学习顺序。
9. PDF/PPT 可填写 page_from/page_to；媒体可填写 media_start/media_end（秒）。
10. 单个单元通常控制在 20～120 分钟；超长媒体允许先保留为可拆分单元。

输出必须严格匹配 JSON Schema：
{json.dumps(schema, ensure_ascii=False)}"""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": _document_manifest(docs)}]
    base_url = config.base_url if config else settings.openai_base_url
    try:
        response = model_post(db,
            f"{base_url.rstrip('/')}/chat/completions",
            provider=provider_name, model=model,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": model, "temperature": temperature, "messages": messages, "response_format": {"type": "json_object"}},
            timeout=max(timeout, 90),
        )
        response.raise_for_status()
        result = LearningUnitSet.model_validate_json(response.json()["choices"][0]["message"]["content"])
    except httpx.HTTPStatusError as exc:
        raise LLMProviderError(f"{provider_name} 返回 HTTP {exc.response.status_code}，智能规划已停止") from exc
    except httpx.RequestError as exc:
        raise LLMProviderError(f"无法连接 {provider_name}，智能规划已停止：{exc.__class__.__name__}") from exc
    except (ValueError, KeyError) as exc:
        raise LLMProviderError(f"{provider_name} 返回的学习单元结构无效，智能规划已停止") from exc

    known = {doc.id: doc for doc in docs}
    cleaned = []
    seen_ids: set[str] = set()
    intensity_multiplier = {"light": 1.0, "standard": 1.25, "intensive": 1.5}[options.intensity]
    # LLM output is semantic advice, not an authority for source facts. Restore
    # its requested sequence first, then clamp ranges and media estimates to the
    # metadata extracted locally from the actual files.
    ordered_units = sorted(enumerate(result.units), key=lambda pair: (pair[1].sequence, pair[0]))
    for original_index, unit in ordered_units:
        if unit.document_id not in known:
            result.warnings.append(f"已忽略模型引用的未知资料 ID：{unit.document_id}")
            continue
        doc = known[unit.document_id]
        unit.source_path = doc.relative_path
        unit.sequence = len(cleaned)

        original_id = unit.id.strip() or f"unit-{doc.id}-{original_index + 1}"
        unique_id = original_id
        suffix = 2
        while unique_id in seen_ids:
            unique_id = f"{original_id}-{suffix}"
            suffix += 1
        if unique_id != unit.id:
            result.warnings.append(f"模型返回了重复或空的学习单元 ID，已规范为：{unique_id}")
        unit.id = unique_id
        seen_ids.add(unique_id)

        page_total = doc.page_count or doc.slide_count
        if page_total and (unit.page_from is not None or unit.page_to is not None):
            old_range = (unit.page_from, unit.page_to)
            start_page = min(max(unit.page_from or 1, 1), page_total)
            end_page = min(max(unit.page_to or start_page, 1), page_total)
            if end_page < start_page:
                start_page, end_page = end_page, start_page
            unit.page_from, unit.page_to = start_page, end_page
            if old_range != (start_page, end_page):
                result.warnings.append(f"已按实际页数修正范围：{doc.relative_path} → 第 {start_page}～{end_page} 页")

        if doc.duration_seconds:
            media_type = (doc.metadata_ or {}).get("media_type")
            if media_type in {"video", "audio"}:
                unit.unit_type = media_type
            old_range = (unit.media_start, unit.media_end)
            old_minutes = unit.estimated_minutes
            start_second = min(max(unit.media_start or 0, 0), max(0, doc.duration_seconds - 1))
            end_second = min(max(unit.media_end or doc.duration_seconds, start_second + 1), doc.duration_seconds)
            unit.media_start, unit.media_end = start_second, end_second
            unit.estimated_minutes = max(1, math.ceil((end_second - start_second) / 60 * intensity_multiplier))
            unit.splittable = True
            if old_range != (start_second, end_second) or old_minutes != unit.estimated_minutes:
                result.warnings.append(
                    f"已按实际媒体时长修正：{doc.relative_path} → {start_second}～{end_second} 秒，"
                    f"预计 {unit.estimated_minutes} 分钟"
                )
        cleaned.append(unit)
    represented = {unit.document_id for unit in cleaned}
    for doc in docs:
        if doc.id in represented:
            continue
        minutes = max(10, round((doc.duration_seconds or max(len(doc.extracted_text) / 350, 20)) / (60 if doc.duration_seconds else 1)))
        fallback_id = f"doc-{doc.id}-{uuid4().hex[:6]}"
        while fallback_id in seen_ids:
            fallback_id = f"doc-{doc.id}-{uuid4().hex[:6]}"
        seen_ids.add(fallback_id)
        cleaned.append(LearningUnit(
            id=fallback_id, title=f"学习 {doc.filename}", document_id=doc.id,
            source_path=doc.relative_path, unit_type=(doc.metadata_ or {}).get("media_type", "reading"),
            sequence=len(cleaned), estimated_minutes=minutes, splittable=True,
            summary="LLM 未单独列出该文件，本地完整性校验已将其补回。",
        ))
        result.warnings.append(f"已补回模型遗漏的资料：{doc.relative_path}")
    result.units = cleaned
    return result, provider_name, model


def _available_dates(options: PlanningOptions) -> list[date]:
    excluded = set(options.excluded_dates)
    dates, current = [], options.start_date
    if options.schedule_mode == "end_date":
        assert options.end_date is not None
        while current <= options.end_date:
            if current not in excluded and (options.include_weekends or current.weekday() < 5):
                dates.append(current)
            current += timedelta(days=1)
        if not dates:
            raise ValueError("所选日期范围内没有可安排任务的日期")
        return dates
    while len(dates) < options.day_count:
        if current not in excluded and (options.include_weekends or current.weekday() < 5):
            dates.append(current)
        current += timedelta(days=1)
        if (current - options.start_date).days > 730:
            raise ValueError("无法在指定条件下生成足够的可用日期")
    return dates


def _split_unit(unit: LearningUnit, target: int) -> list[LearningUnit]:
    if not unit.splittable or unit.estimated_minutes <= max(target * 1.35, 60):
        return [unit]
    count = max(2, math.ceil(unit.estimated_minutes / max(target, 30)))
    minutes = [unit.estimated_minutes // count] * count
    for index in range(unit.estimated_minutes % count):
        minutes[index] += 1
    parts = []
    media_span = (unit.media_end or 0) - (unit.media_start or 0)
    page_span = (unit.page_to or 0) - (unit.page_from or 1) + 1 if unit.page_to else 0
    minute_cursor = 0
    for index, part_minutes in enumerate(minutes):
        data = unit.model_dump()
        data["id"] = f"{unit.id}-part-{index + 1}"
        data["title"] = f"{unit.title}（第 {index + 1}/{count} 部分）"
        data["estimated_minutes"] = part_minutes
        if media_span > 0:
            start = (unit.media_start or 0) + round(media_span * minute_cursor / unit.estimated_minutes)
            end = (unit.media_start or 0) + round(media_span * (minute_cursor + part_minutes) / unit.estimated_minutes)
            data["media_start"], data["media_end"] = start, end
        if page_span > 0:
            start_page = (unit.page_from or 1) + round(page_span * minute_cursor / unit.estimated_minutes)
            end_page = (unit.page_from or 1) + round(page_span * (minute_cursor + part_minutes) / unit.estimated_minutes) - 1
            data["page_from"], data["page_to"] = start_page, max(start_page, end_page)
        minute_cursor += part_minutes
        parts.append(LearningUnit.model_validate(data))
    return parts


def _partition_ordered(units: list[LearningUnit], day_count: int) -> list[list[LearningUnit]]:
    """Minimize the largest daily content load without changing unit order."""
    if not units:
        return [[] for _ in range(day_count)]
    group_count = min(day_count, len(units))
    weights = [unit.estimated_minutes for unit in units]

    def required_groups(limit: int) -> int:
        groups, current = 1, 0
        for weight in weights:
            if current and current + weight > limit:
                groups += 1
                current = 0
            current += weight
        return groups

    low, high = max(weights), sum(weights)
    while low < high:
        middle = (low + high) // 2
        if required_groups(middle) <= group_count:
            high = middle
        else:
            low = middle + 1
    optimal_limit = low

    groups: list[list[LearningUnit]] = [[] for _ in range(group_count)]
    group_index, current = group_count - 1, 0
    for unit_index in range(len(units) - 1, -1, -1):
        unit = units[unit_index]
        # The second condition reserves one source unit for every earlier group,
        # which guarantees exactly group_count non-empty consecutive partitions.
        must_reserve = unit_index < group_index
        if group_index > 0 and groups[group_index] and (current + unit.estimated_minutes > optimal_limit or must_reserve):
            group_index -= 1
            current = 0
        groups[group_index].insert(0, unit)
        current += unit.estimated_minutes
    return groups + [[] for _ in range(day_count - group_count)]


def build_schedule(units: LearningUnitSet, options: PlanningOptions, provider: str, model: str) -> SchedulePreview:
    dates = _available_dates(options)
    base_total = sum(unit.estimated_minutes for unit in units.units)
    review_total = round(base_total * options.review_ratio)
    # The requested day count defines the balancing target. Daily availability is
    # a capacity constraint, not a reason to finish early and leave trailing days empty.
    # Review is appended separately, so splitting content against a target that
    # already contains review time makes content chunks too large and can cause
    # avoidable overload. Balance source work first, then distribute review.
    target = max(15, math.ceil(base_total / len(dates)))
    expanded = []
    for unit in sorted(units.units, key=lambda current: current.sequence):
        can_split = unit.splittable and ((unit.unit_type in {"video", "audio"} and options.allow_media_split) or (unit.unit_type not in {"video", "audio"} and options.allow_section_split))
        expanded.extend(_split_unit(unit, target) if can_split else [unit])

    buckets = _partition_ordered(expanded, len(dates))
    loads = [sum(unit.estimated_minutes for unit in bucket) for bucket in buckets]

    if review_total:
        nonempty_indices = [index for index, bucket in enumerate(buckets) if bucket]
        review_parts = [review_total // len(nonempty_indices)] * len(nonempty_indices) if nonempty_indices else []
        for index in range(review_total % len(nonempty_indices) if nonempty_indices else 0):
            review_parts[index] += 1
        for review_index, (index, review_minutes) in enumerate(zip(nonempty_indices, review_parts)):
            if review_minutes <= 0:
                continue
            bucket = buckets[index]
            topic = bucket[-1].topic
            review = LearningUnit(
                id=f"review-{review_index + 1}", title=f"复习与巩固：{topic}", document_id=bucket[-1].document_id,
                source_path=bucket[-1].source_path, unit_type="review", topic=topic, sequence=10_000 + review_index,
                estimated_minutes=review_minutes, splittable=False, summary="回顾当天重点并记录仍需解决的问题。",
            )
            bucket.append(review)
            loads[index] += review_minutes

    average = max(1, round(sum(loads) / len(dates)))
    days = []
    warnings = list(units.warnings)
    for index, current_date in enumerate(dates):
        topics = list(dict.fromkeys(unit.topic for unit in buckets[index]))
        title = "、".join(topics[:2]) if topics else "缓冲与调整"
        capacity = options.daily_available_minutes or math.ceil(average * (1 + options.buffer_ratio))
        day_warnings = []
        if loads[index] > capacity:
            day_warnings.append(f"预计超出当日容量 {loads[index] - capacity} 分钟")
        days.append(PlannedDay(
            date=current_date, title=title, estimated_minutes=loads[index], capacity_minutes=capacity,
            load_ratio=round(loads[index] / capacity, 2) if capacity else 0, units=buckets[index], warnings=day_warnings,
        ))
    if options.daily_available_minutes and sum(loads) > options.daily_available_minutes * len(dates):
        shortage = sum(loads) - options.daily_available_minutes * len(dates)
        warnings.append(
            f"当前资料预计超出总容量 {shortage} 分钟；所有资料仍会保留并创建为可编辑任务。"
            "如需降低每日负载，请增加天数、延后结束日期或提高每日可用时间。"
        )
    nonempty = [load for load in loads if load]
    if nonempty and max(nonempty) - min(nonempty) > average * 0.3:
        warnings.append("存在不可自然拆分的较大单元，每日负载偏差超过建议范围。")
    return SchedulePreview(
        title=units.title, goal=units.goal or options.goal, start_date=dates[0], end_date=dates[-1],
        day_count=len(dates), total_estimated_minutes=sum(loads), average_daily_minutes=average,
        days=days, warnings=warnings, provider=provider, model=model,
    )
