"""Activities for team Support (caller). v0.1 has only the scaffold's hello Activity."""

from temporalio import activity


@activity.defn
async def say_hello(name: str) -> str:
    # Pure string formatting: nothing blocks, so an async Activity is safe here.
    return f"Hello, {name}, from namespace agents!"
