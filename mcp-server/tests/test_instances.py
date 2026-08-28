import pytest

from unreal_agent_player.errors import AgentError, ErrorCode
from unreal_agent_player.instances import InstanceRegistry


def test_register_and_resolve_target():
    reg = InstanceRegistry(editor_port=30010)
    iid = reg.register(port=30030, pid=1234)
    assert reg.resolve_port(iid) == 30030
    assert reg.resolve_port(None) == 30010
    assert reg.resolve_port("editor") == 30010


def test_resolve_unknown_target_raises():
    reg = InstanceRegistry(editor_port=30010)
    with pytest.raises(AgentError) as e:
        reg.resolve_port("nope")
    assert e.value.code == ErrorCode.INSTANCE_NOT_FOUND


def test_list_and_remove():
    reg = InstanceRegistry(editor_port=30010)
    iid = reg.register(port=30031, pid=None)
    assert any(i["instance_id"] == iid and i["port"] == 30031 for i in reg.list())
    reg.remove(iid)
    assert reg.list() == []


def test_free_port_picks_unused():
    reg = InstanceRegistry(editor_port=30010)
    p1 = reg.next_free_port(base=30100)
    p2 = reg.next_free_port(base=30100)
    assert p1 >= 30100 and p2 >= 30100 and p1 != p2
