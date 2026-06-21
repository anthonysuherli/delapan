from delapan.core.config import AppConfig, OKFConfig


def test_okf_defaults_present():
    cfg = AppConfig()
    assert cfg.okf.model == "anthropic/claude-sonnet-4.6"
    assert cfg.okf.temperature == 0.3
    assert cfg.okf.max_tokens == 900


def test_okf_config_is_a_model():
    okf = OKFConfig(model="x/y", temperature=0.1, max_tokens=100)
    assert okf.model == "x/y"
