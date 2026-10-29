"""Live model ping through the exact pinned factory (needs RUN_BEDROCK_TESTS=1)."""

import pytest

from finance_agent import config as finance_config
from support_agent import config as support_config

pytestmark = pytest.mark.bedrock


@pytest.mark.parametrize("cfg", [support_config, finance_config], ids=["support", "finance"])
async def test_pinned_model_answers(cfg) -> None:
    model = cfg.bedrock_model()
    text = ""
    async for event in model.stream(
        [{"role": "user", "content": [{"text": "Reply with exactly the word: pong"}]}]
    ):
        delta = event.get("contentBlockDelta", {}).get("delta", {})
        text += delta.get("text", "")
    assert "pong" in text.lower()
