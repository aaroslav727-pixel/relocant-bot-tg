"""Тексты сообщений (HTML для Telegram). Всё, что приходит из данных, экранируется."""
from __future__ import annotations

from datetime import date
from html import escape

from .data import Catalog, Status
from .logic import fmt_date, plural_days

CONFIDENCE_LABEL = {
    "high": "высокая: есть официальные источники",
    "medium": "средняя: часть данных из вторичных источников",
    "low": "низкая: источники расходятся, проверяйте на официальных сайтах",
}
SOURCE_KIND_LABEL = {
    "official": "официальный",
    "university": "сайт вуза",
    "secondary": "вторичный источник",
    "forum": "форум",
}
SOURCE_ORDER = {"official": 0, "university": 1, "secondary": 2, "forum": 3}
SHORT_DISCLAIMER = "<i>Справочная информация из открытых источников, не юридическая консультация.</i>"


def esc(s: str) -> str:
    return escape(s, quote=False)


def chunk(text: str, limit: int = 3900) -> list[str]:
    """Делит длинный текст на части по абзацам, чтобы уложиться в лимит Telegram."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for para in text.split("\n\n"):
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            parts.append(current)
        while len(para) > limit:
            parts.append(para[:limit])
            para = para[limit:]
        current = para
    if current:
        parts.append(current)
    return parts


def left_phrase(left: int) -> str:
    if left > 0:
        return f"осталось {plural_days(left)}"
    if left == 0:
        return "истекает сегодня"
    return f"истёк {plural_days(left)} назад"


# ---------- общие ----------

def welcome(catalog: Catalog) -> str:
    return (
        f"<b>Привет! Я слежу за сроками ваших документов в стране: {esc(catalog.country)}.</b>\n\n"
        "Добавьте дату окончания пропуска, и я напомню заранее: за 4 месяца, 3, 2, 1, за 2 недели, неделю и в день окончания. "
        "Ещё есть чек-листы по каждому статусу, напоминание про MDAC перед прилётом и счётчик дней "
        "для налогового резидентства.\n\n"
        "/add — добавить срок\n"
        "/my — мои сроки\n"
        "/checklist — что подготовить\n"
        "/help — все команды\n\n"
        f"{esc(catalog.disclaimer)}"
    )


def help_text(free_profiles: int, premium_profiles: int) -> str:
    return (
        "<b>Команды</b>\n"
        "/add — добавить срок документа или напоминание про MDAC\n"
        "/my — мои сроки, обновить дату после продления, удалить\n"
        "/checklist — чек-лист и важные факты по статусу\n"
        "/sources — на чём основаны данные\n"
        "/tax — счётчик дней для налогового резидентства (Premium)\n"
        "/in [дата] и /out [дата] — записать въезд или выезд (Premium)\n"
        "/undo — отменить последнюю запись въезда или выезда\n"
        "/tz — часовой пояс для напоминаний\n"
        "/premium — что входит в Premium\n"
        "/forget — удалить все мои данные\n"
        "/cancel — отменить текущее действие\n\n"
        f"Бесплатно: {free_profiles} документ. Premium: до {premium_profiles} (вся семья)."
    )


def status_card(status: Status) -> str:
    lines = [f"<b>{esc(status.title)}</b>", esc(status.summary), ""]
    if status.facts:
        lines.append("<b>Что важно знать</b>")
        lines += [f"• {esc(f)}" for f in status.facts]
        lines.append("")
    if status.warnings:
        lines.append("<b>Осторожно</b>")
        lines += [f"• {esc(w)}" for w in status.warnings]
        lines.append("")
    lines.append(f"Надёжность данных: {CONFIDENCE_LABEL[status.confidence]}.")
    return "\n".join(lines)


def checklist_text(status: Status, catalog: Catalog) -> str:
    items = "\n".join(f"☐ {esc(c)}" for c in status.checklist)
    return (
        f"{status_card(status)}\n\n"
        f"<b>Чек-лист</b> (типовой, уточняйте требования)\n{items}\n\n"
        f"Проверено по открытым источникам: {esc(catalog.verified_on)}. /sources — ссылки.\n"
        f"{SHORT_DISCLAIMER}"
    )


# ---------- профили ----------

def profile_line(status: Status, expiry: date, left: int) -> str:
    return f"<b>{esc(status.title)}</b>\nДата: {fmt_date(expiry)} ({left_phrase(left)})"


def upcoming_line(pending_leads: list[int], send_hour: int) -> str:
    positive = [lead for lead in pending_leads if lead > 0]
    if not positive and not any(lead == 0 for lead in pending_leads):
        return "Новых напоминаний по этой дате не будет: срок слишком близко. Проверьте всё прямо сейчас."
    parts = ", ".join(str(lead) for lead in positive)
    day_of = " и в день окончания" if 0 in pending_leads else ""
    if positive:
        return f"Напомню в {send_hour:02d}:00 по вашему времени за {parts} дн.{day_of}."
    return f"Напомню в {send_hour:02d}:00 по вашему времени{day_of}."


def profile_added(status: Status, expiry: date, left: int, pending: list[int], send_hour: int) -> str:
    return (
        f"Записал: <b>{esc(status.title)}</b>, {fmt_date(expiry)} ({left_phrase(left)}).\n"
        f"{upcoming_line(pending, send_hour)}\n\n"
        f"{status_card(status)}\n\n"
        f"{SHORT_DISCLAIMER}"
    )


def reminder(status: Status, expiry: date, lead: int, left: int, catalog: Catalog) -> str:
    note = status.lead_notes.get(lead)
    if note is None:
        if lead > 0:
            note = f"До окончания срока осталось {plural_days(lead)}. Проверьте, что продление подано."
        elif lead == 0:
            note = "Срок истекает сегодня."
        else:
            note = "Срок истёк."
    icon = "✈️" if status.kind == "trip" else "⏰"
    return (
        f"{icon} <b>{esc(status.title)}</b>\n"
        f"Дата: {fmt_date(expiry)}, {left_phrase(left)}.\n\n"
        f"{esc(note)}\n\n"
        "/checklist — что подготовить\n"
        f"{SHORT_DISCLAIMER}"
    )


# ---------- источники ----------

def sources_text(catalog: Catalog) -> str:
    ordered = sorted(catalog.sources.values(), key=lambda s: (SOURCE_ORDER[s.kind], s.title))
    lines = [
        f"<b>Источники</b> (проверено {esc(catalog.verified_on)})",
        "Официальные источники надёжнее остальных. Если что-то расходится, верьте официальному сайту.",
        "",
    ]
    for s in ordered:
        lines.append(
            f'• <a href="{escape(s.url, quote=True)}">{esc(s.title)}</a>, {SOURCE_KIND_LABEL[s.kind]}'
        )
    return "\n".join(lines)


# ---------- налоговый счётчик ----------

def tax_text(
    catalog: Catalog,
    year: int,
    total: int,
    ongoing_since: date | None,
    projected: date | None,
    recent: list[tuple[str, date]],
) -> str:
    threshold = catalog.tax.threshold_days
    lines = [f"<b>Дней в Малайзии за {year}:</b> {total} из {threshold}"]
    if total >= threshold:
        lines.append(f"Порог {threshold} дней достигнут: по общему правилу вы налоговый резидент за {year}.")
    else:
        lines.append(f"До порога: {plural_days(threshold - total)}.")
        if projected:
            lines.append(f"При непрерывном пребывании порог будет достигнут около {fmt_date(projected)}.")
    if ongoing_since:
        lines.append(f"Сейчас в Малайзии, въезд {fmt_date(ongoing_since)}.")
    else:
        lines.append("Сейчас вне Малайзии, по вашим записям.")
    if recent:
        lines.append("")
        lines.append("<b>Последние записи</b>")
        for kind, day in recent:
            lines.append(f"{'въезд' if kind == 'in' else 'выезд'}: {fmt_date(day)}")
    lines.append("")
    lines.append("<i>Дни въезда и выезда считаются. Точный подсчёт может отличаться на 1–2 дня.</i>")
    lines.append("")
    lines += [f"• {esc(n)}" for n in catalog.tax.notes]
    lines.append("")
    lines.append("/sources — ссылки. Для налогового решения проконсультируйтесь с налоговым консультантом.")
    return "\n".join(lines)


def tax_upsell(catalog: Catalog) -> str:
    return (
        "<b>Счётчик дней для налогового резидентства</b> входит в Premium.\n"
        f"Он считает дни в Малайзии за календарный год и показывает, сколько осталось до порога в "
        f"{catalog.tax.threshold_days} дней. /premium — подробнее."
    )


def premium_text(
    active_until: date | None, free_profiles: int, premium_profiles: int, tax_premium: bool, contact: str
) -> str:
    lines = ["<b>Бесплатно</b>", f"• {free_profiles} документ с напоминаниями", "• напоминания про MDAC, чек-листы, источники", ""]
    lines.append("<b>Premium</b>")
    lines.append(f"• до {premium_profiles} документов: вся семья")
    if tax_premium:
        lines.append("• счётчик дней для налогового резидентства: /in, /out, /tax")
    lines.append("")
    if active_until:
        lines.append(f"Premium активен до {fmt_date(active_until)}.")
    elif contact:
        lines.append(f"Оформить: {esc(contact)}")
    else:
        lines.append("Оплата пока оформляется вручную через администратора бота.")
    return "\n".join(lines)
