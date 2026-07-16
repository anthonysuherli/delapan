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
    assert cfg.memory.enabled is False
    assert cfg.memory.neighbor_top_k == 5
    assert cfg.memory.resolution_model == "anthropic/claude-sonnet-4.6"

    monkeypatch.setenv("DLP_MEMORY__ENABLED", "true")
    get_config.cache_clear()
    assert get_config().memory.enabled is True
    get_config.cache_clear()


def test_memory_resolution_disabled_by_default():
    from delapan.core.config import get_config

    get_config.cache_clear()
    assert get_config().memory.enabled is False


def test_curation_defaults():
    from delapan.core.config import AppConfig

    c = AppConfig().curation
    assert c.enabled is True
    assert c.record_search is True
    assert c.topic_match_threshold == 0.83
    assert c.recency_half_life_days == 14
    assert c.gap_weight == 2.0
    assert c.sparse_weight == 1.0
    assert c.backlog_limit == 20
    assert c.min_query_chars == 8
    assert c.events_retention_days == 90
    assert c.prune_sample_rate == 0.01


def test_curation_env_override(monkeypatch):
    from delapan.core.config import get_config

    monkeypatch.setenv("DLP_CURATION__TOPIC_MATCH_THRESHOLD", "0.91")
    monkeypatch.setenv("DLP_CURATION__ENABLED", "false")
    get_config.cache_clear()
    try:
        cfg = get_config()
        assert cfg.curation.topic_match_threshold == 0.91
        assert cfg.curation.enabled is False
    finally:
        get_config.cache_clear()
