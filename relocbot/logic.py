"""Чистая логика без ввода-вывода: даты, выбор напоминаний, подсчёт дней для налогового резидентства."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

_DATE_FORMATS = ("%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d")


def parse_date(text: str) -> date:
    """Принимает ДД.ММ.ГГГГ, ДД/ММ/ГГГГ, ДД-ММ-ГГГГ и ГГГГ-ММ-ДД."""
    s = text.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"не удалось разобрать дату: {text!r}")


def fmt_date(d: date) -> str:
    return d.strftime("%d.%m.%Y")


def days_left(expiry: date, today: date) -> int:
    return (expiry - today).days


def plural_days(n: int) -> str:
    n = abs(n)
    if 11 <= n % 100 <= 14:
        word = "дней"
    elif n % 10 == 1:
        word = "день"
    elif 2 <= n % 10 <= 4:
        word = "дня"
    else:
        word = "дней"
    return f"{n} {word}"


# ---------- напоминания ----------

def pick_due(left: int, leads, sent) -> tuple[int | None, list[int]]:
    """Какое напоминание отправить сейчас.

    Подходят сроки, которые уже наступили (left <= lead) и ещё не отправлялись.
    Отправляем самый срочный, а все более ранние помечаем отправленными,
    чтобы бот после простоя не засыпал пользователя целой пачкой сообщений.
    Возвращает (срок для отправки или None, все подходящие сроки).
    """
    eligible = [lead for lead in leads if left <= lead and lead not in sent]
    if not eligible:
        return None, []
    return min(eligible), eligible


def skipped_on_create(left: int, leads) -> list[int]:
    """Сроки, о которых не нужно напоминать после создания записи:
    пользователь и так видит остаток дней в ответе бота."""
    return [lead for lead in leads if lead >= left]


# ---------- счётчик дней ----------

@dataclass(frozen=True)
class Stay:
    start: date
    end: date | None  # None: пребывание продолжается


def build_stays(events: list[tuple[str, date]]) -> list[Stay]:
    """events: события ('in'|'out', дата) в хронологическом порядке."""
    stays: list[Stay] = []
    open_start: date | None = None
    for kind, day in events:
        if kind == "in":
            if open_start is not None:
                raise ValueError("въезд записан дважды подряд")
            open_start = day
        elif kind == "out":
            if open_start is None:
                raise ValueError("выезд без въезда")
            if day < open_start:
                raise ValueError("выезд раньше въезда")
            stays.append(Stay(open_start, day))
            open_start = None
        else:
            raise ValueError(f"неизвестное событие {kind!r}")
    if open_start is not None:
        stays.append(Stay(open_start, None))
    return stays


def check_new_event(events: list[tuple[str, date]], kind: str, day: date) -> str | None:
    """Проверяет, что новое событие не ломает цепочку въезд/выезд. Возвращает текст ошибки или None."""
    candidate = sorted(events + [(kind, day)], key=lambda e: e[1])  # сортировка устойчива
    try:
        build_stays(candidate)
    except ValueError as e:
        return str(e)
    return None


def days_in_year(stays: list[Stay], year: int, today: date) -> int:
    """Число дней присутствия в календарном году (день въезда и день выезда считаются)."""
    y_start, y_end = date(year, 1, 1), date(year, 12, 31)
    total = 0
    for s in stays:
        end = min(s.end or today, y_end, today)
        start = max(s.start, y_start)
        if end >= start:
            total += (end - start).days + 1
    return total


def is_ongoing(stays: list[Stay]) -> bool:
    return bool(stays) and stays[-1].end is None


def threshold_date(total: int, threshold: int, today: date, ongoing: bool) -> date | None:
    """Когда будет достигнут порог при непрерывном пребывании. None, если уже достигнут,
    если вы вне страны, или если порог не будет достигнут в этом календарном году."""
    if total >= threshold or not ongoing:
        return None
    target = today + timedelta(days=threshold - total)
    return target if target.year == today.year else None
