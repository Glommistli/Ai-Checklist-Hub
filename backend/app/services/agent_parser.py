from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal


Granularity = Literal["compact", "balanced", "detailed"]


@dataclass(frozen=True)
class AgentOptions:
    granularity: Granularity = "balanced"
    max_top_level_tasks: int = 8
    preserve_source_structure: bool = True
    steps_as_subtasks: bool = False
    user_context: str = ""
    only_user_actions: bool = True


GRANULARITY_GUIDANCE: dict[Granularity, str] = {
    "compact": "优先生成 2～5 个顶层任务；说明只写一句目标与交付物，每项 0～2 个关键步骤，验收标准只保留 1～3 个核心结果。",
    "balanced": "通常生成 3～8 个顶层任务；说明不超过两句，每项保留 2～4 个会改变交付物状态的关键阶段。",
    "detailed": "通常生成 6～15 个任务；每项保留 3～6 个关键阶段，但仍不得记录点击、打开、提交、确认等微操作。",
}


def build_agent_messages(content: str, schema: dict, options: AgentOptions) -> list[dict[str, str]]:
    structure_rule = (
        "尽量保留原文明确的标题和分组，但仍须修正明显过细的任务。"
        if options.preserve_source_structure
        else "可以重新组织原文分组，以最终目标和执行阶段为优先。"
    )
    identity = options.user_context.strip() or "未提供具体身份"
    responsibility_rule = (
        f"当前用户身份/职责：{identity}。只生成明确由该用户身份负责，或面向所有接收者且用户需要执行的任务。"
        if options.only_user_actions
        else f"当前用户身份/职责：{identity}。可以保留通知中其他角色的任务，但必须在 metadata.assignee_role 标明责任主体。"
    )
    system = f"""你是一个目标导向的任务规划 Agent，不是逐句提取器。

你的职责是把输入组织成少量、清晰、可执行、可验收的任务。先理解最终目标、工作阶段、责任边界、约束和条件，再输出 JSON；不要输出分析过程或 Markdown。

任务规划规则：
1. 按独立交付结果拆分，不按动词数量拆分。
2. 同一目标、对象、负责人和阶段下的连续动作必须合并为一个任务。
3. steps 只保留会改变交付物状态、具有独立技术判断或阶段产出的关键动作；不是所有原文动作都要进入 steps。
4. “如果/若/发生……时”的动作放入 conditional_actions；条件尚未发生时不得创建顶层待办。
5. 风险、背景、解释和验收条件不得伪装成任务。
6. 顶层任务必须能独立完成、分配、延期或验收；否则应成为步骤。
7. 保留负责人、日期、优先级、依赖和验收条件，不得发明原文不存在的工作。
8. 顶层任务软上限为 {options.max_top_level_tasks}，超过时优先合并相同成果的小任务。
9. {structure_rule}
10. 当前粒度为 {options.granularity}：{GRANULARITY_GUIDANCE[options.granularity]}
11. title 使用结果导向的动宾表达，例如“完成发布前验证”，避免“看一下”“处理一下”。
12. source_excerpt 只保留支持该任务的简短原文片段。
13. 不得把“打开页面、进入目录、点击按钮、提交平台、确认通过、运行示例、查看结果、口头解释、复述知识”等操作性或验证性细节生成为 steps；把必要的结果要求合并到 acceptance_criteria。
14. steps 应描述阶段而非操作手册。例如保留“实现频次数组滑动窗口”，删除“在 LeetCode 提交并确认通过”。
15. 如果任务的 title、description 和 acceptance_criteria 已足以指导执行，steps 可以为空；不要为了填充字段而生成步骤。
16. 精简粒度下：title 使用 8～20 个汉字；description 只写一句“做什么 + 产出什么”，不要复述背景、示例、过程或技术清单；steps 最多 2 条；acceptance_criteria 用一句话保留最多 3 个可核验结果；notes 仅保留风险或不可忽略的限制；tags 最多 3 个。
17. 平衡粒度下也应避免把原文重新讲一遍：description 最多两句，acceptance_criteria 聚合为不超过 3 个结果。
18. 当原文已有编号顶层模块或明确类别（例如“1. 算法、2. 后端、3. Agent、4. 项目”），且要求保留原文结构时，必须逐项保留为独立顶层任务；只精简字段，不得合并或遗漏模块，除非模块数量明确超过顶层任务上限。
19. {responsibility_rule}
20. 先识别每条要求的责任主体、适用对象和触发条件。原文明确写“团支书需”“管理员负责”“负责人完成”等角色专属要求时，如果该角色与用户身份不匹配，不得把它创建成用户任务、步骤或子任务。
21. 用户未提供身份时，不得假定用户具有通知中的特定职务；只保留面向“所有人/各位成员/本人”的普遍任务。对被排除的角色专属要求，在 warnings 中简短说明“已排除仅面向某角色的事项”。
22. “如忘记密码”“如遇问题”等尚未发生的情况只进入 conditional_actions，不能成为当前顶层任务。每个保留任务可在 metadata.assignee_role 记录责任主体。
23. 顶层任务表示一个阶段目标；原文明确指定、可以分别勾选完成、但共同服务于该目标的工作单元，应放入 subtasks，而不是拆成多个顶层任务或笼统写成 steps。典型工作单元包括算法题编号、明确的章节、接口、页面、测试用例或交付件。
24. 例如原文要求“完成二叉树 BFS，需要做 LC102 和 LC199”，应生成一个顶层任务“完成二叉树 BFS”，并生成两个 subtasks：“完成 LC102”和“完成 LC199”。不得把“打开 LeetCode、提交、确认通过”等操作步骤作为子任务。
25. 精简模式限制的是顶层任务和说明篇幅，不得因此删除原文明示的独立工作单元；一个顶层任务可以保留 1～6 个简短、可勾选的 subtasks。subtasks 只保留原文明示的工作，不得自行扩充练习题或交付物。

输出必须严格匹配以下 JSON Schema：
{json.dumps(schema, ensure_ascii=False)}"""
    user = f"请把下面内容规划为可长期管理的任务。不要遗漏重要成果，也不要过度拆分。\n\n原始内容：\n{content}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def build_actor_messages(content: str, schema: dict) -> list[dict[str, str]]:
    system = f"""你是任务通知的责任主体分析 Agent。你的当前工作不是生成任务，而是识别文本中所有真正承担动作或结果责任的主体，供用户选择自己的身份。

分析规则：
1. 只返回需要执行任务、审批、提交、通知、填写或交付结果的责任主体。
2. 不要把文档、平台、组织名称、链接、被服务对象或仅被提及的人当成责任主体。
3. 合并同义称呼，例如“班级团员”和“支部团员”可归并为“团员（个人）”；但“团支书”和“团务指导部工作人员”必须分开。
4. 如果要求面向所有接收者，增加“所有接收者”主体并设置 is_general=true。
5. evidence 保留 1～3 条能证明该主体承担责任的简短原文；description 用一句话概括其职责范围。
6. estimated_task_count 估计该主体最终会得到的顶层任务数量，不统计点击、查看、转发后的确认等微操作。
7. 即使只有一个主体也要返回；若文本没有可执行事项，actors 返回空数组并在 warnings 说明。
8. 不生成具体任务，不猜测当前用户属于哪个主体，不自动选择主体。

输出必须严格匹配以下 JSON Schema：
{json.dumps(schema, ensure_ascii=False)}"""
    user = f"请识别下面文本中的全部责任主体，供用户下一步选择。\n\n原始内容：\n{content}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
