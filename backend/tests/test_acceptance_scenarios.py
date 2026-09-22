"""Representative end-to-end acceptance scenarios for the task-planning agent."""


def _enable_provider(client, monkeypatch, env_name: str, provider_name: str):
    monkeypatch.setenv(env_name, "sk-test-only")
    response = client.post(
        "/api/settings/providers",
        json={
            "name": provider_name,
            "base_url": "https://example.invalid/v1",
            "model": "acceptance-model",
            "api_key_env": env_name,
            "enabled": True,
        },
    )
    assert response.status_code == 201, response.text


def test_case_1_algorithm_problem_ids_become_real_subtasks(client, monkeypatch):
    """An algorithm goal stays concise while explicit problem IDs become children."""
    from app.schemas import ParsedItem, ParsedList, ParsedPlan
    from app.services.parser import OpenAICompatibleProvider

    _enable_provider(client, monkeypatch, "TEST_CASE_1_KEY", "算法用例模型")

    def fake_parse(self, content, options=None):
        return ParsedPlan(
            title="二叉树 BFS 学习",
            lists=[
                ParsedList(
                    name="算法",
                    items=[
                        ParsedItem(
                            title="完成二叉树 BFS 两题与模板默写",
                            description="完成两道层序遍历题，并整理一份可复用的 BFS 模板。",
                            steps=["完成 LC102", "完成 LC199", "在 LeetCode 提交并确认通过"],
                        )
                    ],
                )
            ],
        )

    monkeypatch.setattr(OpenAICompatibleProvider, "parse", fake_parse)
    parsed = client.post(
        "/api/import/parse",
        json={
            "content": "完成二叉树 BFS：LC102、LC199，并默写模板。",
            "llm_mode": "force",
            "granularity": "compact",
        },
    )
    assert parsed.status_code == 200, parsed.text
    plan = parsed.json()
    parent = plan["lists"][0]["items"][0]
    assert plan["task_count"] == 1
    assert [child["title"] for child in parent["subtasks"]] == ["完成 LC102", "完成 LC199"]
    assert parent["steps"] == []
    assert "提交并确认通过" in parent["acceptance_criteria"]

    confirmed = client.post(
        "/api/import/confirm",
        json={"plan": plan, "source_name": "验收测试-算法", "raw_content": "算法测试"},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["created"] == 3
    items = client.get("/api/items").json()
    saved_parent = next(item for item in items if item["title"] == parent["title"])
    saved_children = [item for item in items if item["parent_id"] == saved_parent["id"]]
    assert {item["title"] for item in saved_children} == {"完成 LC102", "完成 LC199"}


def test_case_2_multi_actor_notice_filters_to_selected_identity(client, monkeypatch):
    """All actors are offered first; only the selected actor's actions are imported."""
    from app.schemas import ActorAnalysis, ActorOption, ParsedItem, ParsedList, ParsedPlan
    from app.services.parser import OpenAICompatibleProvider

    _enable_provider(client, monkeypatch, "TEST_CASE_2_KEY", "多主体用例模型")

    def fake_actor_analysis(self, content):
        return ActorAnalysis(
            summary="通知包含三个责任主体",
            actors=[
                ActorOption(name="团支书", description="审核转入申请", estimated_task_count=1),
                ActorOption(name="团员", description="提交本人转入申请", estimated_task_count=2),
                ActorOption(name="团务指导部工作人员", description="处理密码与答疑", estimated_task_count=1),
            ],
        )

    def fake_parse(self, content, options=None):
        return ParsedPlan(
            title="团组织关系转接",
            lists=[
                ParsedList(
                    name="转接事项",
                    items=[
                        ParsedItem(title="审核本班团员转入申请", assignee_role="团支书"),
                        ParsedItem(title="提交本人团组织关系转入申请", assignee_role="团员"),
                        ParsedItem(title="忘记密码时填写登记表", assignee_role="团员"),
                        ParsedItem(title="处理密码重置与问题答疑", assignee_role="团务指导部工作人员"),
                        ParsedItem(title="9月28日前完成转接", assignee_role="所有人"),
                    ],
                )
            ],
        )

    monkeypatch.setattr(OpenAICompatibleProvider, "analyze_actors", fake_actor_analysis)
    monkeypatch.setattr(OpenAICompatibleProvider, "parse", fake_parse)

    actors = client.post("/api/import/actors", json={"content": "团组织关系转接通知"})
    assert actors.status_code == 200, actors.text
    assert [actor["name"] for actor in actors.json()["actors"]] == [
        "团支书",
        "团员",
        "团务指导部工作人员",
    ]

    parsed = client.post(
        "/api/import/parse",
        json={
            "content": "团组织关系转接通知",
            "llm_mode": "force",
            "user_context": "普通团员",
            "only_user_actions": True,
            "granularity": "compact",
        },
    )
    assert parsed.status_code == 200, parsed.text
    plan = parsed.json()
    titles = [item["title"] for task_list in plan["lists"] for item in task_list["items"]]
    assert titles == [
        "提交本人团组织关系转入申请",
        "忘记密码时填写登记表",
        "9月28日前完成转接",
    ]
    assert all("审核" not in title and "处理密码重置" not in title for title in titles)
    assert any("已排除" in warning for warning in plan["warnings"])


def test_case_3_mixed_daily_plan_stays_compact_and_importable(client, monkeypatch):
    """Mixed algorithm, backend and administrative work remains concise and complete."""
    from app.schemas import ParsedItem, ParsedList, ParsedPlan
    from app.services.parser import OpenAICompatibleProvider

    _enable_provider(client, monkeypatch, "TEST_CASE_3_KEY", "混合日程用例模型")
    verbose = (
        "先阅读相关资料并理解背景，然后完成实现和必要验证，最后整理输出结果；"
        "过程中记录所有细节、可能的问题、处理方式以及后续可以继续优化的方向。"
    )

    def fake_parse(self, content, options=None):
        return ParsedPlan(
            title="完整学习日",
            lists=[
                ParsedList(
                    name="今日核心目标",
                    items=[
                        ParsedItem(title="完成滑动窗口练习", description=verbose, steps=["完成 LC3"]),
                        ParsedItem(
                            title="完成用户查询接口",
                            description=verbose,
                            steps=["实现 GET /users", "补充接口测试", "运行测试用例验证结果"],
                        ),
                        ParsedItem(title="提交周报与明日计划", description=verbose),
                    ],
                )
            ],
        )

    monkeypatch.setattr(OpenAICompatibleProvider, "parse", fake_parse)
    parsed = client.post(
        "/api/import/parse",
        json={
            "content": "今天完成算法题、后端接口和周报。",
            "llm_mode": "force",
            "granularity": "compact",
            "max_top_level_tasks": 4,
        },
    )
    assert parsed.status_code == 200, parsed.text
    plan = parsed.json()
    items = plan["lists"][0]["items"]
    assert plan["task_count"] == 3
    assert [item["title"] for item in items] == [
        "完成滑动窗口练习",
        "完成用户查询接口",
        "提交周报与明日计划",
    ]
    assert all(len(item["description"] or "") <= 72 for item in items)
    assert all(len(item["steps"]) <= 2 for item in items)
    assert items[0]["subtasks"][0]["title"] == "完成 LC3"
    assert all("full_description" in item["metadata"] for item in items)

    confirmed = client.post(
        "/api/import/confirm",
        json={"plan": plan, "source_name": "验收测试-混合日程", "raw_content": "混合任务测试"},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["created"] == 4
    saved = client.get("/api/items").json()
    assert {item["title"] for item in saved} == {
        "完成滑动窗口练习",
        "完成 LC3",
        "完成用户查询接口",
        "提交周报与明日计划",
    }
