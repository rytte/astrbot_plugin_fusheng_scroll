import json

import pytest
from astrbot.api import AstrBotConfig
from astrbot_plugin_fusheng_scroll.core.branding import LEGACY_PLUGIN_NAME, PLUGIN_NAME
from astrbot_plugin_fusheng_scroll.core.domain import StudioError
from astrbot_plugin_fusheng_scroll.main import PersonaStudioPlugin


@pytest.mark.parametrize("legacy_exists", [False, True])
async def test_new_install_reuses_legacy_config_once(
    env, tmp_path, monkeypatch, legacy_exists
):
    from astrbot.api.star import StarTools

    monkeypatch.setattr(StarTools, "get_data_dir", lambda name: tmp_path / "data")
    legacy = tmp_path / f"{LEGACY_PLUGIN_NAME}_config.json"
    settings = {**env.config, "allow_group_chat": False, "backup_keep_count": 7}
    if legacy_exists:
        legacy.write_text(json.dumps(settings), encoding="utf-8-sig")
    path = tmp_path / f"{PLUGIN_NAME}_config.json"
    defaults = {"whitelist": [], "allow_group_chat": True, "backup_keep_count": 50}
    config = AstrBotConfig(str(path), default_config=defaults)
    plugin = PersonaStudioPlugin(env.context, config)
    await plugin.initialize()
    try:
        if legacy_exists:
            assert dict(config) == settings
            assert json.loads(path.read_text(encoding="utf-8-sig")) == settings
            assert json.loads(legacy.read_text(encoding="utf-8-sig")) == settings
            config.save_config({"allow_group_chat": True})
            plugin.restore_legacy_config()
            assert config["allow_group_chat"] is True
        else:
            assert config == defaults
            assert plugin.studio.settings().whitelist == ()
    finally:
        await plugin.terminate()


def test_existing_new_config_is_not_overwritten_by_legacy(env, tmp_path):
    legacy = tmp_path / f"{LEGACY_PLUGIN_NAME}_config.json"
    legacy.write_text(json.dumps({"allow_group_chat": False}))
    path = tmp_path / f"{PLUGIN_NAME}_config.json"
    path.write_text(json.dumps(env.config))
    config = AstrBotConfig(str(path), default_config=dict(env.config))
    PersonaStudioPlugin(env.context, config).restore_legacy_config()
    assert config == env.config
    assert json.loads(path.read_text(encoding="utf-8-sig")) == env.config


@pytest.mark.parametrize(
    "content", ['{"allow_group_chat": "false"}', "[]", "invalid-json"]
)
def test_invalid_legacy_config_does_not_change_new_config(env, tmp_path, content):
    legacy = tmp_path / f"{LEGACY_PLUGIN_NAME}_config.json"
    legacy.write_text(content)
    path = tmp_path / f"{PLUGIN_NAME}_config.json"
    config = AstrBotConfig(str(path), default_config={})
    before = path.read_bytes()
    with pytest.raises((StudioError, ValueError)):
        PersonaStudioPlugin(env.context, config).restore_legacy_config()
    assert path.read_bytes() == before
    assert config == {}
    assert legacy.read_text() == content
