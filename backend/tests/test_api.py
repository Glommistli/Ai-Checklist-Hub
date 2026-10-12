from app.models import Checklist, Item


def test_list_and_item_crud(client):
    lst = client.post("/api/lists", json={"name":"Work","metadata":{}})
    assert lst.status_code == 201
    list_id = lst.json()["id"]
    item = client.post("/api/items", json={"title":"Fix API bug","list_id":list_id,"tags":["bug"],"metadata":{}})
    assert item.status_code == 201
    item_id = item.json()["id"]
    edited = client.put(f"/api/items/{item_id}", json={
        "notes": "Reproduce first", "start_date": "2026-10-01",
        "due_date": "2026-10-03", "estimated_minutes": 150,
    }).json()
    assert edited["notes"] == "Reproduce first"
    assert edited["start_date"] == "2026-10-01"
    assert edited["due_date"] == "2026-10-03"
    assert edited["estimated_minutes"] == 150
    assert client.post(f"/api/items/{item_id}/complete").json()["completed"] is True
    assert client.post(f"/api/items/{item_id}/complete").json()["completed"] is False
    assert len(client.get("/api/items", params={"q":"API"}).json()) == 1
    assert client.delete(f"/api/items/{item_id}").status_code == 204
    assert client.delete(f"/api/lists/{list_id}").status_code == 204


def test_item_pagination_keeps_large_imported_lists_visible(client, db):
    imported = Checklist(name="大型自动规划")
    db.add(imported)
    db.flush()
    db.add_all([
        Item(title=f"自动任务 {index + 1}", list_id=imported.id, position=index, metadata_={})
        for index in range(1205)
    ])
    db.commit()

    first = client.get("/api/items", params={"limit": 1000, "offset": 0})
    second = client.get("/api/items", params={"limit": 1000, "offset": 1000})
    assert first.status_code == 200
    assert second.status_code == 200
    combined = first.json() + second.json()
    assert len(first.json()) == 1000
    assert len(second.json()) == 205
    assert len({item["id"] for item in combined}) == 1205
    assert {item["list_id"] for item in combined} == {imported.id}


def test_plan_crud(client):
    plan = client.post("/api/plans", json={"title":"Japan Trip","metadata":{}})
    assert plan.status_code == 201
    pid = plan.json()["id"]
    assert client.put(f"/api/plans/{pid}", json={"status":"archived"}).json()["status"] == "archived"
    assert client.delete(f"/api/plans/{pid}").status_code == 204


def test_import_preview_confirm_and_duplicates(client):
    text = "发布 1.2.0 前：\n完成测试\n更新 changelog\nbuild production\npush tag\n部署"
    parsed = client.post("/api/import/parse", json={"content":text,"use_llm":False}).json()
    assert len(parsed["lists"][0]["items"]) == 5
    result = client.post("/api/import/confirm", json={"plan":parsed,"source_name":"test","raw_content":text,"duplicate_strategy":"skip"})
    assert result.json()["created"] == 5
    assert len(result.json()["item_ids"]) == 5
    assert len(result.json()["list_ids"]) == 1
    imported = client.get("/api/items", params={"list_id": result.json()["list_ids"][0]}).json()
    assert len(imported) == 5
    second = client.post("/api/import/parse", json={"content":text,"use_llm":False}).json()
    assert second["duplicate_count"] == 5
    again = client.post("/api/import/confirm", json={"plan":second,"source_name":"test","raw_content":text,"duplicate_strategy":"skip"})
    assert again.json()["skipped"] == 5
    assert again.json()["created"] == 0
    assert again.json()["list_ids"] == []
    assert again.json()["item_ids"] == []


def test_move_item(client):
    a = client.post("/api/lists", json={"name":"A","metadata":{}}).json()
    b = client.post("/api/lists", json={"name":"B","metadata":{}}).json()
    item = client.post("/api/items", json={"title":"Move me","list_id":a["id"],"metadata":{}}).json()
    moved = client.put(f"/api/items/{item['id']}", json={"list_id":b["id"]}).json()
    assert moved["list_id"] == b["id"]


def test_ui_settings_persist_sidebar_width(client):
    updated = client.put("/api/settings/ui", json={"sidebar_width": 372})
    assert updated.status_code == 200
    assert updated.json() == {"sidebar_width": 372}

    loaded = client.get("/api/settings/ui")
    assert loaded.status_code == 200
    assert loaded.json() == {"sidebar_width": 372}


def test_generic_webhook_import(client):
    response = client.post("/api/webhooks/import", json={"content":"购物：\n牛奶\n鸡蛋","source_name":"automation","use_llm":False})
    assert response.status_code == 200
    assert response.json()["created"] == 2


def test_import_persists_shared_tags_without_rolling_back(client):
    parsed = {
        "title": "学习计划",
        "lists": [{"name": "学习", "items": [
            {"title": "任务一", "tags": ["AI"], "steps": [], "subtasks": [], "conditional_actions": [], "dependencies": [], "metadata": {}, "duplicate": False},
            {"title": "任务二", "tags": ["AI"], "steps": [], "subtasks": [], "conditional_actions": [], "dependencies": [], "metadata": {}, "duplicate": False},
        ]}],
    }
    result = client.post("/api/import/confirm", json={"plan": parsed, "source_name": "test", "raw_content": "x"})
    assert result.status_code == 200, result.text
    assert result.json()["created"] == 2
    items = client.get("/api/items", params={"tag": "ai"}).json()
    assert len(items) == 2


def test_import_normalizes_model_priority_before_item_list_serialization(client):
    parsed = {
        "title": "模型任务",
        "lists": [{"name": "今日任务", "items": [
            {"title": "普通优先级任务", "priority": "normal"},
            {"title": "中文高优先级任务", "priority": "高"},
            {"title": "未知优先级任务", "priority": "occasionally"},
        ]}],
    }
    result = client.post("/api/import/confirm", json={"plan": parsed, "source_name": "test", "raw_content": "x"})
    assert result.status_code == 200, result.text
    items = client.get("/api/items")
    assert items.status_code == 200, items.text
    priorities = {item["title"]: item["priority"] for item in items.json()}
    assert priorities["普通优先级任务"] == "medium"
    assert priorities["中文高优先级任务"] == "high"
    assert priorities["未知优先级任务"] is None


def test_force_llm_calls_enabled_provider_for_structured_text(client, monkeypatch):
    from app.schemas import ParsedItem, ParsedList, ParsedPlan
    from app.services.parser import OpenAICompatibleProvider

    monkeypatch.setenv("TEST_FORCE_API_KEY", "sk-test-only")
    provider = client.post("/api/settings/providers", json={
        "name": "Test AI",
        "base_url": "https://example.invalid/v1",
        "model": "test-model",
        "api_key_env": "TEST_FORCE_API_KEY",
        "enabled": True,
    })
    assert provider.status_code == 201
    calls = []

    def fake_parse(self, content, options=None):
        calls.append(content)
        return ParsedPlan(title="AI 计划", goal="完成发布", parser="agent_v2", confidence=0.95, lists=[
            ParsedList(name="AI 清单", items=[ParsedItem(title="AI 任务")])
        ])

    monkeypatch.setattr(OpenAICompatibleProvider, "parse", fake_parse)
    response = client.post("/api/import/parse", json={
        "content": "清晰清单：\n- 第一项\n- 第二项",
        "llm_mode": "force",
    })
    assert response.status_code == 200, response.text
    assert calls == ["清晰清单：\n- 第一项\n- 第二项"]
    assert response.json()["parser"] == "agent_v2"
    assert response.json()["lists"][0]["items"][0]["title"] == "AI 任务"


def test_duplicate_provider_returns_clear_conflict(client):
    payload = {
        "name": "DeepSeek", "base_url": "https://api.deepseek.com",
        "model": "deepseek-flash", "enabled": True,
    }
    first = client.post("/api/settings/providers", json=payload)
    assert first.status_code == 201, first.text

    duplicate = client.post("/api/settings/providers", json={**payload, "name": "  deepseek  "})
    assert duplicate.status_code == 409, duplicate.text
    assert "已存在" in duplicate.json()["detail"]
    providers = client.get("/api/settings/providers").json()
    assert len(providers) == 1
    assert providers[0]["enabled"] is True


def test_force_llm_reports_missing_provider(client):
    response = client.post("/api/import/parse", json={"content": "请帮我整理任务", "llm_mode": "force"})
    assert response.status_code == 400
    assert "没有可用的大模型" in response.json()["detail"]


def test_auto_mode_uses_agent_for_role_specific_notice(client, monkeypatch):
    from app.schemas import ParsedItem, ParsedList, ParsedPlan
    from app.services.parser import OpenAICompatibleProvider

    monkeypatch.setenv("TEST_ROLE_API_KEY", "sk-test-only")
    client.post("/api/settings/providers", json={
        "name": "Role Agent", "base_url": "https://example.invalid/v1", "model": "test-model",
        "api_key_env": "TEST_ROLE_API_KEY", "enabled": True,
    })
    calls = []

    def role_parse(self, content, options=None):
        calls.append((content, options.user_context, options.only_user_actions))
        return ParsedPlan(title="角色通知", lists=[ParsedList(name="个人事项", items=[
            ParsedItem(title="完成个人转入", assignee_role="团员")
        ])])

    monkeypatch.setattr(OpenAICompatibleProvider, "parse", role_parse)
    response = client.post("/api/import/parse", json={
        "content": "团支书需审核申请，班级团员必须完成转入。",
        "llm_mode": "auto",
        "user_context": "普通团员",
        "only_user_actions": True,
    })
    assert response.status_code == 200, response.text
    assert calls == [("团支书需审核申请，班级团员必须完成转入。", "普通团员", True)]


def test_actor_analysis_returns_all_roles_before_task_generation(client, monkeypatch):
    from app.schemas import ActorAnalysis, ActorOption
    from app.services.parser import OpenAICompatibleProvider

    monkeypatch.setenv("TEST_ACTOR_API_KEY", "sk-test-only")
    client.post("/api/settings/providers", json={
        "name": "Actor Agent", "base_url": "https://example.invalid/v1", "model": "test-model",
        "api_key_env": "TEST_ACTOR_API_KEY", "enabled": True,
    })
    calls = []

    def actor_analysis(self, content):
        calls.append(content)
        return ActorAnalysis(summary="通知涉及三个责任主体", actors=[
            ActorOption(name="团支书", description="审核团员申请", evidence=["团支书对申请进行审核"], estimated_task_count=2),
            ActorOption(name="团员（个人）", description="提交本人转入申请", evidence=["团员提交转入申请"], estimated_task_count=1),
            ActorOption(name="团务指导部工作人员", description="处理密码和咨询", evidence=["工作人员进行解答"], estimated_task_count=1),
        ])

    monkeypatch.setattr(OpenAICompatibleProvider, "analyze_actors", actor_analysis)
    response = client.post("/api/import/actors", json={
        "content": "团支书审核申请；团员提交申请；工作人员处理咨询。",
    })
    assert response.status_code == 200, response.text
    assert calls == ["团支书审核申请；团员提交申请；工作人员处理咨询。"]
    assert [actor["name"] for actor in response.json()["actors"]] == ["团支书", "团员（个人）", "团务指导部工作人员"]
    assert response.json()["provider"] == "Actor Agent"


def test_granularity_normalizer_absorbs_conditions_and_caps_tasks(client, monkeypatch):
    from app.schemas import ParsedItem, ParsedList, ParsedPlan
    from app.services.parser import OpenAICompatibleProvider

    monkeypatch.setenv("TEST_AGENT_KEY", "sk-test-only")
    client.post("/api/settings/providers", json={
        "name": "Test Agent", "base_url": "https://example.invalid/v1", "model": "test-model",
        "api_key_env": "TEST_AGENT_KEY", "enabled": True,
    })

    def fragmented(self, content, options=None):
        return ParsedPlan(title="发布计划", lists=[ParsedList(name="发布", items=[
            ParsedItem(title="运行测试"), ParsedItem(title="检查结果"), ParsedItem(title="备份数据库"),
            ParsedItem(title="发布版本"), ParsedItem(title="观察日志"),
            ParsedItem(title="如果出现严重错误则回滚", steps=["通知负责人"]),
        ])])

    monkeypatch.setattr(OpenAICompatibleProvider, "parse", fragmented)
    result = client.post("/api/import/parse", json={
        "content": "发布描述", "llm_mode": "force", "granularity": "balanced", "max_top_level_tasks": 3,
    })
    assert result.status_code == 200, result.text
    plan = result.json()
    assert plan["task_count"] == 3
    assert plan["step_count"] >= 2
    assert any(item["conditional_actions"] for item in plan["lists"][0]["items"])
    condition = next(item["conditional_actions"][0] for item in plan["lists"][0]["items"] if item["conditional_actions"])
    assert isinstance(condition["actions"], list)
    assert plan["warnings"]


def test_confirm_persists_steps_and_subtasks(client):
    parsed = {
        "title": "Agent 计划", "parser": "agent_v2", "granularity": "balanced",
        "lists": [{"name": "交付", "items": [{
            "title": "完成发布前验证", "steps": ["运行测试"],
            "subtasks": [{"title": "备份数据库"}],
            "conditional_actions": [{"condition": "测试失败", "actions": ["停止发布"]}],
            "dependencies": [], "tags": [], "metadata": {}, "duplicate": False,
        }]}],
    }
    response = client.post("/api/import/confirm", json={"plan": parsed, "source_name": "test", "raw_content": "x"})
    assert response.status_code == 200, response.text
    assert response.json()["created"] == 2
    items = client.get("/api/items").json()
    parent = next(item for item in items if item["title"] == "完成发布前验证")
    child = next(item for item in items if item["title"] == "备份数据库")
    assert parent["metadata"]["steps"] == ["运行测试"]
    assert parent["metadata"]["conditional_actions"][0]["condition"] == "测试失败"
    assert child["parent_id"] == parent["id"]
