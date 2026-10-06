"""scripts/compute_slot.py — slot label from the cron that fired (WS-A)."""
from datetime import datetime, timezone

from scripts.compute_slot import slot_for_cron


def _t(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def test_label_comes_from_cron_not_latest_slot():
    # The 17:00 cron fired late at 18:40 — still the 17:00 slot.
    assert slot_for_cron("0 17 * * 1-5", _t("2026-10-06T18:40")) == "2026-10-06T17:00"
    # A delayed 14:05 cron landing AFTER 17:00 must NOT be relabelled 17:00.
    assert slot_for_cron("5 14 * * 1-5", _t("2026-10-06T17:20")) == "2026-10-06T14:05"


def test_delay_across_utc_midnight_keeps_the_scheduled_day():
    assert slot_for_cron("0 17 * * 1-5", _t("2026-10-07T00:30")) == "2026-10-06T17:00"
