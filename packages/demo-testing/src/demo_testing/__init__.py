"""Test-only helpers shared by every service's test suite.

- ``demo_testing.fixtures``: the two-namespace dev-server fixture (v0.1).
- ``demo_testing.scripted_model``: ``ScriptedModel``, an offline Strands model
  registered under the same ``"bedrock"`` key the services pin Opus 5.5 under (v0.2).
- ``demo_testing.history``: event-history helpers for assertions (v0.2; Nexus v0.4; timeline v0.5).
- ``demo_testing.finance_stub``: a stub Finance peer behind the real contract (v0.5).
"""

from typing import Final

#: Temporal CLI version the whole project is tested with (also in docs/versions.md).
TEMPORAL_CLI_VERSION: Final[str] = "1.9.1"
#: Caller namespace (team Support).
AGENTS_NAMESPACE: Final[str] = "agents"
#: Handler namespace (team Finance).
AGENT_SERVICES_NAMESPACE: Final[str] = "agent-services"
