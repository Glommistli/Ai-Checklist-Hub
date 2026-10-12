import httpx
import pytest
from app.services import gmail_service as gmail

def test_invalid_grant_requires_reconnect(db, monkeypatch):
    gmail.save_gmail_state(db, {"connected": True, "email": "me@example.com", "history_id": "123"})
    monkeypatch.setattr(gmail, "token_configured", lambda: True)
    class Expired:
        def history_message_ids(self, cursor):
            raise gmail._oauth_error(httpx.Response(400, json={"error": "invalid_grant", "error_description": "secret-must-not-leak"}))
    with pytest.raises(gmail.GmailAuthorizationExpired, match="invalid_grant"):
        gmail.sync_gmail(db, Expired())
    state = gmail.gmail_state(db)
    assert state["connected"] is False
    assert state["authorization_required"] is True
    assert state["history_id"] == "123"
    assert "secret-must-not-leak" not in state["last_sync_error"]

def test_transient_oauth_error_does_not_disconnect(db, monkeypatch):
    gmail.save_gmail_state(db, {"connected": True, "email": "me@example.com", "history_id": "123"})
    monkeypatch.setattr(gmail, "token_configured", lambda: True)
    class Unavailable:
        def history_message_ids(self, cursor):
            raise gmail._oauth_error(httpx.Response(503, text="unavailable"))
    with pytest.raises(gmail.GmailError, match="503"):
        gmail.sync_gmail(db, Unavailable())
    assert gmail.gmail_state(db)["connected"] is True

@pytest.mark.parametrize("email,expected", [("me@example.com", "123"), ("other@example.com", None)])
def test_reconnect_preserves_only_same_account_cursor(db, monkeypatch, email, expected):
    gmail.save_gmail_state(db, {"connected": False, "email": "me@example.com", "history_id": "123", "oauth_state": "state", "last_sync_error": "expired", "authorization_required": True})
    monkeypatch.setattr(gmail, "_oauth_client", lambda: {"client_id": "test", "client_secret": "test"})
    monkeypatch.setattr(gmail.httpx, "post", lambda *a, **kw: httpx.Response(200, json={"access_token": "test", "refresh_token": "test"}))
    monkeypatch.setattr(gmail, "set_secret", lambda *a: None)
    monkeypatch.setattr(gmail.GmailClient, "profile", lambda self: {"emailAddress": email})
    state = gmail.finish_oauth(db, "code", "state")
    assert state["history_id"] == expected
    assert state["connected"] is True
    assert state["authorization_required"] is False
    assert state["last_sync_error"] is None
