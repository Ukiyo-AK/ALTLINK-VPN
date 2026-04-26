from altlink.domain.enums import PlanCode
from altlink.domain.plans import parse_paid_plan_code, parse_plan_code


def test_parse_plan_code_accepts_current_values() -> None:
    assert parse_plan_code("trial") == PlanCode.TRIAL
    assert parse_plan_code("single_10gbit") == PlanCode.SINGLE_10GBIT
    assert parse_plan_code("single_10gbit_weekly") == PlanCode.SINGLE_10GBIT_WEEKLY
    assert parse_plan_code("unlimited") == PlanCode.UNLIMITED
    assert parse_plan_code("unlimited_weekly") == PlanCode.UNLIMITED_WEEKLY


def test_parse_plan_code_rejects_removed_or_unknown_values() -> None:
    assert parse_plan_code("limited_50gb") is None
    assert parse_plan_code("anything-else") is None
    assert parse_plan_code(None) is None


def test_parse_paid_plan_code_rejects_trial_and_removed_values() -> None:
    assert parse_paid_plan_code("trial") is None
    assert parse_paid_plan_code("limited_50gb") is None
    assert parse_paid_plan_code("single_10gbit") == PlanCode.SINGLE_10GBIT
    assert parse_paid_plan_code("single_10gbit_weekly") == PlanCode.SINGLE_10GBIT_WEEKLY
    assert parse_paid_plan_code("unlimited_weekly") == PlanCode.UNLIMITED_WEEKLY
