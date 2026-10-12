import pytest
from app.schemas import ParsedPlan, ParsedList, ParsedItem
from app.services.agent_parser import AgentOptions, build_agent_messages
from app.services.granularity import normalize_plan

def add(client, title, parent=None):
    response=client.post('/api/items',json={'title':title,'parent_id':parent})
    assert response.status_code==201
    return response.json()['id']

def states(client):
    return {r['id']:r for r in client.get('/api/items').json()}

def test_parent_completion_and_reopen_cascade_all_depths(client):
    p=add(client,'父');c=add(client,'子',p);g=add(client,'孙',c);s=add(client,'兄弟',p)
    assert client.post(f'/api/items/{p}/complete').status_code==200
    rows=states(client)
    assert all(rows[x]['completed'] and rows[x]['completed_at'] for x in [p,c,g,s])
    assert client.put(f'/api/items/{p}',json={'completed':False}).status_code==200
    rows=states(client)
    assert all(not rows[x]['completed'] and rows[x]['completed_at'] is None for x in [p,c,g,s])

def test_child_updates_ancestors_without_changing_siblings(client):
    p=add(client,'父');a=add(client,'甲',p);b=add(client,'乙',p);g=add(client,'孙',a)
    client.put(f'/api/items/{g}',json={'completed':True})
    rows=states(client)
    assert rows[a]['completed'] and not rows[p]['completed']
    client.put(f'/api/items/{b}',json={'completed':True})
    assert states(client)[p]['completed']
    client.post(f'/api/items/{g}/complete')
    rows=states(client)
    assert not rows[p]['completed'] and not rows[a]['completed'] and rows[b]['completed']

def test_add_delete_and_reparent_refresh_parent_status(client):
    p=add(client,'父');a=add(client,'已完成子',p)
    client.put(f'/api/items/{a}',json={'completed':True})
    assert states(client)[p]['completed']
    b=add(client,'新子',p)
    assert not states(client)[p]['completed']
    client.delete(f'/api/items/{b}')
    assert states(client)[p]['completed']
    q=add(client,'另一父');c=add(client,'移动子',q)
    client.put(f'/api/items/{c}',json={'parent_id':p})
    assert not states(client)[p]['completed']

def test_cycles_and_missing_parents_rejected_without_changes(client):
    p=add(client,'父');c=add(client,'子',p)
    assert client.put(f'/api/items/{p}',json={'parent_id':c}).status_code==400
    assert client.put(f'/api/items/{p}',json={'parent_id':p}).status_code==400
    assert client.post('/api/items',json={'title':'无效','parent_id':999999}).status_code==400
    assert states(client)[p]['parent_id'] is None

def test_auto_preserves_model_units_steps_and_details_without_cap():
    items=[ParsedItem(title=f'独立成果{i}',description='详细说明'*40,steps=['完成 LC102','口头解释设计取舍']) for i in range(12)]
    plan=normalize_plan(ParsedPlan(title='计划',lists=[ParsedList(name='项目',items=items)]),AgentOptions(max_top_level_tasks=1,steps_as_subtasks=True))
    assert plan.granularity=='auto' and plan.task_count==12
    assert len(plan.lists[0].items[0].description)>72
    assert plan.lists[0].items[0].steps==['完成 LC102','口头解释设计取舍']
    assert plan.lists[0].items[0].subtasks==[]

def test_auto_prompt_has_no_fixed_task_count():
    prompt=build_agent_messages('任务内容',{},AgentOptions())[0]['content']
    assert '自行判断合理任务粒度' in prompt
    assert '2～5' not in prompt and '3～8' not in prompt and '软上限' not in prompt

def test_other_roles_filtered_recursively():
    task=ParsedItem(title='收件人任务',subtasks=[ParsedItem(title='管理员任务',assignee_role='管理员'),ParsedItem(title='我的子任务',assignee_role='学生')])
    plan=normalize_plan(ParsedPlan(title='计划',lists=[ParsedList(name='项目',items=[task])]),AgentOptions(user_context='学生'))
    assert [x.title for x in plan.lists[0].items[0].subtasks]==['我的子任务']
