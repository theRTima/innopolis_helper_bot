"""Provider-scoped latency controls, separate from evidence and prompts."""

from urllib.parse import urlparse


def thinking_options(base_url, model, enabled=None):
    if enabled is None:
        if (
            urlparse(base_url).hostname == "opencode.ai"
            and model == "qwen3.8-flash"
        ):
            enabled = False
        else:
            return {}
    return {"enable_thinking": enabled}
