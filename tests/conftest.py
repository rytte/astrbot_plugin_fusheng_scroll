import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
SOURCE = Path(os.environ.get("ASTRBOT_SOURCE", PLUGIN.parent / "AstrBot"))
sys.path.insert(0, str(PLUGIN.parent))
sys.path.insert(0, str(SOURCE))
os.environ["ASTRBOT_ROOT"] = tempfile.mkdtemp(prefix="persona-studio-tests-")


def native_event(
    user="user", *, admin=False, platform="platform", group=False, text="管理人格"
):
    from astrbot.api.event import AstrMessageEvent
    from astrbot.api.message_components import Plain
    from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.platform.platform_metadata import PlatformMetadata

    message = AstrBotMessage()
    message.type = MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
    message.sender = MessageMember(user_id=user, nickname=user)
    message.self_id = "bot"
    message.session_id = user
    message.message_id = "native-message"
    message.message_str = text
    message.message = [Plain(text)]
    message.raw_message = {"text": text}
    event = AstrMessageEvent(
        text, message, PlatformMetadata("test", "Test", platform), user
    )
    event.role = "admin" if admin else "member"
    event.send = AsyncMock()
    return event


@pytest.fixture
async def env(tmp_path, monkeypatch):
    from astrbot.api import sp
    from astrbot.core.agent.tool import FunctionTool
    from astrbot.core.conversation_mgr import ConversationManager
    from astrbot.core.db.sqlite import SQLiteDatabase
    from astrbot.core.persona_mgr import PersonaManager
    from astrbot.core.provider.func_tool_manager import FunctionToolManager
    from astrbot.core.star.context import Context
    from astrbot_plugin_persona_studio.card_tools import CARD_MODEL_TOOLS
    from astrbot_plugin_persona_studio.domain import MODEL_TOOLS
    from astrbot_plugin_persona_studio.service import PersonaStudio
    from astrbot_plugin_persona_studio.storage import Storage

    db = SQLiteDatabase(str(tmp_path / "core.sqlite3"))
    await db.initialize()
    core_config = {
        "wake_prefix": ["/"],
        "provider_settings": {
            "default_personality": "default",
            "computer_use_runtime": "none",
        },
    }
    config_manager = SimpleNamespace(
        default_conf=core_config, confs={"default": core_config}
    )
    manager = PersonaManager(db, config_manager)
    await manager.initialize()
    tools = FunctionToolManager()
    handler = AsyncMock(return_value="echo-result")
    for name in ("echo", *MODEL_TOOLS, *CARD_MODEL_TOOLS):
        tools.func_list.append(
            FunctionTool(
                name=name,
                description="Test",
                parameters={"type": "object", "properties": {}},
                handler=handler,
                handler_module_path="astrbot_plugin_persona_studio.main",
            )
        )
    context = Context.__new__(Context)
    context.persona_manager = manager
    context.conversation_manager = ConversationManager(db)
    context.astrbot_config_mgr = config_manager
    context.subagent_orchestrator = None
    context.get_llm_tool_manager = lambda: tools
    context.get_config = lambda umo=None: core_config
    context.get_registered_star = lambda name: None
    config = {"whitelist": [{"platform_id": "platform", "user_id": "user"}]}
    storage = Storage(tmp_path / "studio.sqlite3")
    studio = PersonaStudio(context, config, storage)
    monkeypatch.setattr(sp, "get_async", AsyncMock(return_value={}))
    monkeypatch.setattr(sp, "global_get", AsyncMock(return_value={}))
    monkeypatch.setattr(sp, "session_get", AsyncMock(return_value=None))
    monkeypatch.setattr(sp, "session_put", AsyncMock())
    result = SimpleNamespace(
        db=db,
        manager=manager,
        context=context,
        config=config,
        core_config=core_config,
        storage=storage,
        studio=studio,
        event=native_event(),
        admin=native_event("admin", admin=True),
        tools=tools,
        handler=handler,
        native_event=native_event,
    )
    yield result
    storage.close()
    await db.engine.dispose()


@pytest.fixture
async def created(env):
    result = await env.studio.execute(
        env.event,
        "manage",
        "create",
        {
            "persona_id": "alice",
            "system_prompt": "版本1",
            "begin_dialogs": ["你好", "你好呀"],
            "tools": ["echo"],
            "skills": [],
        },
    )
    assert result["ok"], result
    return env
