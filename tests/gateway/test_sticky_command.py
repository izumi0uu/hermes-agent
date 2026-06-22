"""Tests for gateway sticky-session controls and Obsidian auto-enable."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key


def _make_source(*, chat_type: str = "dm") -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type=chat_type,
    )


def _make_event(text: str, source: SessionSource) -> MessageEvent:
    return MessageEvent(text=text, source=source, message_id="m1")


def _make_entry(source: SessionSource, *, sticky: bool = False) -> SessionEntry:
    return SessionEntry(
        session_key=build_session_key(source),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        origin=source,
        platform=source.platform,
        chat_type=source.chat_type,
        sticky_no_auto_reset=sticky,
    )


def _make_runner(session_entry: SessionEntry):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner.hooks = SimpleNamespace(
        emit=AsyncMock(),
        emit_collect=AsyncMock(return_value=[]),
        loaded_hooks=False,
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = session_entry

    def _set_sticky(session_key: str, enabled: bool):
        assert session_key == session_entry.session_key
        session_entry.sticky_no_auto_reset = enabled
        return session_entry

    runner.session_store.set_sticky_no_auto_reset = MagicMock(side_effect=_set_sticky)
    runner.session_store.load_transcript.return_value = []
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._session_run_generation = {}
    runner._pending_approvals = {}
    runner._update_prompt_pending = {}
    runner._draining = False
    runner._check_slash_access = lambda *_args, **_kwargs: None
    runner._is_user_authorized = lambda _source: True
    runner._claim_active_session_slot = lambda *_args, **_kwargs: (None, None)
    runner._persist_active_agents = lambda: None
    runner._begin_session_run_generation = lambda *_args, **_kwargs: 1
    runner._release_running_agent_state = (
        lambda session_key, *_args, **_kwargs: runner._running_agents.pop(session_key, None)
    )
    runner._post_turn_goal_continuation = AsyncMock()
    return runner, adapter


@pytest.fixture
def bundle_skill_env(tmp_path, monkeypatch):
    bundles_dir = tmp_path / "skill-bundles"
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    monkeypatch.setenv("HERMES_BUNDLES_DIR", str(bundles_dir))
    import tools.skills_tool as skills_tool_module
    monkeypatch.setattr(skills_tool_module, "SKILLS_DIR", skills_dir)
    import agent.skill_bundles as bundles_mod
    bundles_mod._bundles_cache = {}
    bundles_mod._bundles_cache_mtime = None
    import agent.skill_commands as skills_mod
    skills_mod._skill_commands = {}
    skills_mod._skill_commands_platform = None
    return bundles_dir, skills_dir


def _make_skill(skills_dir, name, body="content"):
    sd = skills_dir / name
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: desc {name}\n---\n\n# {name}\n\n{body}\n",
        encoding="utf-8",
    )


def _make_bundle(bundles_dir, slug, skills):
    bundles_dir.mkdir(parents=True, exist_ok=True)
    (bundles_dir / f"{slug}.yaml").write_text(
        f"name: {slug}\nskills:\n" + "\n".join(f"  - {s}" for s in skills) + "\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_sticky_command_toggles_dm_session(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_args, **_kwargs: [])
    source = _make_source()
    entry = _make_entry(source)
    runner, adapter = _make_runner(entry)

    result_on = await runner._handle_message(_make_event("/sticky on", source))
    result_status = await runner._handle_message(_make_event("/sticky", source))
    result_off = await runner._handle_message(_make_event("/sticky off", source))

    assert "Sticky is now on." in result_on
    assert "Sticky: on" in result_status
    assert "Sticky is now off." in result_off
    assert entry.sticky_no_auto_reset is False
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_sticky_command_rejects_group_enable(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_args, **_kwargs: [])
    source = _make_source(chat_type="group")
    entry = _make_entry(source)
    runner, adapter = _make_runner(entry)

    result = await runner._handle_message(_make_event("/sticky on", source))

    assert "private/DM chats" in result
    assert entry.sticky_no_auto_reset is False
    runner.session_store.set_sticky_no_auto_reset.assert_not_called()
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_obsidian_bundle_auto_enables_sticky_in_dm(bundle_skill_env, monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_args, **_kwargs: [])
    bundles_dir, skills_dir = bundle_skill_env
    _make_skill(skills_dir, "obsidian-llm-wiki", body="bundle wiki guidance")
    _make_bundle(bundles_dir, "ob", ["obsidian-llm-wiki"])

    source = _make_source()
    entry = _make_entry(source)
    runner, adapter = _make_runner(entry)
    seen = {}

    async def _capture(event, source, _quick_key, _run_generation):
        seen["text"] = event.text
        return "agent-result"

    runner._handle_message_with_agent = _capture

    result = await runner._handle_message(_make_event("/ob answer this", source))

    assert result == "agent-result"
    assert entry.sticky_no_auto_reset is True
    assert "Bundle: ob" in seen["text"]
    assert "Skills loaded: obsidian-llm-wiki" in seen["text"]
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_obsidian_bundle_group_keeps_normal_reset(bundle_skill_env, monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_args, **_kwargs: [])
    bundles_dir, skills_dir = bundle_skill_env
    _make_skill(skills_dir, "obsidian-llm-wiki", body="bundle wiki guidance")
    _make_bundle(bundles_dir, "ob", ["obsidian-llm-wiki"])

    source = _make_source(chat_type="group")
    entry = _make_entry(source)
    runner, adapter = _make_runner(entry)
    seen = {}

    async def _capture(event, source, _quick_key, _run_generation):
        seen["text"] = event.text
        return "agent-result"

    runner._handle_message_with_agent = _capture

    result = await runner._handle_message(_make_event("/ob answer this", source))

    assert result == "agent-result"
    assert entry.sticky_no_auto_reset is False
    assert "Bundle: ob" in seen["text"]
    adapter.send.assert_awaited_once()
    assert "private/DM chats" in adapter.send.await_args.args[1]


@pytest.mark.asyncio
async def test_obsidian_skill_auto_enables_sticky_in_dm(bundle_skill_env, monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_args, **_kwargs: [])
    _, skills_dir = bundle_skill_env
    _make_skill(skills_dir, "obsidian-llm-wiki", body="single skill guidance")

    source = _make_source()
    entry = _make_entry(source)
    runner, adapter = _make_runner(entry)
    seen = {}

    async def _capture(event, source, _quick_key, _run_generation):
        seen["text"] = event.text
        return "agent-result"

    runner._handle_message_with_agent = _capture

    result = await runner._handle_message(
        _make_event("/obsidian-llm-wiki answer this", source)
    )

    assert result == "agent-result"
    assert entry.sticky_no_auto_reset is True
    assert "single skill guidance" in seen["text"]
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_obsidian_bundle_auto_enable_is_idempotent(bundle_skill_env, monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_args, **_kwargs: [])
    bundles_dir, skills_dir = bundle_skill_env
    _make_skill(skills_dir, "obsidian-llm-wiki", body="bundle wiki guidance")
    _make_bundle(bundles_dir, "ob", ["obsidian-llm-wiki"])

    source = _make_source()
    entry = _make_entry(source, sticky=True)
    runner, adapter = _make_runner(entry)

    async def _capture(event, source, _quick_key, _run_generation):
        return "agent-result"

    runner._handle_message_with_agent = _capture

    result = await runner._handle_message(_make_event("/ob answer this", source))

    assert result == "agent-result"
    runner.session_store.set_sticky_no_auto_reset.assert_not_called()
    adapter.send.assert_not_awaited()
