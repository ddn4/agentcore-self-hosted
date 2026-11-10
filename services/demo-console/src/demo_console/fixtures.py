"""Keep the recorded fixtures (``static/fixtures/*.json``) on the backend's contract.

A fixture is a recording: its stack, runs, rows and replay are what a real stack showed. Its
``config``, ``scenarios`` and ``actions`` are what the API derives from that stack, so they are
recomputed here from the backend code with facts read off the fixture's own stack snapshot
(no Temporal, no processes). ``make console-fixtures`` rewrites them; a unit test fails when a
fixture has drifted from what ``scenario_json`` and ``ActionService.describe_all`` produce.

    python -m demo_console.fixtures            # rewrite every fixture's API-derived parts
    python -m demo_console.fixtures --check    # exit 1 and name the fixtures that drifted
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from demo_console.actions import ActionService
from demo_console.kind import KindFacts
from demo_console.scenarios import StackFacts, load_catalog, scenario_json
from demo_console.settings import Settings

FIXTURES_DIR = Path(__file__).parent / "static" / "fixtures"
#: The API-derived parts of a fixture, in the order they appear in the file.
DERIVED = ("config", "scenarios", "actions")
_UP = ("up", "external")


def _components(stack: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["id"]: c for c in stack.get("components") or []}


def facts_of(stack: dict[str, Any], profile: str) -> StackFacts:
    """The ``StackFacts`` a live probe would have computed for this snapshot."""
    comps = _components(stack)
    pollers = stack.get("pollers") or {}

    def up(cid: str) -> bool:
        return (comps.get(cid) or {}).get("status") in _UP

    deployment = stack.get("deployment") or {}
    wci = (stack.get("wci") or {}).get("mode")
    return StackFacts(
        profile=profile,
        temporal_up=up("temporal"),
        endpoint_ok=up("endpoint"),
        finance_pollers=len(pollers.get("agent-services/agent-services-tq") or []),
        support_dev_pollers=len(pollers.get("agents/support-dev-tq") or []),
        smoke_pollers=len(pollers.get("agents/nexus-smoke-tq") or []),
        payments_up=up("payments"),
        agentcore_up=up("agentcore"),
        emulator_up=up("emulator"),
        dev_server_wci={"emulator": "1", "off": "0"}.get(wci or ""),
        build_id=(stack.get("build_id") or {}).get("value"),
        current_build=deployment.get("current_build"),
        compute=deployment.get("compute"),
        managed={
            c["process"]: True for c in comps.values() if c.get("process") and c["status"] == "up"
        },
        shape=stack.get("shape") or "unknown",
        kind_gap=(stack.get("kind") or {}).get("gap"),
        read_errors=dict(stack.get("read_errors") or {}),
    )


def _kind_of(stack: dict[str, Any]) -> KindFacts | None:
    k = stack.get("kind")
    if not k:
        return None
    return KindFacts(
        docker=bool(k.get("docker")),
        cluster=k.get("cluster"),
        port_forward=bool(k.get("port_forward")),
        certs=k.get("certs"),
    )


def _workflow_id(old: dict[str, Any] | None, prefix: str) -> str:
    """Keep the fixture's workflow ID (screenshots stay stable); a fixed one otherwise."""
    for o in (old or {}).get("options") or []:
        if o.get("id") == "workflow_id" and o.get("default"):
            return str(o["default"])
    return f"{prefix}-demo"


def derived(fixture: dict[str, Any], root: Path | None = None) -> dict[str, Any]:
    """``{config, scenarios, actions}`` as the backend would serve them for this fixture."""
    stack = fixture.get("stack") or {}
    profile = (fixture.get("config") or {}).get("profile") or "local"
    settings = Settings(root=root or Path.cwd(), profile=profile, base_env={})
    facts = facts_of(stack, profile)
    probe = SimpleNamespace(
        last=stack, facts=facts, kind=SimpleNamespace(last=lambda: _kind_of(stack))
    )
    none = cast(Any, None)
    actions = ActionService(settings, none, none, cast(Any, probe), none)
    catalog = load_catalog()
    model = (stack.get("model") or {}).get("mode", "unknown")
    old = {s["id"]: s for s in (fixture.get("scenarios") or {}).get("scenarios") or []}
    scenarios = [
        scenario_json(s, facts, model, catalog, wf=_workflow_id(old.get(s.id), s.id_prefix))
        for s in catalog.values()
    ]
    # The response bodies, as GET /api/config, /api/scenarios and /api/actions send them.
    return {
        "config": settings.config_body(),
        "scenarios": {"scenarios": scenarios},
        "actions": {"actions": actions.describe_all()},
    }


def dump(fixture: dict[str, Any]) -> str:
    """The fixtures' on-disk format: ``indent=1``, UTF-8 as is, a final newline."""
    return json.dumps(fixture, indent=1, ensure_ascii=False) + "\n"


def refreshed(fixture: dict[str, Any]) -> dict[str, Any]:
    """The fixture with its API-derived parts recomputed (every other key kept in place)."""
    new = derived(fixture)
    return {k: new.get(k, v) if k in DERIVED else v for k, v in fixture.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m demo_console.fixtures")
    parser.add_argument("--check", action="store_true", help="only report drift (exit 1)")
    parser.add_argument("--dir", type=Path, default=FIXTURES_DIR)
    args = parser.parse_args(argv)
    drifted = []
    for path in sorted(args.dir.glob("*.json")):
        text = path.read_text(encoding="utf-8")
        new = dump(refreshed(json.loads(text)))
        if new == text:
            continue
        drifted.append(path.name)
        if not args.check:
            path.write_text(new, encoding="utf-8")
    verb = "drifted" if args.check else "refreshed"
    print(f"{verb}: {', '.join(drifted)}" if drifted else "fixtures match the backend")
    return 1 if args.check and drifted else 0


if __name__ == "__main__":
    sys.exit(main())
