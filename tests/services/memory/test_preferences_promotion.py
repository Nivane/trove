"""Auto-preference extraction + promotion tests."""

from __future__ import annotations

import pytest

from trove.services.memory.models import MemoryScope
from trove.services.memory.preferences import (
    HIGH_CONFIDENCE,
    PreferenceStore,
    build_extract_prompt,
    extract_and_store,
    is_usable_preference,
    parse_extract_response,
)
from trove.services.memory.promotion import apply_evidence, maybe_promote
from trove.services.user_facts.service import UserFactsService


class _ScriptedLLM:
    def __init__(self, response):
        self.response = response

    async def chat(self, model, messages, **kwargs):
        return self.response


def test_is_usable_preference_rejects_noise():
    assert is_usable_preference("use 30-day average") is True
    assert is_usable_preference("thanks") is False
    assert is_usable_preference("short") is False
    assert is_usable_preference("can you help me with this") is False


def test_parse_extract_response_ok():
    resp = (
        '{"preferences": ['
        '{"fact": "revenue = net income", "confidence": 0.95, "evidence": "stated"},'
        '{"fact": "show quarterly", "confidence": 0.6, "evidence": "asked"}]}'
    )
    out = parse_extract_response(resp)
    assert len(out) == 2
    assert out[0]["fact"] == "revenue = net income"
    assert out[0]["confidence"] == 0.95


def test_parse_extract_response_markdown_fence():
    resp = '```json\n{"preferences": [{"fact": "x", "confidence": 0.8}]}\n```'
    out = parse_extract_response(resp)
    assert len(out) == 1


def test_parse_extract_response_garbage():
    assert parse_extract_response("not json at all") == []
    assert parse_extract_response("") == []


def test_extract_prompt_renders():
    p = build_extract_prompt("user: revenue = net income", lang="en")
    assert "preferences" in p
    assert "revenue = net income" in p


@pytest.mark.asyncio
async def test_extract_and_store_high_confidence_commits(tmp_path):
    prefs = PreferenceStore(tmp_path / "prefs.sqlite")
    facts = UserFactsService(tmp_path / "facts.db")
    llm = _ScriptedLLM(
        '{"preferences": [{"fact": "revenue = net income", "confidence": 0.95, "evidence": "stated"}]}'
    )
    result = await extract_and_store(
        prefs, facts, llm, MemoryScope(datasource="demo", user_id="u"),
        "user: revenue is net income", model="m", lang="en",
    )
    assert result["committed"], "high-confidence candidate should commit to facts"
    listed = await facts.list("u", "demo")
    assert any("revenue" in f["fact"] for f in listed)


@pytest.mark.asyncio
async def test_extract_and_store_low_confidence_drafts(tmp_path):
    prefs = PreferenceStore(tmp_path / "prefs.sqlite")
    facts = UserFactsService(tmp_path / "facts.db")
    llm = _ScriptedLLM(
        '{"preferences": [{"fact": "show quarterly", "confidence": 0.6, "evidence": "asked"}]}'
    )
    result = await extract_and_store(
        prefs, facts, llm, MemoryScope(datasource="demo", user_id="u"),
        "user: show me quarterly", model="m", lang="en",
    )
    assert result["drafted"]
    drafts = await prefs.list_pending(MemoryScope(datasource="demo", user_id="u"))
    assert any("quarterly" in d["fact"] for d in drafts)


@pytest.mark.asyncio
async def test_extract_and_store_noise_skipped(tmp_path):
    prefs = PreferenceStore(tmp_path / "prefs.sqlite")
    llm = _ScriptedLLM(
        '{"preferences": [{"fact": "thanks", "confidence": 0.9, "evidence": "x"}]}'
    )
    result = await extract_and_store(
        prefs, None, llm, MemoryScope(datasource="demo", user_id="u"),
        "user: thanks", model="m", lang="en",
    )
    assert result["skipped"]


def test_promotion_evidence_delta():
    assert apply_evidence(0.0, "upvote") == 0.4
    assert apply_evidence(0.8, "upvote") == 1.0  # 封顶 1.0
    # repeated_correction 需至少 2 次独立证据
    assert apply_evidence(0.0, "repeated_correction", count=1) == 0.0
    assert apply_evidence(0.0, "repeated_correction", count=2) == 0.6


def test_promotion_maybe_promote():
    assert maybe_promote({"confidence": 0.9, "upvotes": 0, "downvotes": 0}, 0.8) is True
    assert maybe_promote({"confidence": 0.5, "upvotes": 2, "downvotes": 0}, 0.8) is False
    assert maybe_promote({"confidence": 0.5, "upvotes": 3, "downvotes": 0}, 0.8) is True
