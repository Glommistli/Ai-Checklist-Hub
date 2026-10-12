import json
from datetime import UTC, datetime
from fastapi import HTTPException
from sqlalchemy import select
from app.models import Item
from app.models.workflow import ReplanProposal
from app.schemas import ParsedPlan
from .agent_parser import AgentOptions
from .parser import parse_content
from .workflow import tree, snapshot_item, capture, checksum, record_action, source_for, is_active

EDIT_FIELDS=["title","description","due_date","estimated_minutes","acceptance_criteria","notes"]
def propose(db,root):
    if root.completed:raise HTTPException(409,"已完成目标受到保护，请对未完成目标重新规划")
    rows=[snapshot_item(i) for i in tree(db,root) if is_active(i)]
    source=source_for(db,root)
    context=("只重新规划用户选中的目标（标题、来源和当前进度均在用户消息中）。保留已经完成的任务。对于对应现有任务，请在 metadata.existing_item_id 标明原 id；"
             "需要合并未完成任务时，可在 metadata.existing_item_ids 给出原 id 数组。新增工作不得编造。用户手动修改的字段不得改写。")
    content="来源原文：\n"+source["content"]+"\n当前任务与进度：\n"+json.dumps(rows,ensure_ascii=False)
    plan=parse_content(content,db,force_llm=True,agent_options=AgentOptions(user_context=context,only_user_actions=False))
    existing={i["id"]:i for i in rows};used=set();operations=[]
    def walk(parsed,parent_key=None,first_root=False):
        data=parsed.model_dump(mode="json")
        ids=data["metadata"].get("existing_item_ids",[])
        if ids:
            if not isinstance(ids,list) or any(type(i) is not int for i in ids) or len(ids)<2 or len(set(ids))!=len(ids) or any(i not in existing or i in used for i in ids):
                raise HTTPException(400,"模型返回无效的合并范围")
            if len({existing[i]["parent_id"] for i in ids})!=1:raise HTTPException(400,"只允许合并同级任务")
            if any(existing[i]["completed"] or (existing[i]["metadata"].get("manual_fields") or any(existing[i]["metadata"].get(field) for field in ["evidence","result_note","delay_reason"])) for i in ids):
                operations.append({"kind":"preserved","title":parsed.title,"reason":"合并涉及已完成或手动修改任务，已保护"});return
            used.update(ids);key=len(operations)
            operations.append({"kind":"merge","ids":ids,"title":parsed.title,"data":data,"parent_key":parent_key})
        else:
            id=data["metadata"].get("existing_item_id")
            if id is not None and (type(id) is not int or id not in existing):raise HTTPException(400,"模型引用了范围外的任务")
            if id is None:
                matches=[i["id"] for i in rows if i["title"]==parsed.title and i["id"] not in used]
                id=matches[0] if len(matches)==1 else (root.id if first_root and root.id not in used else None)
            if id in used:raise HTTPException(400,"模型重复引用任务")
            key=len(operations)
            if id is None:
                operations.append({"kind":"add","title":parsed.title,"data":data,"parent_key":parent_key})
            else:
                used.add(id);old=existing[id]
                if old["completed"]:
                    operations.append({"kind":"preserved","id":id,"title":old["title"],"reason":"已完成，保留进度"});return
                else:
                    baseline=old["metadata"].get("ai_baseline") or {}
                    manual=set(old["metadata"].get("manual_fields",[]))
                    changes={};protected=[];protected_changes={}
                    for field in EDIT_FIELDS:
                        value=data.get(field)
                        if old.get(field)==value:continue
                        if field in manual or field not in baseline or old.get(field)!=baseline.get(field):
                            protected.append(field);protected_changes[field]={"before":old.get(field),"after":value}
                        else:changes[field]={"before":old.get(field),"after":value}
                    operations.append({"kind":"update","id":id,"title":old["title"],"changes":changes,"protected":protected,"protected_changes":protected_changes,"data":data,"parent_key":parent_key})
        for child in parsed.subtasks:walk(child,key)
    first=True
    for group in plan.lists:
        for parsed in group.items:
            walk(parsed,None,first);first=False
    proposal=ReplanProposal(item_id=root.id,baseline={"rows":rows,"checksum":checksum(rows)},
        operations=operations,provider=plan.provider,model=plan.model)
    db.add(proposal);db.commit()
    return proposal

def apply(db,proposal,selected,overrides=None):
    overrides=overrides or {}
    from .repository import set_tags, _refresh_ancestors
    from datetime import date
    if proposal.status!="preview":raise HTTPException(409,"该提议已处理，不能重复应用")
    root=db.get(Item,proposal.item_id)
    if not root or not is_active(root):raise HTTPException(409,"原任务已删除")
    current=[snapshot_item(i) for i in tree(db,root) if is_active(i)]
    if checksum(current)!=proposal.baseline["checksum"]:raise HTTPException(409,"任务在预览后发生变化，请重新生成提议")
    operations=proposal.operations
    if len(set(selected))!=len(selected) or any(i<0 or i>=len(operations) for i in selected):raise HTTPException(400,"选择范围无效")
    before=capture(db);mapping={};created=updated=merged=0
    for index,op in enumerate(operations):
        if op.get("id"):mapping[index]=op["id"]
    for index in sorted(selected):
        op=operations[index]
        if op["kind"]=="preserved":continue
        if op["kind"]=="update":
            item=db.get(Item,op["id"])
            baseline=dict((item.metadata_ or {}).get("ai_baseline",{}))
            changes=dict(op["changes"])
            for field in overrides.get(str(index),[]):
                if field not in op.get("protected_changes",{}):raise HTTPException(400,"替换字段不在提议范围内")
                changes[field]=op["protected_changes"][field]
            for field,change in changes.items():
                value=change["after"]
                if field=="due_date" and value:value=date.fromisoformat(value)
                setattr(item,field,value);baseline[field]=change["after"]
            item.metadata_={**(item.metadata_ or {}),"ai_baseline":baseline,"llm_provider":proposal.provider,"llm_model":proposal.model,"replan_proposal":proposal.id}
            updated+=bool(changes)
        else:
            parent_key=op.get("parent_key")
            if parent_key is not None and parent_key not in mapping:raise HTTPException(400,"请同时选择新增子任务的父任务")
            data=op["data"]
            parent_id=mapping[parent_key] if parent_key is not None else root.parent_id
            item=Item(title=data["title"],description=data.get("description"),notes=data.get("notes"),acceptance_criteria=data.get("acceptance_criteria"),
                due_date=date.fromisoformat(data["due_date"]) if data.get("due_date") else None,estimated_minutes=data.get("estimated_minutes"),
                list_id=root.list_id,plan_id=root.plan_id,parent_id=parent_id,source=root.source,position=index,
                metadata_={**(root.metadata_ or {}),"source_excerpt":data.get("source_excerpt"),"ai_baseline":{k:data.get(k) for k in EDIT_FIELDS},"manual_fields":[],"steps":data.get("steps",[]),"conditional_actions":data.get("conditional_actions",[]),"dependencies":data.get("dependencies",[]),"replan_proposal":proposal.id,"llm_provider":proposal.provider,"llm_model":proposal.model})
            db.add(item);db.flush();set_tags(db,item,data.get("tags",[]));mapping[index]=item.id;created+=1
            if op["kind"]=="merge":
                group="replan-"+str(proposal.id)+"-"+str(index)
                for old_id in op["ids"]:
                    old=db.get(Item,old_id)
                    for child in db.scalars(select(Item).where(Item.parent_id==old_id)):child.parent_id=item.id
                    old.metadata_={**(old.metadata_ or {}),"deleted_at":datetime.now(UTC).isoformat(),"trash_group":group,"trash_root":old.id}
                merged+=1
    db.flush()
    for id in mapping.values():
        node=db.get(Item,id)
        if node:_refresh_ancestors(db,node.parent_id,datetime.now(UTC).replace(tzinfo=None))
    record_action(db,"replan",before,"应用 AI 重新规划："+root.title)
    proposal.status="applied";db.commit()
    return {"created":created,"updated":updated,"merged":merged}
