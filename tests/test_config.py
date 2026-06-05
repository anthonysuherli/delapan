from __future__ import annotations


def test_settings_boot_without_supabase(monkeypatch):
    for k in [
        "SUPABASE_URL",
        "SUPABASE_ANON_KEY",
        "SUPABASE_SERVICE_ROLE_KEY",
        "SUPABASE_JWT_SECRET",
        "DATABASE_URL",
    ]:
        monkeypatch.delenv(k, raising=False)
    from delapan.core.config import get_settings

    get_settings.cache_clear()
    s = get_settings()
    assert s.supabase_url is None
    assert s.openai_api_key is None or isinstance(s.openai_api_key, str)


def test_config_override_prefix_is_dlp(monkeypatch):
    monkeypatch.setenv("DLP_AGENT__TEMPERATURE", "0.4")
    from delapan.core.config import get_config

    get_config.cache_clear()
    assert abs(get_config().agent.temperature - 0.4) < 1e-9
