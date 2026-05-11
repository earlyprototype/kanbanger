"""Tests for the review_gate_etiquette prompt."""

from __future__ import annotations


def test_review_gate_etiquette_registered(registered_prompts):
    assert "review_gate_etiquette" in registered_prompts
    assert callable(registered_prompts["review_gate_etiquette"])


def test_review_gate_etiquette_content(registered_prompts):
    prompt_fn = registered_prompts["review_gate_etiquette"]
    text = prompt_fn()

    # Load-bearing facts the prompt MUST mention. If the copy is
    # rewritten, these assertions document the contract that any
    # rewrite has to preserve.
    assert "propose_done" in text
    assert "approve_done" in text
    assert "reject_review" in text
    assert "REVIEW" in text
    assert "DONE" in text
    # The "do not call move_task to DONE" constraint is the whole
    # point of the prompt; assert it's there.
    assert "move_task" in text
    # Pattern C — Rework task is the canonical rework signal.
    assert "Rework:" in text
    # REJECTED annotation appears in the original-task narrative.
    assert "REJECTED" in text
