def test_compute_run_outcome_win():
    """Win: avg confidence >= target, no abandon, contradiction_rate <= 0.3."""
    from score_updates import compute_run_outcome
    case = {
        "confidence_per_topic": {"t1": 0.82, "t2": 0.78},
        "contradiction_rate": 0.15,
        "outcomes": {"user_decisions": [{"decision": "continue", "at": "2026-10-26T00:00:00+00:00"}]},
        "depths_used": {"standard": 2},
    }
    assert compute_run_outcome(case, confidence_target=0.75) == "W"


def test_compute_run_outcome_loss_low_confidence():
    """Loss: avg confidence below target."""
    from score_updates import compute_run_outcome
    case = {
        "confidence_per_topic": {"t1": 0.55, "t2": 0.6},
        "contradiction_rate": 0.1,
        "outcomes": {"user_decisions": []},
    }
    assert compute_run_outcome(case, confidence_target=0.75) == "L"


def test_compute_run_outcome_loss_user_abandoned():
    """Loss: user picked 'abandon' at quality gate."""
    from score_updates import compute_run_outcome
    case = {
        "confidence_per_topic": {"t1": 0.82},
        "contradiction_rate": 0.1,
        "outcomes": {"user_decisions": [{"decision": "abandon", "at": "2026-10-26T00:00:00+00:00"}]},
    }
    assert compute_run_outcome(case, confidence_target=0.75) == "L"


def test_compute_run_outcome_reads_decisions_as_state_writes_them(tmp_path):
    """The abandon check must match the entry shape record_user_decision writes
    (key "decision"), traced through build_case_record. A fixture-only test used
    the key "choice", which the writer never produces, so abandon never scored."""
    from score_updates import compute_run_outcome
    from state import create_run, init_topic, load_run, save_state, record_user_decision, build_case_record
    create_run(tmp_path, "r1", "full")
    run = load_run(tmp_path)
    run["topics"] = [init_topic("t1", "web_research", "standard")]
    run["topics"][0]["confidence_history"] = [0.9]
    save_state(tmp_path, run)
    record_user_decision(tmp_path, decision="abandon", confidence=0.9)
    case = build_case_record(load_run(tmp_path), {"query": "q"})
    assert compute_run_outcome(case, confidence_target=0.75) == "L"


def test_compute_run_outcome_loss_contradiction_spike():
    """Loss: contradiction_rate > 0.3."""
    from score_updates import compute_run_outcome
    case = {
        "confidence_per_topic": {"t1": 0.82},
        "contradiction_rate": 0.45,
        "outcomes": {"user_decisions": []},
    }
    assert compute_run_outcome(case, confidence_target=0.75) == "L"


def test_apply_score_increments_wins():
    from score_updates import apply_score
    from learned_patterns import LearnedPattern
    p = LearnedPattern(id="p1", name="", body="", domain_tags=[],
                       target_stage="search", wins=2, losses=1)
    apply_score(p, "W")
    assert p.wins == 3 and p.losses == 1


def test_apply_score_increments_losses():
    from score_updates import apply_score
    from learned_patterns import LearnedPattern
    p = LearnedPattern(id="p1", name="", body="", domain_tags=[],
                       target_stage="search", wins=2, losses=1)
    apply_score(p, "L")
    assert p.wins == 2 and p.losses == 2


def test_apply_score_rejects_unknown_outcome():
    import pytest
    from score_updates import apply_score
    from learned_patterns import LearnedPattern
    p = LearnedPattern(id="p1", name="", body="", domain_tags=[],
                       target_stage="search", wins=2, losses=1)
    with pytest.raises(ValueError):
        apply_score(p, "X")
    assert p.wins == 2 and p.losses == 1


def test_demotion_sweep_flags_below_ratio():
    """Pattern with W:L < 0.4 AND uses >= 5 is flagged for demotion."""
    from score_updates import find_demotion_targets
    from learned_patterns import LearnedPattern, LearnedPatternsFile
    f = LearnedPatternsFile(patterns=[
        LearnedPattern(id="bad", name="", body="", domain_tags=[],
                       target_stage="search", wins=1, losses=4),  # 1/5 = 0.2 ratio
        LearnedPattern(id="good", name="", body="", domain_tags=[],
                       target_stage="search", wins=4, losses=1),  # 4/5 = 0.8 ratio
        LearnedPattern(id="few", name="", body="", domain_tags=[],
                       target_stage="search", wins=0, losses=3),  # uses < 5
    ])
    targets = find_demotion_targets(f, min_uses=5, max_loss_ratio=0.4)
    assert {p.id for p in targets} == {"bad"}


def test_demotion_sweep_ratio_exact_threshold():
    """Pattern with W:L = 0.4 exactly is NOT demoted (must be < 0.4)."""
    from score_updates import find_demotion_targets
    from learned_patterns import LearnedPattern, LearnedPatternsFile
    f = LearnedPatternsFile(patterns=[
        LearnedPattern(id="boundary", name="", body="", domain_tags=[],
                       target_stage="search", wins=2, losses=3),  # 2/5 = 0.4 exactly
    ])
    targets = find_demotion_targets(f, min_uses=5, max_loss_ratio=0.4)
    assert targets == []
