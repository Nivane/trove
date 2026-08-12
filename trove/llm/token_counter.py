"""Token counting utility using tiktoken.

Provides fast estimation of token counts for prompt/context
size management and compaction triggering.
"""

from __future__ import annotations


class TokenCounter:
    """Count tokens for a given model using tiktoken.

    Falls back to a character-based heuristic if tiktoken
    encoding is unavailable for the specified model.
    """

    # Known model → encoding mappings
    _MODEL_ENCODINGS: dict[str, str] = {
        "gpt-4": "cl100k_base",
        "gpt-4o": "o200k_base",
        "gpt-4o-mini": "o200k_base",
        "gpt-3.5-turbo": "cl100k_base",
        "text-davinci-003": "p50k_base",
        "text-embedding-ada-002": "cl100k_base",
    }

    def __init__(self, model: str = "gpt-4o"):
        """Initialize with a model name for encoding selection.

        Args:
            model: Model name to select the appropriate encoding.
        """
        self.model = model
        self._encoding = self._get_encoding(model)

    def _get_encoding(self, model: str):
        """Get the tiktoken encoding for a model.

        Falls back to "cl100k_base" if model is not recognized.
        """
        encoding_name = self._MODEL_ENCODINGS.get(model, "cl100k_base")
        try:
            import tiktoken
            return tiktoken.get_encoding(encoding_name)
        except (ImportError, ValueError):
            return None

    def count(self, text: str) -> int:
        """Count tokens in a text string.

        Args:
            text: The text to count tokens for.

        Returns:
            Estimated token count.
        """
        if self._encoding is not None:
            return len(self._encoding.encode(text))
        # Fallback: ~4 characters per token (rough heuristic)
        return len(text) // 4

    def count_messages(self, messages: list[dict[str, str]]) -> int:
        """Count tokens across a list of chat messages.

        Includes overhead for message formatting (~4 tokens per message).

        Args:
            messages: List of message dicts with "role" and "content".

        Returns:
            Total estimated token count.
        """
        total = 0
        for msg in messages:
            # ~4 token overhead per message for role formatting
            total += 4
            content = msg.get("content", "")
            total += self.count(content)
        # ~3 tokens for assistant reply priming
        total += 3
        return total

    def should_compact(
        self,
        messages: list[dict[str, str]],
        context_limit: int = 128000,
        threshold: float = 0.9,
    ) -> bool:
        """Check if context is approaching the limit and needs compaction.

        Args:
            messages: Current conversation messages.
            context_limit: Model's context window size.
            threshold: Fraction of context limit that triggers compaction.

        Returns:
            True if compaction is recommended.
        """
        token_count = self.count_messages(messages)
        return token_count > (context_limit * threshold)

    def estimate_context_usage(
        self,
        messages: list[dict[str, str]],
        context_limit: int = 128000,
    ) -> dict[str, float]:
        """Return context usage statistics.

        Args:
            messages: Current conversation messages.
            context_limit: Model's context window size.

        Returns:
            Dict with token_count, context_limit, usage_ratio.
        """
        token_count = self.count_messages(messages)
        return {
            "token_count": token_count,
            "context_limit": context_limit,
            "usage_ratio": token_count / context_limit if context_limit > 0 else 0.0,
        }
