from __future__ import annotations

from altlink.domain.enums import UserStatus
from altlink.domain.policies import can_start_trial


def test_trial_can_start_only_for_new_user_without_previous_trial():
    assert can_start_trial(UserStatus.NEW, has_existing_trial=False) is True


def test_trial_denied_for_non_new_user():
    assert can_start_trial(UserStatus.ACTIVE, has_existing_trial=False) is False


def test_trial_denied_when_already_used():
    assert can_start_trial(UserStatus.NEW, has_existing_trial=True) is False

