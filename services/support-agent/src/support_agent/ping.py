"""``PingWorkflow``: the smallest thing that proves a serverless Worker is alive (v0.7).

Started on the versioned queue ``support-tq`` with no Worker running, it is what makes
the WCI (or ``make invoke-local``) start a Worker. It runs one Activity (so the
``ActivityTracker`` sees work and the debounce restarts) and reports which Worker
Deployment Version ran it, from ``workflow.info().get_current_deployment_version()``.

No model, no Nexus, no AWS. Same shape as samples-python ``hello/hello_activity.py``
and the temporal-developer skill's python.md (Workflow and Activity in separate files,
the Activity imported through ``imports_passed_through``).
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from support_agent.activities import ping


@workflow.defn
class PingWorkflow:
    """Run one Activity and report the Worker Deployment Version that executed it."""

    @workflow.run
    async def run(self, note: str = "") -> str:
        echo = await workflow.execute_activity(
            ping, note or workflow.info().workflow_id, start_to_close_timeout=timedelta(seconds=10)
        )
        version = workflow.info().get_current_deployment_version()
        where = f"{version.deployment_name}:{version.build_id}" if version else "unversioned"
        return f"{echo} [{where}]"
