from types import SimpleNamespace

from delapan.core.clients import supabase as sb


def test_service_client_uses_service_role_key(monkeypatch):
    captured = {}

    def fake_create_client(url, key):
        captured["url"], captured["key"] = url, key
        return SimpleNamespace(url=url, key=key)

    monkeypatch.setattr(sb, "create_client", fake_create_client)
    monkeypatch.setattr(
        sb, "get_settings",
        lambda: SimpleNamespace(
            supabase_url="https://x.supabase.co",
            supabase_service_role_key="svc",
            supabase_anon_key="anon",
        ),
    )
    c = sb.service_client()
    assert captured == {"url": "https://x.supabase.co", "key": "svc"}
    assert c.url == "https://x.supabase.co"


def test_user_client_applies_token(monkeypatch):
    applied = {}

    class FakeClient:
        def __init__(self):
            self.postgrest = SimpleNamespace(auth=lambda t: applied.__setitem__("token", t))

    monkeypatch.setattr(sb, "create_client", lambda url, key: FakeClient())
    monkeypatch.setattr(
        sb, "get_settings",
        lambda: SimpleNamespace(
            supabase_url="https://x.supabase.co",
            supabase_service_role_key="svc",
            supabase_anon_key="anon",
        ),
    )
    sb.user_client("jwt-123")
    assert applied["token"] == "jwt-123"
