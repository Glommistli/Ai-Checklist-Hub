import pytest
from app.services.parser import rule_parse
from app.services.agent_parser import AgentOptions, build_actor_messages, build_agent_messages
from app.services.granularity import normalize_plan
from app.schemas import ParsedItem, ParsedList, ParsedPlan

CASES = [
    ("算法：\nLeetCode 200\nLeetCode 994\n\n后端：\n学习 ThreadLocal", "study_plan", ["算法", "后端"], 3),
    ("今晚买：\n\n牛奶\n鸡蛋\n咖啡豆\n垃圾袋", "shopping_list", ["Inbox"], 4),
    ("去日本之前：\n\n证件：\n护照\n身份证\n\n行李：\n充电器\n相机\n衣服\n\n交通：\n购买机场快线车票", "travel_checklist", ["证件", "行李", "交通"], 6),
    ("网站 v2 上线：\n\n前端：\n完成 production build\n\n后端：\n运行 migration\n\n部署：\n上传服务器\n重启 Docker\n检查日志", "release_checklist", ["前端", "后端", "部署"], 5),
    ("会议 Action Items：\n\n张三完成 API 文档\n李四修改 UI\n周五之前完成测试", "meeting_actions", ["Inbox"], 3),
]


@pytest.mark.parametrize("text,kind,list_names,count", CASES)
def test_generic_domain_cases(text, kind, list_names, count):
    parsed = rule_parse(text)
    assert parsed.content_type == kind
    assert [x.name for x in parsed.lists] == list_names
    assert sum(len(x.items) for x in parsed.lists) == count


def test_optional_fields_are_not_required():
    parsed = rule_parse("购物：\n牛奶\n鸡蛋")
    assert parsed.lists[0].items[0].estimated_minutes is None
    assert parsed.lists[0].items[0].acceptance_criteria is None


def test_balanced_agent_prompt_rejects_administrative_micro_steps():
    messages = build_agent_messages("完成算法题", {"type": "object"}, AgentOptions(granularity="balanced"))
    prompt = messages[0]["content"]
    assert "2～4 个" in prompt
    assert "删除“在 LeetCode 提交并确认通过”" in prompt
    assert "steps 可以为空" in prompt


def test_compact_agent_prompt_requests_short_but_complete_fields():
    messages = build_agent_messages("完成完整学习日", {"type": "object"}, AgentOptions(granularity="compact"))
    prompt = messages[0]["content"]
    assert "description 只写一句" in prompt
    assert "steps 最多 2 条" in prompt
    assert "最多 3 个可核验结果" in prompt
    assert "不得合并或遗漏模块" in prompt


def test_agent_prompt_filters_tasks_by_current_user_role():
    messages = build_agent_messages(
        "团支书审核申请，普通团员完成转入",
        {"type": "object"},
        AgentOptions(user_context="2026级普通团员", only_user_actions=True),
    )
    prompt = messages[0]["content"]
    assert "当前用户身份/职责：2026级普通团员" in prompt
    assert "不得把它创建成用户任务" in prompt
    assert "metadata.assignee_role" in prompt


def test_agent_prompt_groups_explicit_problem_ids_as_subtasks():
    messages = build_agent_messages(
        "完成二叉树 BFS，需要做 LC102 和 LC199",
        {"type": "object"},
        AgentOptions(granularity="compact"),
    )
    prompt = messages[0]["content"]
    assert "一个顶层任务“完成二叉树 BFS”" in prompt
    assert "两个 subtasks：“完成 LC102”和“完成 LC199”" in prompt
    assert "不得因此删除原文明示的独立工作单元" in prompt


def test_actor_prompt_finds_roles_without_generating_tasks_or_guessing_user():
    messages = build_actor_messages("团支书审核申请，普通团员提交申请", {"type": "object"})
    prompt = messages[0]["content"]
    assert "当前工作不是生成任务" in prompt
    assert "不猜测当前用户属于哪个主体" in prompt
    assert "团支书" in prompt
    assert "团务指导部工作人员" in prompt


def test_role_filter_excludes_other_people_without_losing_general_tasks():
    plan = ParsedPlan(
        title="团组织关系转接",
        lists=[ParsedList(name="通知", items=[
            ParsedItem(title="审核本班申请", assignee_role="团支书"),
            ParsedItem(title="完成组织关系转入", assignee_role="班级团员"),
            ParsedItem(title="阅读截止时间", assignee_role="所有人"),
        ])],
    )
    normalized = normalize_plan(
        plan,
        AgentOptions(user_context="2026级普通团员", only_user_actions=True),
    )
    titles = [item.title for task_list in normalized.lists for item in task_list.items]
    assert titles == ["完成组织关系转入", "阅读截止时间"]
    assert any("团支书" in warning for warning in normalized.warnings)


def test_normalizer_absorbs_low_value_steps_into_acceptance_criteria():
    plan = ParsedPlan(
        title="算法学习",
        lists=[
            ParsedList(
                name="算法",
                items=[
                    ParsedItem(
                        title="完成 LeetCode 438 滑动窗口解法",
                        steps=[
                            "实现频次数组滑动窗口",
                            "使用三组用例验证输出",
                            "在 LeetCode 提交并确认通过",
                            "打开页面查看结果",
                        ],
                        acceptance_criteria="解法复杂度为 O(n)。",
                    )
                ],
            )
        ],
    )

    normalized = normalize_plan(plan, AgentOptions(granularity="balanced"))
    item = normalized.lists[0].items[0]
    assert item.steps == ["实现频次数组滑动窗口"]
    assert "在 LeetCode 提交并确认通过" in item.acceptance_criteria
    assert "打开页面查看结果" in item.acceptance_criteria
    assert normalized.step_count == 1


def test_normalizer_promotes_explicit_leetcode_units_to_subtasks():
    plan = ParsedPlan(
        title="算法学习",
        lists=[ParsedList(name="算法", items=[ParsedItem(
            title="完成二叉树 BFS",
            steps=["完成 LC102", "完成 LC199", "总结二叉树层序遍历模板"],
        )])],
    )
    normalized = normalize_plan(plan, AgentOptions(granularity="compact"))
    item = normalized.lists[0].items[0]
    assert item.steps == ["总结二叉树层序遍历模板"]
    assert [child.title for child in item.subtasks] == ["完成 LC102", "完成 LC199"]
    assert normalized.step_count == 3


def test_compact_normalizer_preserves_full_details_in_metadata():
    description = "完成滑动窗口算法并形成可运行代码。这里是背景、示例和逐步解释，不应占据精简说明。"
    acceptance = "代码通过核心用例；复杂度为 O(n)；完成错因复盘；补充更多示例与背景说明。"
    plan = ParsedPlan(
        title="精简计划",
        lists=[ParsedList(name="算法", items=[ParsedItem(
            title="完成滑动窗口算法",
            description=description,
            acceptance_criteria=acceptance,
            steps=["实现窗口状态更新", "处理边界条件", "整理示例", "补充说明"],
            tags=["算法", "C++", "滑动窗口", "LeetCode"],
        )])],
    )

    item = normalize_plan(plan, AgentOptions(granularity="compact")).lists[0].items[0]
    assert item.description == "完成滑动窗口算法并形成可运行代码。"
    assert len(item.steps) == 2
    assert item.tags == ["算法", "C++", "滑动窗口"]
    assert item.metadata["full_description"] == description
    assert item.metadata["full_acceptance_criteria"] == acceptance
    assert item.metadata["additional_steps"] == ["整理示例", "补充说明"]
    assert item.metadata["additional_tags"] == ["LeetCode"]
