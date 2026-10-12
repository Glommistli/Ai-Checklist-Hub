import base64

from sqlalchemy import func, select

from app.models import AppPreference, EmailIngestion, Item
from app.services.gmail_classifier import classify_by_rules
from app.services.gmail_service import gmail_state, process_message_ids, save_gmail_state, sync_gmail


def encoded(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def payload(message_id: str, subject: str, body: str, sender: str = "sender@example.com", labels=None):
    return {
        "id": message_id,
        "threadId": f"thread-{message_id}",
        "internalDate": "1790265600000",
        "labelIds": labels or ["INBOX"],
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": sender},
            ],
            "body": {"data": encoded(body)},
        },
    }


class FakeGmailClient:
    def __init__(self, messages):
        self.messages = messages
        self.calls = []

    def get_message(self, message_id):
        self.calls.append(message_id)
        return self.messages[message_id]


class FakeIncrementalClient(FakeGmailClient):
    def __init__(self, messages):
        super().__init__(messages)
        self.initial_calls = 0
        self.history_calls = []

    def current_history_id(self):
        return "100"

    def initial_message_ids(self, lookback_days, exclude_promotions):
        self.initial_calls += 1
        assert lookback_days == 14
        assert exclude_promotions is True
        return ["m-1"]

    def history_message_ids(self, start_history_id):
        self.history_calls.append(start_history_id)
        return ["m-1", "m-2"], "101"


def test_rules_exclude_otp_and_promotions():
    otp = classify_by_rules("登录验证码 123456", "security@example.com", "验证码 123456，5 分钟内有效", {}, ["INBOX"])
    assert otp and otp.category == "otp" and otp.is_task is False

    promotion = classify_by_rules("会员优惠", "shop@example.com", "本周全场八折", {}, ["INBOX", "CATEGORY_PROMOTIONS"])
    assert promotion and promotion.category == "advertisement" and promotion.confidence == 0.99


def test_processing_ledger_prevents_body_download_and_import_twice(db, monkeypatch):
    mock_model(monkeypatch)
    message = payload("m-1", "提交报名材料", "请于2026年9月25日前完成并提交报名材料。")
    client = FakeGmailClient({"m-1": message})
    settings = {"auto_import": True, "auto_import_threshold": 0.9}

    first = process_message_ids(db, "me@example.com", ["m-1", "m-1"], client, settings)
    second = process_message_ids(db, "me@example.com", ["m-1"], client, settings)

    assert first["discovered"] == 1
    assert first["scanned"] == 1
    assert first["imported"] == 1
    assert second["scanned"] == 0
    assert second["already_processed"] == 1
    assert client.calls == ["m-1"]
    assert db.scalar(select(func.count(EmailIngestion.id))) == 1
    assert db.scalar(select(func.count(Item.id))) == 1


def test_uncertain_email_becomes_candidate_without_creating_task(db):
    message = payload("m-2", "与你分享一则近况", "最近项目进展顺利，供你了解。")
    client = FakeGmailClient({"m-2": message})

    result = process_message_ids(db, "me@example.com", ["m-2"], client)

    record = db.scalar(select(EmailIngestion).where(EmailIngestion.message_id == "m-2"))
    assert result["candidates"] == 1
    assert record.status == "candidate"
    assert record.classification == "uncertain"
    assert db.scalar(select(func.count(Item.id))) == 0


def test_sync_switches_from_bounded_initial_scan_to_history_cursor(db, monkeypatch):
    mock_model(monkeypatch)
    from app.services import gmail_service

    client = FakeIncrementalClient({
        "m-1": payload("m-1", "提交报名材料", "请于2026年9月25日前完成并提交报名材料。"),
        "m-2": payload("m-2", "登录验证码 123456", "验证码 123456，5 分钟内有效。"),
    })
    save_gmail_state(db, {"connected": True, "email": "me@example.com", "history_id": None})
    monkeypatch.setattr(gmail_service, "token_configured", lambda: True)

    initial = sync_gmail(db, client)
    incremental = sync_gmail(db, client)

    assert initial["mode"] == "initial"
    assert incremental["mode"] == "history"
    assert incremental["already_processed"] == 1
    assert client.initial_calls == 1
    assert client.history_calls == ["100"]
    assert client.calls == ["m-1", "m-2"]
    assert gmail_state(db)["history_id"] == "101"


def test_old_minute_interval_is_migrated_to_hours(db):
    db.add(AppPreference(key="gmail", value={"sync_interval_minutes": 120}))
    db.commit()

    assert gmail_state(db)["sync_interval_hours"] == 2


def mock_model(monkeypatch):
    from app.services import gmail_service
    from app.schemas import ParsedPlan, ParsedList, ParsedItem
    monkeypatch.setattr(gmail_service, "_plan_for_email", lambda db, account, message: ParsedPlan(
        title="邮件报名", parser="gmail_llm", provider="Mock", model="mock", confidence=0.94,
        lists=[ParsedList(name="邮件任务", items=[ParsedItem(title="提交报名材料")])]))
