"""Test-only helpers shared by every service's test suite.

v0.1: the two-namespace dev-server fixture (see ``demo_testing.fixtures``).
Later tags add the ScriptedModel (registered under the "bedrock" model key) and
history assertions.
"""

from typing import Final

#: Temporal CLI version the whole project is tested with (also in docs/versions.md).
TEMPORAL_CLI_VERSION: Final[str] = "1.9.1"
#: Caller namespace (team Support).
AGENTS_NAMESPACE: Final[str] = "agents"
#: Handler namespace (team Finance).
AGENT_SERVICES_NAMESPACE: Final[str] = "agent-services"
