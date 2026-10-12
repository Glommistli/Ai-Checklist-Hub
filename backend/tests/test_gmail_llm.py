import pytest
from sqlalchemy import select, func
from app.models import EmailIngestion, Item
from app.schemas import ParsedPlan, ParsedList, ParsedItem
from app.services import gmail_service as gmail
from app.services.parser import LLMProviderError
from test_gmail import FakeGmailClient, payload

def plan(confidence=0.96, empty=False):
    return ParsedPlan(title='完成报名', provider='Test model', model='mock', confidence=confidence,
        lists=[] if empty else [ParsedList(name='邮件任务', items=[
            ParsedItem(title='准备报名材料', subtasks=[ParsedItem(title='准备证明文件')]),
            ParsedItem(title='完成报名表', due_date='2026-10-20')])])

def test_ai_plan_precedes_auto_import_and_keeps_subtasks(db, monkeypatch):
    calls=[]
    def parse(content, session, **kw):
        calls.append((content,kw));return plan()
    monkeypatch.setattr(gmail,'parse_content',parse)
    client=FakeGmailClient({'m':payload('m','报名','请完成并提交报名材料。')})
    result=gmail.process_message_ids(db,'me@example.com',['m'],client)
    assert result['imported']==1
    assert len(calls)==1 and calls[0][1]['force_llm'] is True
    assert 'me@example.com' in calls[0][1]['agent_options'].user_context
    items=list(db.scalars(select(Item)))
    assert {i.title for i in items}=={'准备报名材料','准备证明文件','完成报名表'}
    assert all(i.metadata_['parser']=='gmail_llm' for i in items)
    again=gmail.process_message_ids(db,'me@example.com',['m'],client)
    assert again['already_processed']==1 and len(calls)==1

def test_low_confidence_candidate_reuses_model_plan_on_accept(db,monkeypatch):
    calls=[]
    monkeypatch.setattr(gmail,'parse_content',lambda *a,**kw:(calls.append(kw) or plan(0.85)))
    result=gmail.process_message_ids(db,'me@example.com',['m'],FakeGmailClient({'m':payload('m','报名','请完成并提交报名材料。')}))
    assert result['candidates']==1 and result['imported']==0
    record=db.scalar(select(EmailIngestion))
    gmail.accept_candidate(db,record)
    assert record.status=='imported' and len(calls)==1
    with pytest.raises(gmail.GmailError,match='已处理'):
        gmail.accept_candidate(db,record)

def test_model_failure_leaves_retryable_candidate_without_rule_task(db,monkeypatch):
    def fail(*a,**kw):raise LLMProviderError('model unavailable')
    monkeypatch.setattr(gmail,'parse_content',fail)
    result=gmail.process_message_ids(db,'me@example.com',['m'],FakeGmailClient({'m':payload('m','报名','请完成并提交报名材料。')}))
    record=db.scalar(select(EmailIngestion))
    assert result['failed']==1 and result['candidates']==1
    assert record.status=='candidate' and 'model unavailable' in record.reason
    assert db.scalar(select(func.count(Item.id)))==0

def test_old_candidate_uses_full_message_and_model(db,monkeypatch):
    gmail.save_gmail_state(db,{'connected':True,'email':'me@example.com'})
    record=EmailIngestion(account_email='me@example.com',message_id='m',status='candidate',subject='报名',sender='sender@example.com',body_excerpt='truncated')
    db.add(record);db.commit()
    full='背景'*800+'请于2026年10月20日前提交报名表。'
    monkeypatch.setattr(gmail.GmailClient,'get_message',lambda self,mid:payload(mid,'报名',full))
    calls=[]
    monkeypatch.setattr(gmail,'parse_content',lambda content,*a,**kw:(calls.append(content) or plan()))
    gmail.accept_candidate(db,record)
    assert full in calls[0] and record.status=='imported'

def test_empty_ai_plan_is_ignored_and_advertisements_skip_model(db,monkeypatch):
    calls=[]
    monkeypatch.setattr(gmail,'parse_content',lambda *a,**kw:(calls.append(kw) or plan(empty=True)))
    result=gmail.process_message_ids(db,'me@example.com',['m','ad','otp'],FakeGmailClient({
        'm':payload('m','近况','供你了解'),
        'ad':payload('ad','优惠','限时折扣',labels=['INBOX','CATEGORY_PROMOTIONS']),
        'otp':payload('otp','登录验证码 123456','验证码 123456，5 分钟内有效')}))
    assert result['ignored']==3 and len(calls)==1
    assert db.scalar(select(func.count(Item.id)))==0

def test_manual_model_failure_preserves_candidate_for_retry(db,monkeypatch):
    gmail.save_gmail_state(db,{'connected':True,'email':'me@example.com'})
    record=EmailIngestion(account_email='me@example.com',message_id='m',status='candidate',subject='报名',sender='sender@example.com')
    db.add(record);db.commit()
    monkeypatch.setattr(gmail.GmailClient,'get_message',lambda self,mid:payload(mid,'报名','请完成报名'))
    def fail(*a,**kw):raise LLMProviderError('model unavailable')
    monkeypatch.setattr(gmail,'parse_content',fail)
    with pytest.raises(gmail.GmailError,match='model unavailable'):
        gmail.accept_candidate(db,record)
    assert record.status=='candidate' and record.error_message
    assert db.scalar(select(func.count(Item.id)))==0
