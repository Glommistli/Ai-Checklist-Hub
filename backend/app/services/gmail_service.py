from __future__ import annotations

import base64
import html
import json
import re
import secrets
import threading
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.core.secrets import delete_secret, get_secret, set_secret
from app.models import AppPreference, EmailIngestion
from app.schemas import ImportConfirmRequest, ParsedItem, ParsedList, ParsedPlan
from .gmail_classifier import EmailDecision, classify_by_rules
from .importer import confirm_parsed_plan


GMAIL_CLIENT_SECRET = "gmail:oauth-client"
GMAIL_TOKEN_SECRET = "gmail:oauth-token"
GMAIL_PREF_KEY = "gmail"
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_ROOT = "https://gmail.googleapis.com/gmail/v1/users/me"
DEFAULTS: dict[str, Any] = {
    "connected": False,
    "email": None,
    "history_id": None,
    "last_synced_at": None,
    "last_attempt_at": None,
    "sync_interval_hours": 1,
    "lookback_days": 14,
    "auto_import": True,
    "auto_import_threshold": 0.9,
    "exclude_promotions": True,
}
_sync_lock = threading.Lock()
_worker_stop = threading.Event()
_worker_thread: threading.Thread | None = None


class GmailError(RuntimeError):
    pass


class GmailHistoryExpired(GmailError):
    pass


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def gmail_state(db: Session) -> dict[str, Any]:
    pref = db.get(AppPreference, GMAIL_PREF_KEY)
    saved = dict(pref.value) if pref else {}
    # Migrate settings written by versions that exposed a minute-only field.
    if "sync_interval_hours" not in saved and "sync_interval_minutes" in saved:
        saved["sync_interval_hours"] = max(1, min(168, round(float(saved["sync_interval_minutes"]) / 60)))
    return {**DEFAULTS, **saved}


def save_gmail_state(db: Session, changes: dict[str, Any]) -> dict[str, Any]:
    state = {**gmail_state(db), **changes}
    pref = db.get(AppPreference, GMAIL_PREF_KEY)
    if pref:
        pref.value = state
    else:
        db.add(AppPreference(key=GMAIL_PREF_KEY, value=state))
    db.commit()
    return state


def client_configured() -> bool:
    return bool(get_secret(GMAIL_CLIENT_SECRET))


def token_configured() -> bool:
    return bool(get_secret(GMAIL_TOKEN_SECRET))


def save_oauth_client(client_id: str, client_secret: str) -> None:
    set_secret(GMAIL_CLIENT_SECRET, json.dumps({"client_id": client_id, "client_secret": client_secret}))


def _oauth_client() -> dict[str, str]:
    raw = get_secret(GMAIL_CLIENT_SECRET)
    if not raw:
        raise GmailError("请先配置 Google OAuth 桌面应用的 Client ID 和 Client Secret")
    try:
        value = json.loads(raw)
        return {"client_id": value["client_id"], "client_secret": value["client_secret"]}
    except (ValueError, KeyError, TypeError) as exc:
        raise GmailError("本机保存的 Google OAuth 客户端配置无效，请重新保存") from exc


def begin_oauth(db: Session, redirect_uri: str) -> str:
    client = _oauth_client()
    state_token = secrets.token_urlsafe(32)
    save_gmail_state(db, {"oauth_state": state_token, "oauth_redirect_uri": redirect_uri})
    return f"{AUTH_URL}?{urlencode({
        'client_id': client['client_id'], 'redirect_uri': redirect_uri,
        'response_type': 'code', 'scope': GMAIL_SCOPE,
        'access_type': 'offline', 'prompt': 'consent', 'state': state_token,
    })}"


def finish_oauth(db: Session, code: str, state_token: str) -> dict[str, Any]:
    state = gmail_state(db)
    if not state_token or not secrets.compare_digest(state_token, str(state.get("oauth_state") or "")):
        raise GmailError("OAuth state 校验失败，请返回应用重新连接")
    client = _oauth_client()
    try:
        response = httpx.post(TOKEN_URL, data={
            "client_id": client["client_id"], "client_secret": client["client_secret"],
            "code": code, "redirect_uri": state.get("oauth_redirect_uri"),
            "grant_type": "authorization_code",
        }, timeout=30)
    except httpx.RequestError as exc:
        raise GmailError(f"无法连接 Google OAuth 服务：{exc.__class__.__name__}") from exc
    if response.is_error:
        raise GmailError(f"Google OAuth 换取令牌失败：HTTP {response.status_code}")
    token = response.json()
    token["expires_at"] = (datetime.now(UTC) + timedelta(seconds=int(token.get("expires_in", 3600)))).timestamp()
    set_secret(GMAIL_TOKEN_SECRET, json.dumps(token))
    profile = GmailClient().profile()
    return save_gmail_state(db, {
        "connected": True,
        "email": profile.get("emailAddress"),
        "history_id": None,
        "oauth_state": None,
        "oauth_redirect_uri": None,
    })


def disconnect_gmail(db: Session) -> None:
    delete_secret(GMAIL_TOKEN_SECRET)
    save_gmail_state(db, {"connected": False, "email": None, "history_id": None, "last_synced_at": None})


class GmailClient:
    def _token(self) -> dict[str, Any]:
        raw = get_secret(GMAIL_TOKEN_SECRET)
        if not raw:
            raise GmailError("Gmail 尚未连接")
        try:
            token = json.loads(raw)
        except ValueError as exc:
            raise GmailError("本机 Gmail 授权令牌无效，请重新连接") from exc
        if float(token.get("expires_at", 0)) <= datetime.now(UTC).timestamp() + 60:
            token = self._refresh(token)
        return token

    def _refresh(self, token: dict[str, Any]) -> dict[str, Any]:
        refresh_token = token.get("refresh_token")
        if not refresh_token:
            raise GmailError("Gmail 授权已失效，请重新连接")
        client = _oauth_client()
        try:
            response = httpx.post(TOKEN_URL, data={
                "client_id": client["client_id"], "client_secret": client["client_secret"],
                "refresh_token": refresh_token, "grant_type": "refresh_token",
            }, timeout=30)
        except httpx.RequestError as exc:
            raise GmailError(f"无法连接 Google OAuth 服务：{exc.__class__.__name__}") from exc
        if response.is_error:
            raise GmailError(f"刷新 Gmail 授权失败：HTTP {response.status_code}")
        refreshed = {**token, **response.json(), "refresh_token": refresh_token}
        refreshed["expires_at"] = (datetime.now(UTC) + timedelta(seconds=int(refreshed.get("expires_in", 3600)))).timestamp()
        set_secret(GMAIL_TOKEN_SECRET, json.dumps(refreshed))
        return refreshed

    def request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        token = self._token()
        access_token = token.get("access_token")
        if not access_token:
            raise GmailError("Gmail 访问令牌无效，请重新连接")
        try:
            response = httpx.request(
                method, f"{API_ROOT}{path}", headers={"Authorization": f"Bearer {access_token}"},
                timeout=30, **kwargs,
            )
        except httpx.RequestError as exc:
            raise GmailError(f"无法连接 Gmail API：{exc.__class__.__name__}") from exc
        if response.status_code == 404 and path.startswith("/history"):
            raise GmailHistoryExpired("Gmail 增量游标已过期")
        if response.is_error:
            raise GmailError(f"Gmail API 请求失败：HTTP {response.status_code}")
        return response.json()

    def profile(self) -> dict[str, Any]:
        return self.request("GET", "/profile")

    def current_history_id(self) -> str:
        return str(self.profile()["historyId"])

    def initial_message_ids(self, lookback_days: int, exclude_promotions: bool) -> list[str]:
        query = f"in:inbox newer_than:{lookback_days}d -in:spam -in:trash"
        if exclude_promotions:
            query += " -category:promotions"
        ids: list[str] = []
        page_token: str | None = None
        while len(ids) < 500:
            params: dict[str, Any] = {"q": query, "labelIds": "INBOX", "maxResults": min(100, 500 - len(ids))}
            if page_token:
                params["pageToken"] = page_token
            payload = self.request("GET", "/messages", params=params)
            ids.extend(str(item["id"]) for item in payload.get("messages", []))
            page_token = payload.get("nextPageToken")
            if not page_token:
                break
        return ids

    def history_message_ids(self, start_history_id: str) -> tuple[list[str], str]:
        ids: list[str] = []
        page_token: str | None = None
        latest = start_history_id
        while True:
            params: dict[str, Any] = {
                "startHistoryId": start_history_id,
                "historyTypes": "messageAdded",
                "labelId": "INBOX",
                "maxResults": 100,
            }
            if page_token:
                params["pageToken"] = page_token
            payload = self.request("GET", "/history", params=params)
            for event in payload.get("history", []):
                for added in event.get("messagesAdded", []):
                    message = added.get("message", {})
                    if "INBOX" in message.get("labelIds", []):
                        ids.append(str(message["id"]))
            latest = str(payload.get("historyId") or latest)
            page_token = payload.get("nextPageToken")
            if not page_token:
                break
        return list(dict.fromkeys(ids)), latest

    def get_message(self, message_id: str) -> dict[str, Any]:
        return self.request("GET", f"/messages/{message_id}", params={"format": "full"})


def _decode(data: str | None) -> str:
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")
    except (ValueError, UnicodeError):
        return ""


def _body_parts(part: dict[str, Any]) -> tuple[list[str], list[str]]:
    plain: list[str] = []
    rich: list[str] = []
    mime = str(part.get("mimeType", "")).lower()
    content = _decode(part.get("body", {}).get("data"))
    if mime == "text/plain" and content:
        plain.append(content)
    elif mime == "text/html" and content:
        # Keep the packaged desktop runtime independent from html.parser. Gmail
        # task extraction only needs readable text, not a full DOM.
        cleaned = re.sub(r"(?is)<(?:script|style)\b[^>]*>.*?</(?:script|style)>", " ", content)
        cleaned = re.sub(r"(?i)<(?:br\s*/?|/p|/div|/li)\s*>", "\n", cleaned)
        cleaned = re.sub(r"<[^>]+>", " ", cleaned)
        rich.append(html.unescape(re.sub(r"[ \t]+", " ", cleaned)).strip())
    for child in part.get("parts", []) or []:
        child_plain, child_rich = _body_parts(child)
        plain.extend(child_plain)
        rich.extend(child_rich)
    return plain, rich


def normalized_message(payload: dict[str, Any]) -> dict[str, Any]:
    headers = {
        str(header.get("name", "")).lower(): str(header.get("value", ""))
        for header in payload.get("payload", {}).get("headers", [])
    }
    plain, rich = _body_parts(payload.get("payload", {}))
    body = "\n".join(plain or rich)
    body = re.sub(r"\n{3,}", "\n\n", html.unescape(body)).strip()
    received_at = None
    try:
        if payload.get("internalDate"):
            received_at = datetime.fromtimestamp(int(payload["internalDate"]) / 1000, UTC).replace(tzinfo=None)
        elif headers.get("date"):
            received_at = parsedate_to_datetime(headers["date"]).astimezone(UTC).replace(tzinfo=None)
    except (ValueError, TypeError, OverflowError):
        pass
    return {
        "message_id": str(payload["id"]),
        "thread_id": payload.get("threadId"),
        "subject": headers.get("subject", "（无主题）")[:500],
        "sender": headers.get("from", "")[:500],
        "received_at": received_at,
        "headers": headers,
        "label_ids": payload.get("labelIds", []),
        "body": body,
    }


def _decision_for(message: dict[str, Any]) -> EmailDecision:
    decision = classify_by_rules(
        message["subject"], message["sender"], message["body"], message["headers"], message["label_ids"]
    )
    if decision:
        return decision
    return EmailDecision(
        "uncertain", False, 0.5,
        "本地规则无法可靠确认这是用户任务，已保留为候选而不自动创建",
    )


def _due_date(text: str) -> date | None:
    match = re.search(r"(20\d{2})[年/-](\d{1,2})[月/-](\d{1,2})日?", text)
    if not match:
        return None
    try:
        return date(*map(int, match.groups()))
    except ValueError:
        return None


def _plan_for_email(message: dict[str, Any], decision: EmailDecision) -> ParsedPlan:
    subject = re.sub(r"^(?:re|fw|fwd)\s*:\s*", "", message["subject"], flags=re.I).strip() or "处理邮件事项"
    description = message["body"][:1200].strip() or f"查看来自 {message['sender']} 的邮件并完成其中要求。"
    item = ParsedItem(
        title=subject[:500],
        description=description,
        due_date=_due_date(f"{message['subject']}\n{message['body'][:4000]}"),
        notes=f"来自：{message['sender']}\n识别原因：{decision.reason}",
        tags=["gmail", "邮件任务"],
        source_excerpt=message["body"][:300] or message["subject"],
    )
    return ParsedPlan(
        title=f"Gmail · {subject[:220]}",
        goal="完成邮件中要求当前用户处理的事项",
        content_type="email_task",
        lists=[ParsedList(name="邮件任务", items=[item])],
        confidence=decision.confidence,
        parser="gmail_rules",
        task_count=1,
    )


def _import_email(db: Session, account: str, record: EmailIngestion, message: dict[str, Any], decision: EmailDecision) -> dict:
    gmail_url = f"https://mail.google.com/mail/u/0/#all/{record.message_id}"
    metadata = {
        "source": "gmail", "gmail_message_id": record.message_id,
        "gmail_thread_id": record.thread_id, "sender": record.sender,
        "subject": record.subject, "gmail_url": gmail_url,
    }
    result = confirm_parsed_plan(ImportConfirmRequest(
        plan=_plan_for_email(message, decision),
        source_name=f"Gmail:{account}",
        raw_content=f"主题：{record.subject}\n发件人：{record.sender}\n\n{message['body']}",
        duplicate_strategy="skip",
        external_id=f"gmail:{account}:{record.message_id}",
        source_metadata=metadata,
    ), db)
    return result


def process_message_ids(
    db: Session, account: str, message_ids: list[str], client: GmailClient,
    settings: dict[str, Any] | None = None,
) -> dict[str, int]:
    """Download and classify only message ids not already present in the durable ledger."""
    config = {**DEFAULTS, **(settings or gmail_state(db))}
    unique_ids = list(dict.fromkeys(message_ids))
    existing: set[str] = set()
    for offset in range(0, len(unique_ids), 400):
        batch = unique_ids[offset:offset + 400]
        existing.update(db.scalars(select(EmailIngestion.message_id).where(
            EmailIngestion.account_email == account,
            EmailIngestion.message_id.in_(batch),
        )))
    summary = {"discovered": len(unique_ids), "already_processed": len(existing), "scanned": 0, "imported": 0, "candidates": 0, "ignored": 0, "failed": 0}
    for message_id in (mid for mid in unique_ids if mid not in existing):
        record = EmailIngestion(account_email=account, message_id=message_id, status="processing", attempt_count=1)
        db.add(record)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            summary["already_processed"] += 1
            continue
        try:
            message = normalized_message(client.get_message(message_id))
            record.thread_id = message["thread_id"]
            record.subject = message["subject"]
            record.sender = message["sender"]
            record.received_at = message["received_at"]
            record.body_excerpt = message["body"][:1000]
            decision = _decision_for(message)
            record.classification = decision.category
            record.confidence = decision.confidence
            record.reason = decision.reason
            record.preview_title = re.sub(r"^(?:re|fw|fwd)\s*:\s*", "", message["subject"], flags=re.I)[:500]
            if decision.is_task and config["auto_import"] and decision.confidence >= float(config["auto_import_threshold"]):
                result = _import_email(db, account, record, message, decision)
                record.item_ids = result["item_ids"]
                record.status = "imported" if result["created"] else "ignored"
                record.reason = decision.reason if result["created"] else "相同邮件任务已经存在，未重复创建"
                summary["imported" if result["created"] else "ignored"] += 1
            elif decision.is_task or decision.category == "uncertain":
                record.status = "candidate"
                summary["candidates"] += 1
            else:
                record.status = "ignored"
                summary["ignored"] += 1
            record.processed_at = _utcnow()
            summary["scanned"] += 1
            db.commit()
        except Exception as exc:
            db.rollback()
            failed = db.get(EmailIngestion, record.id)
            if failed:
                failed.status = "failed"
                failed.error_message = str(exc)[:2000]
                failed.processed_at = _utcnow()
                db.commit()
            summary["failed"] += 1
    return summary


def sync_gmail(db: Session, client: GmailClient | None = None) -> dict[str, Any]:
    if not _sync_lock.acquire(blocking=False):
        raise GmailError("Gmail 同步正在运行，请稍后再试")
    try:
        state = gmail_state(db)
        if not state.get("connected") or not token_configured():
            raise GmailError("Gmail 尚未连接")
        account = str(state.get("email") or "")
        if not account:
            raise GmailError("Gmail 账户信息缺失，请重新连接")
        api = client or GmailClient()
        attempt_at = _utcnow().isoformat()
        save_gmail_state(db, {"last_attempt_at": attempt_at})
        history_id = state.get("history_id")
        mode = "history"
        try:
            if history_id:
                message_ids, next_history_id = api.history_message_ids(str(history_id))
            else:
                mode = "initial"
                next_history_id = api.current_history_id()
                message_ids = api.initial_message_ids(int(state["lookback_days"]), bool(state["exclude_promotions"]))
        except GmailHistoryExpired:
            mode = "recovery"
            next_history_id = api.current_history_id()
            message_ids = api.initial_message_ids(int(state["lookback_days"]), bool(state["exclude_promotions"]))
        summary = process_message_ids(db, account, message_ids, api, state)
        now = _utcnow().isoformat()
        save_gmail_state(db, {"history_id": next_history_id, "last_synced_at": now, "last_attempt_at": attempt_at, "last_sync_error": None})
        return {**summary, "mode": mode, "history_id": next_history_id, "last_synced_at": now}
    except Exception as exc:
        try:
            save_gmail_state(db, {"last_sync_error": str(exc)[:500]})
        except Exception:
            db.rollback()
        raise
    finally:
        _sync_lock.release()


def accept_candidate(db: Session, record: EmailIngestion) -> dict[str, Any]:
    if record.status != "candidate":
        raise GmailError("这封邮件已处理，不可重复导入")
    message = {
        "subject": record.subject,
        "sender": record.sender,
        "body": record.body_excerpt or "",
    }
    decision = EmailDecision("task", True, record.confidence or 0.8, record.reason or "用户确认这是任务")
    result = _import_email(db, record.account_email, record, message, decision)
    record.status = "imported" if result["created"] else "ignored"
    record.item_ids = result["item_ids"]
    record.processed_at = _utcnow()
    db.commit()
    return result


def _background_loop() -> None:
    while not _worker_stop.wait(60):
        with SessionLocal() as db:
            state = gmail_state(db)
            if not state.get("connected"):
                continue
            last = state.get("last_attempt_at") or state.get("last_synced_at")
            try:
                due = not last or datetime.fromisoformat(str(last)) + timedelta(hours=int(state["sync_interval_hours"])) <= _utcnow()
            except (ValueError, TypeError):
                due = True
            if due:
                try:
                    sync_gmail(db)
                except Exception:
                    pass


def start_gmail_worker() -> None:
    global _worker_thread
    if _worker_thread and _worker_thread.is_alive():
        return
    _worker_stop.clear()
    _worker_thread = threading.Thread(target=_background_loop, name="gmail-sync", daemon=True)
    _worker_thread.start()


def stop_gmail_worker() -> None:
    _worker_stop.set()
    if _worker_thread:
        _worker_thread.join(timeout=2)
