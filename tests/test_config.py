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


def test_memory_config_defaults_and_env_override(monkeypatch):
    from delapan.core.config import get_config

    get_config.cache_clear()
    cfg = get_config()
    assert cfg.memory.enabled is True  # config.yaml enables this on the local tier (Task 8)
    assert cfg.memory.neighbor_top_k == 5
    assert cfg.memory.resolution_model == "anthropic/claude-sonnet-4.6"

    monkeypatch.setenv("DLP_MEMORY__ENABLED", "false")
    get_config.cache_clear()
    assert get_config().memory.enabled is False
    get_config.cache_clear()


def test_memory_resolution_enabled_via_yaml():
    from delapan.core.config import get_config

    get_config.cache_clear()
    assert get_config().memory.enabled is True


def test_memory_code_default_stays_dark():
    from delapan.core.config import MemoryConfig

    assert MemoryConfig().enabled is False


def test_canvas_section_defaults_and_env_override(monkeypatch):
    from delapan.core.config import get_config

    get_config.cache_clear()
    cfg = get_config().canvas
    assert cfg.answer_model == "anthropic/claude-sonnet-4.6"
    assert cfg.max_candidates == 24
    assert cfg.keep_max_candidates == 20
    assert cfg.keep_max_content_chars == 8000
    assert cfg.max_history_turns == 8
    assert cfg.answer_max_tokens == 1024

    monkeypatch.setenv("DLP_CANVAS__MAX_CANDIDATES", "5")
    get_config.cache_clear()
    assert get_config().canvas.max_candidates == 5
    get_config.cache_clear()
