"""Provider-scoped latency controls, separate from evidence and prompts."""

from urllib.parse import urlparse


def answer_format_options(base_url, model):
    if (
        urlparse(base_url).hostname == "opencode.ai"
        and model == "qwen3.8-flash"
    ):
        return {"response_format": {"type": "json_object"}}
    return {}


def thinking_options(base_url, model, enabled=None, budget=None):
    known = (
        urlparse(base_url).hostname == "opencode.ai"
        and model == "qwen3.8-flash"
    )
    if enabled is None:
        if known:
            enabled = True
        else:
            return {}
    options = {"enable_thinking": enabled}
    if enabled and (known or budget is not None):
        budget = 512 if budget is None else budget
        if type(budget) is not int or not 1 <= budget <= 4096:
            raise ValueError(
                "Thinking budget must be an integer from 1 to 4096"
            )
        options["thinking_budget"] = budget
    return options


def completion_options(
    base_url, model, max_tokens, enabled=None, budget=None, total_tokens=1400
):
    options = thinking_options(base_url, model, enabled, budget)
    if "thinking_budget" in options:
        if (
            type(total_tokens) is not int
            or total_tokens <= options["thinking_budget"]
        ):
            raise ValueError("Total token limit must exceed thinking budget")
        return {"extra_body": options, "max_completion_tokens": total_tokens}
    return {"extra_body": options or None, "max_tokens": max_tokens}
