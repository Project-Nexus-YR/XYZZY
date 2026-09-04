"""Finding 55, structural half: the service is a composition of one mixin per
domain cluster, not one 9,700 line class.

Every mixin module is importable on its own, MultiplayerService's MRO carries
every one of them, and no module under services/ is left oversized by a later
edit that grows one cluster back toward what this split undid.
"""

from __future__ import annotations

from pathlib import Path

from multiplayer.services.agent_tasks import _AgentTasksMixin
from multiplayer.services.agents import _AgentsMixin
from multiplayer.services.audit import _AuditMixin
from multiplayer.services.bootstrap import _BootstrapMixin
from multiplayer.services.branches import _BranchesMixin
from multiplayer.services.conversation import _ConversationMixin
from multiplayer.services.meta import _MetaMixin
from multiplayer.services.ontology import _OntologyMixin
from multiplayer.services.organizations import _OrganizationsMixin
from multiplayer.services.records import _RecordsMixin
from multiplayer.services.rooms import _RoomsMixin
from multiplayer.services.runs import _RunsMixin
from multiplayer.services.service import MultiplayerService
from multiplayer.services.steps import _StepsMixin

_EXPECTED_MIXINS = (
    _OrganizationsMixin,
    _RoomsMixin,
    _AgentsMixin,
    _RunsMixin,
    _StepsMixin,
    _BranchesMixin,
    _ConversationMixin,
    _RecordsMixin,
    _OntologyMixin,
    _MetaMixin,
    _AuditMixin,
    _AgentTasksMixin,
    _BootstrapMixin,
)


def test_the_service_inherits_every_domain_mixin() -> None:
    mro = MultiplayerService.__mro__
    for mixin in _EXPECTED_MIXINS:
        assert mixin in mro, f"{mixin.__name__} is missing from MultiplayerService's MRO"


def test_public_surface_survives_the_split_unchanged() -> None:
    """A caller importing the class this way, as every route module does, still
    gets every method it built its call sites against."""
    assert hasattr(MultiplayerService, "spawn_agent")
    assert hasattr(MultiplayerService, "send_message")
    assert hasattr(MultiplayerService, "start_branch")
    assert hasattr(MultiplayerService, "open_agent_task")


def test_no_service_module_exceeds_the_line_budget() -> None:
    services_dir = Path(__file__).resolve().parents[2] / "src" / "multiplayer" / "services"
    oversized = {
        path.name: count
        for path in services_dir.glob("*.py")
        if (count := len(path.read_text(encoding="utf-8").splitlines())) > 1500
    }
    assert oversized == {}


def test_service_py_is_the_composition_only() -> None:
    service_path = (
        Path(__file__).resolve().parents[2] / "src" / "multiplayer" / "services" / "service.py"
    )
    line_count = len(service_path.read_text(encoding="utf-8").splitlines())
    assert line_count < 400, f"service.py grew to {line_count} lines"
