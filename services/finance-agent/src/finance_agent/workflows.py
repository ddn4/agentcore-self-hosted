"""Workflows for team Finance (handler). v0.1 has only the scaffold's HelloWorkflow."""

from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from finance_agent.activities import say_hello


@workflow.defn
class HelloWorkflow:
    """Trivial Workflow that proves the Worker, namespace and test harness work."""

    @workflow.run
    async def run(self, name: str) -> str:
        return await workflow.execute_activity(
            say_hello, name, start_to_close_timeout=timedelta(seconds=10)
        )
