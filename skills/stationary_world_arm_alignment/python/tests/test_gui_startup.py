from stationary_world_arm_alignment.app import auto_bootstrap_providers_enabled


def test_gui_startup_is_passive_by_default(monkeypatch):
    monkeypatch.delenv("MIDBRAIN_GUI_AUTO_BOOTSTRAP_PROVIDERS", raising=False)

    assert auto_bootstrap_providers_enabled() is False


def test_gui_provider_bootstrap_requires_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("MIDBRAIN_GUI_AUTO_BOOTSTRAP_PROVIDERS", "true")

    assert auto_bootstrap_providers_enabled() is True
