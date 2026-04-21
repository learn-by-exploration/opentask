"""Model cost estimation for task tracking."""

from __future__ import annotations

# Approximate cost per 1K tokens (input) for popular models.
# These are ballpark estimates — actual costs depend on provider pricing.
MODEL_COSTS_PER_1K_INPUT: dict[str, float] = {
    # Anthropic
    "anthropic/claude-opus-4": 0.015,
    "anthropic/claude-sonnet-4": 0.003,
    "anthropic/claude-haiku-4": 0.0008,
    # OpenAI
    "openai/gpt-5.4": 0.005,
    "openai/gpt-4.1": 0.002,
    "openai/o3": 0.010,
    # Google
    "google/gemini-2.5-pro": 0.00125,
    "google/gemini-2.5-flash": 0.00015,
    # Shorter aliases (matched after full names)
    "claude-opus-4": 0.015,
    "claude-sonnet-4": 0.003,
    "claude-haiku-4": 0.0008,
    "gpt-5.4": 0.005,
    "gpt-4.1": 0.002,
    "gemini-2.5-pro": 0.00125,
    "gemini-2.5-flash": 0.00015,
}

# Average tokens per character (rough estimate for English text)
AVG_TOKENS_PER_CHAR = 0.25


def estimate_cost(model: str | None, prompt: str, output_chars: int = 0) -> float | None:
    """Estimate the cost of a task based on model and text length.

    Returns estimated cost in USD, or None if the model isn't in our pricing table.
    """
    if not model:
        return None

    cost_per_1k = MODEL_COSTS_PER_1K_INPUT.get(model.lower())
    if cost_per_1k is None:
        # Try partial match (e.g. "sonnet" in "claude-sonnet-4")
        for known, rate in MODEL_COSTS_PER_1K_INPUT.items():
            if known in model.lower() or model.lower() in known:
                cost_per_1k = rate
                break
    if cost_per_1k is None:
        return None

    input_tokens = len(prompt) * AVG_TOKENS_PER_CHAR
    # Agent tasks typically generate 5-10x the input in output
    output_tokens = max(output_chars * AVG_TOKENS_PER_CHAR, input_tokens * 5)
    total_tokens = input_tokens + output_tokens

    return round((total_tokens / 1000) * cost_per_1k, 6)
