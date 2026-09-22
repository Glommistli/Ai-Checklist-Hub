from __future__ import annotations

import re

from app.schemas import ParsedItem, ParsedPlan
from .agent_parser import AgentOptions


CONDITIONAL_RE = re.compile(r"^(?:如果|若|如遇|一旦|当.+?时|if\b|when\b)", re.I)
LOW_VALUE_STEP_RE = re.compile(
    r"^(?:"
    r"(?:在\s*)?(?:LeetCode|力扣|平台).*(?:提交|确认通过)"
    r"|(?:打开|进入|点击|查看).*(?:页面|Swagger|控制台|结果)"
    r"|(?:运行|使用).*(?:示例|用例).*(?:验证|确认|检查).*"
    r"|(?:提交|确认|检查|验证).*(?:通过|成功|正常|可见|存在).*"
    r"|(?:能够?)?(?:口头)?(?:解释|复述).+"
    r")[。；;]?$",
    re.I,
)
EXPLICIT_SUBTASK_RE = re.compile(
    r"(?:\b(?:LC|LeetCode)\s*[-#：:]?\s*\d+\b|力扣\s*[-#：:]?\s*\d+)",
    re.I,
)
ROLE_TERMS = (
    "团支书", "团员", "管理员", "负责人", "班主任", "辅导员", "组长", "经理",
    "审批人", "工作人员", "学生", "成员", "申请人", "开发者", "测试人员", "设计师",
)
UNIVERSAL_ROLES = ("所有人", "全体", "所有接收者", "本人", "当前用户", "用户", "any", "all", "everyone")


def _clean_texts(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        cleaned = str(value).strip()
        if cleaned and cleaned not in result:
            result.append(cleaned)
    return result


def _compact_prose(value: str | None, *, max_segments: int, max_chars: int) -> str | None:
    if not value:
        return value
    cleaned = re.sub(r"\s+", " ", value).strip()
    segments = [part.strip() for part in re.split(r"(?<=[。！？!?；;])\s*|\n+", cleaned) if part.strip()]
    selected: list[str] = []
    for segment in segments:
        candidate = "".join([*selected, segment])
        if selected and (len(selected) >= max_segments or len(candidate) > max_chars):
            break
        selected.append(segment)
        if len("".join(selected)) >= max_chars:
            break
    result = "".join(selected) or cleaned
    if len(result) > max_chars:
        clauses = [part.strip() for part in re.split(r"(?<=[，,、])\s*", result) if part.strip()]
        kept: list[str] = []
        for clause in clauses:
            if kept and len("".join([*kept, clause])) > max_chars:
                break
            kept.append(clause)
        result = "".join(kept) or result[: max_chars - 1].rstrip("，,、；;。") + "…"
    return result


def _apply_compact_shape(item: ParsedItem) -> None:
    """Make compact plans glanceable while preserving omitted details in metadata."""
    original_description = item.description
    item.description = _compact_prose(item.description, max_segments=1, max_chars=72)
    if original_description and item.description != original_description:
        item.metadata.setdefault("full_description", original_description)

    original_acceptance = item.acceptance_criteria
    item.acceptance_criteria = _compact_prose(item.acceptance_criteria, max_segments=3, max_chars=150)
    if original_acceptance and item.acceptance_criteria != original_acceptance:
        item.metadata.setdefault("full_acceptance_criteria", original_acceptance)

    original_notes = item.notes
    item.notes = _compact_prose(item.notes, max_segments=1, max_chars=72)
    if original_notes and item.notes != original_notes:
        item.metadata.setdefault("full_notes", original_notes)

    if len(item.steps) > 2:
        item.metadata.setdefault("additional_steps", item.steps[2:])
        item.steps = item.steps[:2]
    if len(item.tags) > 3:
        item.metadata.setdefault("additional_tags", item.tags[3:])
        item.tags = item.tags[:3]


def _absorb_low_value_steps(item: ParsedItem) -> None:
    """Keep administrative verification details out of the executable step list."""
    kept: list[str] = []
    absorbed: list[str] = []
    for step in item.steps:
        (absorbed if LOW_VALUE_STEP_RE.match(step) else kept).append(step)
    item.steps = kept
    if not absorbed:
        return
    requirement = "；".join(absorbed)
    if item.acceptance_criteria:
        if requirement not in item.acceptance_criteria:
            item.acceptance_criteria = f"{item.acceptance_criteria.rstrip('。')}；{requirement}。"
    else:
        item.acceptance_criteria = requirement.rstrip("。") + "。"


def _promote_explicit_subtasks(item: ParsedItem) -> None:
    """Turn explicitly named problem units into independently checkable children."""
    existing = {child.title.strip().lower() for child in item.subtasks}
    kept_steps: list[str] = []
    for step in item.steps:
        if EXPLICIT_SUBTASK_RE.search(step):
            title = step.strip().rstrip("。；;")
            if title.lower() not in existing:
                item.subtasks.append(ParsedItem(title=title))
                existing.add(title.lower())
        else:
            kept_steps.append(step)
    item.steps = kept_steps


def _normalize_item(item: ParsedItem, options: AgentOptions) -> ParsedItem:
    item.title = item.title.strip()
    item.steps = _clean_texts(item.steps)
    _absorb_low_value_steps(item)
    _promote_explicit_subtasks(item)
    item.dependencies = _clean_texts(item.dependencies)
    normalized_conditions: list[dict] = []
    for entry in item.conditional_actions:
        condition = str(entry.get("condition") or "满足条件时").strip()
        raw_actions = entry.get("actions", entry.get("action", []))
        actions = raw_actions if isinstance(raw_actions, list) else [raw_actions]
        cleaned_actions = _clean_texts([str(action) for action in actions])
        if cleaned_actions:
            normalized_conditions.append({"condition": condition, "actions": cleaned_actions})
    item.conditional_actions = normalized_conditions
    item.subtasks = [_normalize_item(child, options) for child in item.subtasks if child.title.strip()]
    if options.granularity == "compact":
        _apply_compact_shape(item)
    if options.steps_as_subtasks and item.steps:
        existing = {child.title for child in item.subtasks}
        item.subtasks.extend(ParsedItem(title=step) for step in item.steps if step not in existing)
        item.steps = []
    return item


def _role_matches_user(assignee: str | None, user_context: str) -> bool:
    if not assignee:
        return True
    normalized_assignee = assignee.strip().lower()
    if any(role in normalized_assignee for role in UNIVERSAL_ROLES):
        return True
    normalized_user = user_context.strip().lower()
    if not normalized_user:
        return False
    if normalized_assignee in normalized_user or normalized_user in normalized_assignee:
        return True
    assigned_terms = {term for term in ROLE_TERMS if term in normalized_assignee}
    user_terms = {term for term in ROLE_TERMS if term in normalized_user}
    return bool(assigned_terms & user_terms)


def _filter_other_roles(plan: ParsedPlan, options: AgentOptions) -> None:
    if not options.only_user_actions:
        return
    removed_roles: set[str] = set()
    for task_list in plan.lists:
        kept: list[ParsedItem] = []
        for item in task_list.items:
            if _role_matches_user(item.assignee_role, options.user_context):
                kept.append(item)
            else:
                removed_roles.add(item.assignee_role or "其他角色")
        task_list.items = kept
    plan.lists = [task_list for task_list in plan.lists if task_list.items]
    if removed_roles:
        roles = "、".join(sorted(removed_roles))
        plan.warnings.append(f"已排除仅面向其他责任主体的事项：{roles}")
    if removed_roles and not options.user_context.strip():
        plan.warnings.append("当前未指定身份；Agent 未假定你具有通知中的特定职务。可返回导入页填写“我的身份 / 职责”后重新规划")


def _absorb_conditionals(plan: ParsedPlan) -> None:
    for task_list in plan.lists:
        kept: list[ParsedItem] = []
        for item in task_list.items:
            if CONDITIONAL_RE.match(item.title) and kept:
                kept[-1].conditional_actions.append({"condition": item.title, "actions": item.steps or [item.title]})
                if item.description:
                    kept[-1].conditional_actions[-1]["description"] = item.description
            else:
                kept.append(item)
        task_list.items = kept


def _condense_to_limit(plan: ParsedPlan, limit: int) -> None:
    def total() -> int:
        return sum(len(task_list.items) for task_list in plan.lists)

    condensed = 0
    while total() > limit:
        candidates = [task_list for task_list in plan.lists if len(task_list.items) > 1]
        if not candidates:
            break
        task_list = max(candidates, key=lambda current: len(current.items))
        index = min(range(1, len(task_list.items)), key=lambda i: len(task_list.items[i].title))
        item = task_list.items.pop(index)
        parent = task_list.items[index - 1]
        parent.steps = _clean_texts([*parent.steps, item.title, *item.steps])
        parent.subtasks.extend(item.subtasks)
        parent.conditional_actions.extend(item.conditional_actions)
        parent.dependencies = _clean_texts([*parent.dependencies, *item.dependencies])
        condensed += 1
    if condensed:
        plan.warnings.append(f"为符合顶层任务上限，已将 {condensed} 个细小事项收纳为执行步骤")


def normalize_plan(plan: ParsedPlan, options: AgentOptions) -> ParsedPlan:
    plan.granularity = options.granularity
    plan.lists = [task_list for task_list in plan.lists if task_list.items]
    for task_list in plan.lists:
        task_list.items = [_normalize_item(item, options) for item in task_list.items if item.title.strip()]
    _filter_other_roles(plan, options)
    _absorb_conditionals(plan)
    _condense_to_limit(plan, options.max_top_level_tasks)
    plan.task_count = sum(len(task_list.items) for task_list in plan.lists)
    plan.step_count = sum(
        len(item.steps) + len(item.subtasks)
        for task_list in plan.lists
        for item in task_list.items
    )
    return plan
