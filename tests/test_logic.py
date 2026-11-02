from datetime import date

import pytest

from relocbot.logic import (
    Stay,
    build_stays,
    check_new_event,
    days_in_year,
    days_left,
    fmt_date,
    is_ongoing,
    parse_date,
    pick_due,
    plural_days,
    skipped_on_create,
    threshold_date,
)

LEADS = [90, 60, 30, 14, 7, 1, 0, -1]


@pytest.mark.parametrize(
    "text",
    ["15.03.2027", "15/03/2027", "15-03-2027", "2027-03-15", "  15.03.2027  "],
)
def test_parse_date_formats(text):
    assert parse_date(text) == date(2027, 3, 15)


@pytest.mark.parametrize("text", ["", "завтра", "31.02.2027", "15.03.27", "2027.03.15", "15.13.2027"])
def test_parse_date_rejects_garbage(text):
    with pytest.raises(ValueError):
        parse_date(text)


def test_fmt_and_days_left():
    assert fmt_date(date(2027, 3, 5)) == "05.03.2027"
    assert days_left(date(2026, 12, 2), date(2026, 11, 1)) == 31
    assert days_left(date(2026, 11, 1), date(2026, 11, 3)) == -2


@pytest.mark.parametrize(
    "n,expected",
    [(1, "1 день"), (2, "2 дня"), (5, "5 дней"), (11, "11 дней"), (21, "21 день"), (22, "22 дня"), (112, "112 дней"), (-3, "3 дня")],
)
def test_plural_days(n, expected):
    assert plural_days(n) == expected


def test_pick_due_nothing_yet():
    assert pick_due(200, LEADS, set()) == (None, [])


def test_pick_due_first_lead():
    assert pick_due(89, LEADS, set()) == (90, [90])


def test_pick_due_skips_already_sent():
    assert pick_due(25, LEADS, {90, 60}) == (30, [30])
    assert pick_due(25, LEADS, {90, 60, 30}) == (None, [])


def test_pick_due_after_downtime_sends_only_most_urgent():
    lead, eligible = pick_due(5, LEADS, set())
    assert lead == 7
    assert eligible == [90, 60, 30, 14, 7]


def test_pick_due_on_expiry_and_overdue():
    assert pick_due(0, LEADS, {90, 60, 30, 14, 7, 1})[0] == 0
    lead, eligible = pick_due(-3, LEADS, {90, 60, 30, 14, 7, 1})
    assert lead == -1
    assert eligible == [0, -1]


def test_skipped_on_create():
    assert skipped_on_create(200, LEADS) == []
    assert skipped_on_create(20, LEADS) == [90, 60, 30]
    assert skipped_on_create(14, LEADS) == [90, 60, 30, 14]
    assert skipped_on_create(-5, LEADS) == LEADS


def test_no_double_reminder_after_create():
    """Сразу после создания записи с 20 днями до срока не должно быть немедленного напоминания."""
    sent = set(skipped_on_create(20, LEADS))
    assert pick_due(20, LEADS, sent) == (None, [])
    assert pick_due(14, LEADS, sent)[0] == 14


# ---------- счётчик дней ----------

def ev(*pairs):
    return [(k, date.fromisoformat(d)) for k, d in pairs]


def test_build_stays_open_and_closed():
    stays = build_stays(ev(("in", "2026-01-10"), ("out", "2026-01-20"), ("in", "2026-03-01")))
    assert stays == [Stay(date(2026, 1, 10), date(2026, 1, 20)), Stay(date(2026, 3, 1), None)]
    assert is_ongoing(stays)
    assert not is_ongoing(stays[:1])
    assert not is_ongoing([])


@pytest.mark.parametrize(
    "events",
    [ev(("out", "2026-01-10")), ev(("in", "2026-01-10"), ("in", "2026-01-12")), ev(("in", "2026-01-10"), ("out", "2026-01-05"))],
)
def test_build_stays_rejects_broken_chains(events):
    with pytest.raises(ValueError):
        build_stays(events)


def test_check_new_event():
    events = ev(("in", "2026-01-10"))
    assert check_new_event(events, "out", date(2026, 1, 20)) is None
    assert check_new_event(events, "in", date(2026, 2, 1)) is not None  # уже в стране
    assert check_new_event([], "out", date(2026, 1, 1)) is not None  # выезд без въезда
    # запись задним числом между двумя поездками
    two = ev(("in", "2026-01-01"), ("out", "2026-01-10"), ("in", "2026-03-01"))
    assert check_new_event(two, "out", date(2026, 3, 5)) is None


def test_days_in_year_counts_inclusive_and_ongoing():
    stays = [Stay(date(2026, 1, 10), date(2026, 1, 20)), Stay(date(2026, 3, 1), None)]
    assert days_in_year(stays, 2026, date(2026, 3, 10)) == 11 + 10


def test_days_in_year_cuts_at_year_boundaries():
    stays = [Stay(date(2025, 12, 20), date(2026, 1, 5))]
    assert days_in_year(stays, 2026, date(2026, 6, 1)) == 5
    assert days_in_year(stays, 2025, date(2026, 6, 1)) == 12


def test_days_in_year_never_counts_the_future():
    stays = [Stay(date(2026, 5, 1), None)]
    assert days_in_year(stays, 2026, date(2026, 5, 1)) == 1
    assert days_in_year(stays, 2027, date(2026, 5, 1)) == 0


def test_threshold_date():
    assert threshold_date(170, 182, date(2026, 6, 20), ongoing=True) == date(2026, 7, 2)
    assert threshold_date(170, 182, date(2026, 6, 20), ongoing=False) is None
    assert threshold_date(182, 182, date(2026, 6, 20), ongoing=True) is None
    assert threshold_date(10, 182, date(2026, 12, 1), ongoing=True) is None  # не успеть до конца года
