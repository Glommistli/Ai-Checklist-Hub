from datetime import date, datetime
import httpx
import pytest
from sqlalchemy import select, func
from fastapi import HTTPException
from app.models import Item, Checklist, Plan, RawContent, EmailIngestion
from app.models.workflow import ModelCall, ReplanProposal
from app.schemas import ItemCreate, ItemUpdate, ParsedPlan, ParsedList, ParsedItem, ImportConfirmRequest
from app.services import workflow as wf, repository as repo, replanning as rp, model_calls as calls
from app.services.importer import confirm_parsed_plan
from app.services import gmail_service as gmail
from app.services.parser import LLMProviderError
from app.core.utc_json import utc_json
from test_gmail import FakeGmailClient, payload


def make(db,title="Goal",**kwargs):
    return repo.create_item(db,ItemCreate(title=title,list_id=1,metadata={},**kwargs))

def plan(*items):
    return ParsedPlan(title="Plan",lists=[ParsedList(name="Work",items=list(items))],provider="Mock",model="model",confidence=.95)

def test_tree_trash_restore_and_root_statistics(client,db):
    root=make(db);child=make(db,"Child",parent_id=root.id)
    assert client.get("/api/lists").json()[0]["item_count"]==1
    assert client.delete(f"/api/items/{root.id}").status_code==204
    assert client.get("/api/items").json()==[]
    rows=client.get("/api/workspace/trash").json();assert len(rows)==1 and rows[0]["id"]==root.id
    assert client.post(f"/api/workspace/trash/item/{root.id}/restore").status_code==200
    assert len(client.get("/api/items").json())==2


def test_list_trash_restores_tree(client,db):
    listing=Checklist(name="Project");db.add(listing);db.commit()
    root=repo.create_item(db,ItemCreate(title="Root",list_id=listing.id))
    assert client.delete(f"/api/lists/{listing.id}").status_code==204
    assert len(client.get("/api/workspace/trash").json())==1
    assert client.post(f"/api/workspace/trash/list/{listing.id}/restore").status_code==200
    assert client.get("/api/items").json()[0]["id"]==root.id


def test_complete_undo_restores_mixed_child_progress(db):
    root=make(db);a=make(db,"A",parent_id=root.id);b=make(db,"B",parent_id=root.id,completed=True)
    repo.update_item(db,root,ItemUpdate(completed=True));assert a.completed and b.completed
    wf.undo(db);assert not root.completed and not a.completed and b.completed


def test_undo_conflict_preserves_later_external_edit(db):
    root=make(db);repo.update_item(db,root,ItemUpdate(title="AI change"))
    root.title="Latest external change";db.commit()
    with pytest.raises(HTTPException) as error:wf.undo(db)
    assert error.value.status_code==409 and root.title=="Latest external change"


def test_delete_undo_and_restore_conflict(db):
    root=make(db);child=make(db,"Child",parent_id=root.id)
    wf.soft_delete(db,child);wf.soft_delete(db,root)
    with pytest.raises(HTTPException):wf.restore(db,"item",child.id)
    wf.undo(db);assert wf.is_active(root) and not wf.is_active(child)
    wf.restore(db,"item",child.id);assert wf.is_active(child)


def test_parent_context_dedup_and_new_children(db):
    first=plan(ParsedItem(title="A",subtasks=[ParsedItem(title="Review")]),ParsedItem(title="B",subtasks=[ParsedItem(title="Review")]))
    result=confirm_parsed_plan(ImportConfirmRequest(plan=first,source_name="test",external_id="source"),db)
    assert result["created"]==4
    second=plan(ParsedItem(title="A",subtasks=[ParsedItem(title="Review"),ParsedItem(title="New child")]),ParsedItem(title="B",subtasks=[ParsedItem(title="Review")]))
    r=confirm_parsed_plan(ImportConfirmRequest(plan=second,source_name="test",external_id="source"),db)
    assert r["created"]==1 and r["skipped"]==4
    assert db.scalar(select(func.count(Plan.id)))==1
    assert db.scalar(select(func.count(Checklist.id)))==2
    child=db.scalar(select(Item).where(Item.title=="New child"));assert db.get(Item,child.parent_id).title=="A"
    r=confirm_parsed_plan(ImportConfirmRequest(plan=second,source_name="test",external_id="source"),db)
    assert r["created"]==0 and db.scalar(select(func.count(Plan.id)))==1


def test_source_provenance_and_import_undo(db):
    r=confirm_parsed_plan(ImportConfirmRequest(plan=plan(ParsedItem(title="A",metadata={"gmail_url":"malicious"},source_excerpt="Proof")),source_name="test",raw_content="Full original Proof",source_metadata={"gmail_url":"https://mail.google.com/mail/u/0/#all/m","llm_provider":"Mock"}),db)
    item=db.get(Item,r["item_ids"][0]);source=wf.source_for(db,item)
    assert source["content"]=="Full original Proof" and source["gmail_url"].startswith("https:")
    wf.undo(db);assert not wf.is_active(item)


def test_schedule_separate_deadline_cancel_move_and_root(client,db):
    root=make(db,due_date=date(2026,10,12),estimated_minutes=90);child=make(db,"Step",parent_id=root.id,estimated_minutes=40)
    assert client.get("/api/workspace/daily?day=2026-10-12").json()["estimated_minutes"]==90
    client.put(f"/api/workspace/daily/{child.id}",json={"day":"2026-10-13"})
    assert client.get("/api/workspace/daily?day=2026-10-12").json()["planned_ids"]==[]
    assert client.get("/api/workspace/daily?day=2026-10-13").json()["planned_ids"]==[root.id]
    client.put(f"/api/workspace/daily/{root.id}",json={"day":None,"exclude_day":"2026-10-12"})
    assert client.get("/api/workspace/daily?day=2026-10-12").json()["planned_ids"]==[]
    db.refresh(root);assert root.due_date==date(2026,10,12)


def test_capacity_estimate_from_children_and_overdue(db):
    root=make(db,due_date=date(2026,10,12));make(db,"Step",parent_id=root.id,estimated_minutes=300)
    wf.put_preference(db,"daily_capacity",{"minutes":240})
    result=wf.daily(db,date(2026,10,12));assert result["estimated_minutes"]==300 and result["over_capacity"]
    assert wf.daily(db,date(2026,10,13))["overdue_ids"]==[root.id]


def test_weekly_local_timezone_descendant_evidence_and_export(client,db):
    root=make(db);child=make(db,"Step",parent_id=root.id)
    child.metadata_={"evidence":["https://example.com/result"]}
    root.completed=True;root.completed_at=datetime(2026,10,11,18,0);db.commit()
    review=wf.weekly(db,date(2026,10,12))
    assert len(review["completed"])==1 and review["completed"][0]["evidence"]==["https://example.com/result"]
    export=client.get("/api/workspace/review/export?start=2026-10-12")
    assert export.status_code==200 and "https://example.com/result" in export.text


def test_timestamp_conversion_does_not_change_note_content(client,db):
    root=make(db,notes="2026-10-12T12:00:00")
    row=client.get("/api/items").json()[0]
    assert row["created_at"].endswith("Z") and row["notes"]=="2026-10-12T12:00:00"
    assert utc_json({"received_at":"2026-10-12T12:00:00"})["received_at"].endswith("Z")


def test_model_call_usage_quota_and_failure(db,monkeypatch):
    wf.put_preference(db,"ai_controls",{"daily_call_limit":1})
    monkeypatch.setattr(calls.httpx,"post",lambda *a,**kw:httpx.Response(200,json={"usage":{"prompt_tokens":30,"completion_tokens":12}}))
    calls.model_post(db,"https://example.com",provider="test",model="mock")
    call=db.scalar(select(ModelCall));assert call.status=="success" and call.input_tokens==30 and call.output_tokens==12
    with pytest.raises(LLMProviderError):calls.model_post(db,"https://example.com",provider="test",model="mock")
    wf.put_preference(db,"ai_controls",{"daily_call_limit":0})
    def fail(*a,**kw):raise httpx.ConnectError("unreachable")
    monkeypatch.setattr(calls.httpx,"post",fail)
    with pytest.raises(httpx.ConnectError):calls.model_post(db,"https://example.com",provider="test",model="mock")
    assert db.scalars(select(ModelCall).order_by(ModelCall.id)).all()[-1].status=="failed"


def prepared(db):
    root=make(db)
    root.metadata_={"ai_baseline":{key:getattr(root,key) for key in rp.EDIT_FIELDS}}
    db.commit();return root


def test_replan_preview_selective_apply_and_no_duplicate(db,monkeypatch):
    root=prepared(db)
    proposed=plan(ParsedItem(title="Improved",metadata={"existing_item_id":root.id},subtasks=[ParsedItem(title="New step",subtasks=[ParsedItem(title="Child step")])]))
    monkeypatch.setattr(rp,"parse_content",lambda *a,**kw:proposed)
    proposal=rp.propose(db,root);assert root.title=="Goal"
    result=rp.apply(db,proposal,[2,0,1]);assert result["created"]==2 and root.title=="Improved"
    with pytest.raises(HTTPException) as error:rp.apply(db,proposal,[0])
    assert error.value.status_code==409


def test_replan_protects_manual_and_completed_and_explicit_override(db,monkeypatch):
    root=prepared(db);repo.update_item(db,root,ItemUpdate(title="My title"));pending=make(db,"Pending",parent_id=root.id);done=make(db,"Done",parent_id=root.id,completed=True)
    proposed=plan(ParsedItem(title="Model title",metadata={"existing_item_id":root.id},subtasks=[ParsedItem(title="Changed done",metadata={"existing_item_id":done.id},subtasks=[ParsedItem(title="Forbidden")])]))
    monkeypatch.setattr(rp,"parse_content",lambda *a,**kw:proposed)
    proposal=rp.propose(db,root)
    assert proposal.operations[0]["protected_changes"]["title"]["before"]=="My title"
    assert len(proposal.operations)==2 and proposal.operations[1]["kind"]=="preserved"
    rp.apply(db,proposal,[0]);assert root.title=="My title" and done.title=="Done" and done.completed
    proposal=rp.propose(db,root);rp.apply(db,proposal,[0],{"0":["title"]});assert root.title=="Model title"


def test_replan_stale_proposal_conflict(db,monkeypatch):
    root=prepared(db);monkeypatch.setattr(rp,"parse_content",lambda *a,**kw:plan(ParsedItem(title="AI",metadata={"existing_item_id":root.id})))
    proposal=rp.propose(db,root);repo.update_item(db,root,ItemUpdate(notes="New edit"))
    with pytest.raises(HTTPException) as error:rp.apply(db,proposal,[0])
    assert error.value.status_code==409 and root.notes=="New edit"


def test_replan_merge_siblings_retains_children_and_undo(db,monkeypatch):
    root=prepared(db);a=make(db,"A",parent_id=root.id);b=make(db,"B",parent_id=root.id);child=make(db,"Child",parent_id=a.id)
    merged=ParsedItem(title="Merged",metadata={"existing_item_ids":[a.id,b.id]})
    monkeypatch.setattr(rp,"parse_content",lambda *a,**kw:plan(ParsedItem(title=root.title,metadata={"existing_item_id":root.id},subtasks=[merged])))
    proposal=rp.propose(db,root);result=rp.apply(db,proposal,[1])
    assert result["merged"]==1 and not wf.is_active(a) and db.get(Item,child.parent_id).title=="Merged"
    wf.undo(db);assert wf.is_active(a) and wf.is_active(b) and child.parent_id==a.id


@pytest.mark.parametrize("rule,expected",[("example.com",True),("@example.com",True),("sender@example.com",True),("ample.com",False),("other@example.com",False)])
def test_sender_rules_exact_email_and_domain(rule,expected):
    assert gmail.sender_matches("Sender <sender@example.com>",rule)==expected


def test_missing_evidence_never_auto_imports(db,monkeypatch):
    monkeypatch.setattr(gmail,"parse_content",lambda *a,**kw:plan(ParsedItem(title="Action",source_excerpt="Made up evidence")))
    result=gmail.process_message_ids(db,"me@example.com",["m"],FakeGmailClient({"m":payload("m","Action","Please submit results")}))
    assert result["imported"]==0 and result["candidates"]==1


def test_retry_existing_created_tasks_recovers_ledger_without_model(db,monkeypatch):
    gmail.save_gmail_state(db,{"connected":True,"email":"me@example.com"})
    root=make(db);root.source="Gmail:me@example.com";root.metadata_={"gmail_message_id":"m"};db.commit()
    record=EmailIngestion(account_email="me@example.com",message_id="m",status="processing",subject="Action",sender="sender@example.com")
    db.add(record);db.commit()
    def fail(*a,**kw):raise AssertionError("Should not call model")
    monkeypatch.setattr(gmail,"parse_content",fail)
    gmail.retry_ingestion(db,record);assert record.status=="imported"


def test_pending_scoped_account_and_evidence_validation(client,db):
    gmail.save_gmail_state(db,{"email":"me@example.com"})
    for account in ["me@example.com","other@example.com"]:
        db.add(EmailIngestion(account_email=account,message_id=account,status="failed",subject="Action",sender="sender@example.com"))
    db.commit();assert client.get("/api/workspace/pending-count").json()["count"]==1
    root=make(db)
    assert client.put(f"/api/workspace/evidence/{root.id}",json={"links":["javascript:alert(1)"]}).status_code==400


def test_pause_resume_preserves_remaining_message_queue(db,monkeypatch):
    processed=[]
    def parse(*a,**kw):
        processed.append(1)
        if len(processed)==1:gmail.save_gmail_state(db,{"paused":True})
        return plan(ParsedItem(title="Action",source_excerpt="Please submit results"))
    monkeypatch.setattr(gmail,"parse_content",parse)
    fake=FakeGmailClient({mid:payload(mid,"Action","Please submit results") for mid in ["one","two"]})
    first=gmail.process_message_ids(db,"me@example.com",["one","two"],fake)
    assert first["scanned"]==1 and gmail.gmail_state(db)["pending_message_ids"]==["two"]
    gmail.save_gmail_state(db,{"paused":False})
    second=gmail.process_message_ids(db,"me@example.com",["one","two"],fake)
    assert second["already_processed"]==1 and second["scanned"]==1 and len(processed)==2
    assert gmail.gmail_state(db)["pending_message_ids"]==[]


def test_model_invalid_json_is_recorded_without_confidence_floor(db,monkeypatch):
    from app.services.parser import OpenAICompatibleProvider
    monkeypatch.setattr(calls.httpx,"post",lambda *a,**kw:httpx.Response(200,json={"choices":[{"message":{"content":"not JSON"}}]},request=httpx.Request("POST","https://example.com")))
    provider=OpenAICompatibleProvider("https://example.com","mock","model",usage_db=db)
    with pytest.raises(ValueError):provider.parse("Test")
    assert db.scalar(select(ModelCall)).status=="invalid"
    data=plan(ParsedItem(title="Action")).model_dump(mode="json");data["confidence"]=.31
    import json
    monkeypatch.setattr(calls.httpx,"post",lambda *a,**kw:httpx.Response(200,json={"choices":[{"message":{"content":json.dumps(data)}}]},request=httpx.Request("POST","https://example.com")))
    assert provider.parse("Test").confidence==.31


def test_duplicate_parent_new_child_reopens_completed_goal(db):
    initial=plan(ParsedItem(title="Goal",subtasks=[ParsedItem(title="Existing")]))
    result=confirm_parsed_plan(ImportConfirmRequest(plan=initial,source_name="test"),db)
    root=db.get(Item,result["item_ids"][0]);repo.update_item(db,root,ItemUpdate(completed=True))
    revised=plan(ParsedItem(title="Goal",subtasks=[ParsedItem(title="Existing"),ParsedItem(title="New")]))
    confirm_parsed_plan(ImportConfirmRequest(plan=revised,source_name="test"),db)
    assert not root.completed and root.completed_at is None


def test_pending_failed_ignore_and_lock_guard(client,db):
    record=EmailIngestion(account_email="me@example.com",message_id="m",status="failed",subject="Action",sender="sender@example.com")
    db.add(record);db.commit()
    assert gmail._sync_lock.acquire(blocking=False)
    try:assert client.post(f"/api/integrations/gmail/candidates/{record.id}/ignore").status_code==400
    finally:gmail._sync_lock.release()
    assert client.post(f"/api/integrations/gmail/candidates/{record.id}/ignore").status_code==200
    db.refresh(record);assert record.status=="ignored"


def test_list_rename_undo_and_root_stats_consistency(client,db):
    listing=client.post("/api/lists",json={"name":"Original"}).json()
    client.put(f"/api/lists/{listing['id']}",json={"name":"Changed"})
    assert client.post("/api/workspace/undo").status_code==200
    assert any(l["name"]=="Original" for l in client.get("/api/lists").json())
    root=make(db,due_date=date.today());make(db,"Child",parent_id=root.id)
    planned=make(db,"Planned");planned.metadata_={"planned_date":date.today().isoformat()};db.commit()
    stats=client.get("/api/stats").json()
    assert stats["all"]["total"]==2 and stats["today"]["total"]==2


def test_legacy_child_fingerprint_does_not_duplicate_after_upgrade(db):
    request=ImportConfirmRequest(plan=plan(ParsedItem(title="Goal",subtasks=[ParsedItem(title="Step")])),source_name="test",external_id="source")
    result=confirm_parsed_plan(request,db)
    child=db.get(Item,result["item_ids"][1])
    child.fingerprint=repo.fingerprint_for("Step","Work","Plan","source",None);db.commit()
    again=confirm_parsed_plan(request,db)
    assert again["created"]==0 and db.scalar(select(func.count(Item.id)))==2


def test_reparent_moves_descendants_and_rejects_trash_list(client,db):
    first=Checklist(name="First");second=Checklist(name="Second");db.add_all([first,second]);db.commit()
    root=repo.create_item(db,ItemCreate(title="Root",list_id=first.id))
    child=repo.create_item(db,ItemCreate(title="Child",parent_id=root.id))
    grandchild=repo.create_item(db,ItemCreate(title="Grandchild",parent_id=child.id))
    other=repo.create_item(db,ItemCreate(title="Other",list_id=second.id))
    repo.update_item(db,child,ItemUpdate(parent_id=other.id))
    assert child.list_id==second.id and grandchild.list_id==second.id
    assert client.post("/api/items",json={"title":"Wrong","list_id":first.id,"parent_id":other.id}).status_code==400
    wf.soft_delete_list(db,first)
    assert client.post("/api/items",json={"title":"Wrong","list_id":first.id}).status_code==400


@pytest.mark.parametrize("value",["", "   ", None])
def test_edit_title_cannot_be_empty(client,db,value):
    root=make(db)
    assert client.put(f"/api/items/{root.id}",json={"title":value}).status_code==422
    assert client.get("/api/items").json()[0]["title"]=="Goal"


def test_negative_estimate_is_rejected(client,db):
    root=make(db)
    assert client.put(f"/api/items/{root.id}",json={"estimated_minutes":-5}).status_code==422


def test_fold_preference_persists_independent_of_browser_origin(client):
    assert client.put("/api/workspace/task-folds",json={"folds":{"1":False,"2":True}}).status_code==200
    assert client.get("/api/workspace/task-folds").json()["folds"]=={"1":False,"2":True}
    assert client.put("/api/workspace/task-folds",json={"folds":{"bad":True}}).status_code==400


def test_replan_missing_parent_rolls_back_partial_updates(client,db,monkeypatch):
    root=prepared(db)
    monkeypatch.setattr(rp,"parse_content",lambda *a,**kw:plan(ParsedItem(title="Changed",metadata={"existing_item_id":root.id},subtasks=[ParsedItem(title="Parent",subtasks=[ParsedItem(title="Child")])])))
    proposal=rp.propose(db,root)
    assert client.post(f"/api/workspace/replan/{proposal.id}/apply",json={"selected":[0,2]}).status_code==400
    db.expire_all();assert db.get(Item,root.id).title=="Goal" and db.scalar(select(func.count(Item.id)))==1


def test_parent_path_separators_and_model_metadata_cannot_hide_tasks(db):
    structured=plan(ParsedItem(title="A/B",subtasks=[ParsedItem(title="C",metadata={"deleted_at":"2026-10-12","manual_fields":["title"]})]),ParsedItem(title="A",subtasks=[ParsedItem(title="B",subtasks=[ParsedItem(title="C")])]))
    result=confirm_parsed_plan(ImportConfirmRequest(plan=structured,source_name="test"),db)
    assert result["created"]==5
    children=list(db.scalars(select(Item).where(Item.title=="C")))
    assert len(children)==2 and all(wf.is_active(i) and "manual_fields" not in i.metadata_ for i in children)
