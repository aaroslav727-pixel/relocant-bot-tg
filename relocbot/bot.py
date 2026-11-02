"""Диспетчер сообщений и рассылка напоминаний."""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import texts
from .data import Catalog
from .logic import (
    build_stays,
    check_new_event,
    days_in_year,
    days_left,
    fmt_date,
    is_ongoing,
    parse_date,
    pick_due,
    skipped_on_create,
    threshold_date,
)
from .store import Store
from .tg import TelegramAPI, TelegramError

log = logging.getLogger("relocbot")

MYT = timezone(timedelta(hours=8), "MYT")


def get_zone(name: str):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        if name == "Asia/Kuala_Lumpur":  # запасной вариант, если в системе нет базы часовых поясов
            return MYT
        raise


@dataclass
class Config:
    token: str = ""
    db_path: str = "relocbot.sqlite3"
    admin_ids: frozenset = frozenset()
    default_tz: str = "Asia/Kuala_Lumpur"
    send_hour: int = 9
    free_profiles: int = 1
    premium_profiles: int = 8
    max_trips: int = 3
    tax_tracker_premium: bool = True
    contact: str = ""
    poll_timeout: int = 30
    tick_seconds: int = 300

    @classmethod
    def from_env(cls, env=None) -> "Config":
        env = os.environ if env is None else env
        admins = frozenset(int(x) for x in env.get("ADMIN_IDS", "").replace(" ", "").split(",") if x)
        return cls(
            token=env.get("BOT_TOKEN", ""),
            db_path=env.get("DB_PATH", "relocbot.sqlite3"),
            admin_ids=admins,
            default_tz=env.get("DEFAULT_TZ", "Asia/Kuala_Lumpur"),
            send_hour=int(env.get("SEND_HOUR", "9")),
            free_profiles=int(env.get("FREE_PROFILES", "1")),
            premium_profiles=int(env.get("PREMIUM_PROFILES", "8")),
            tax_tracker_premium=env.get("TAX_TRACKER_PREMIUM", "1") not in ("0", "false", "no"),
            contact=env.get("PREMIUM_CONTACT", ""),
        )


@dataclass
class Ctx:
    uid: int
    chat_id: int
    arg: str
    today: date
    tz: str
    premium: bool


def _kb(rows: list[list[tuple[str, str]]]) -> dict:
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


class Bot:
    def __init__(self, api, store: Store, catalog: Catalog, config: Config, now_fn=None):
        self.api = api
        self.store = store
        self.catalog = catalog
        self.cfg = config
        self.now = now_fn or (lambda: datetime.now(timezone.utc))
        self.commands = {
            "start": self.cmd_start,
            "help": self.cmd_help,
            "add": self.cmd_add,
            "my": self.cmd_my,
            "checklist": self.cmd_checklist,
            "sources": self.cmd_sources,
            "tax": self.cmd_tax,
            "in": lambda c: self._cmd_event(c, "in"),
            "out": lambda c: self._cmd_event(c, "out"),
            "undo": self.cmd_undo,
            "tz": self.cmd_tz,
            "premium": self.cmd_premium,
            "cancel": self.cmd_cancel,
            "forget": self.cmd_forget,
            "grant": self.cmd_grant,
            "stats": self.cmd_stats,
        }

    # ---------- отправка ----------
    def send(self, chat_id: int, text: str, kb: dict | None = None) -> None:
        parts = texts.chunk(text)
        for i, part in enumerate(parts):
            params = {
                "chat_id": chat_id,
                "text": part,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if kb and i == len(parts) - 1:
                params["reply_markup"] = kb
            self.api.call("sendMessage", **params)

    def _ctx(self, uid: int, chat_id: int, arg: str = "") -> Ctx:
        now = self.now()
        user = self.store.ensure_user(uid, now.isoformat(), self.cfg.default_tz)
        try:
            tz = get_zone(user["tz"])
        except (ZoneInfoNotFoundError, ValueError, OSError):
            tz = get_zone(self.cfg.default_tz)
        today = now.astimezone(tz).date()
        until = self.store.premium_until(uid)
        return Ctx(uid, chat_id, arg.strip(), today, user["tz"], bool(until and until >= today))

    def _profile_limit(self, ctx: Ctx) -> int:
        return self.cfg.premium_profiles if ctx.premium else self.cfg.free_profiles

    def _count_kind(self, uid: int, kind: str) -> int:
        n = 0
        for p in self.store.list_profiles(uid):
            st = self.catalog.statuses.get(p["status_id"])
            if st and st.kind == kind:
                n += 1
        return n

    # ---------- входящие обновления ----------
    def handle_update(self, update: dict) -> None:
        if "callback_query" in update:
            self._on_callback(update["callback_query"])
        elif "message" in update:
            self._on_message(update["message"])

    def _on_message(self, msg: dict) -> None:
        if msg.get("chat", {}).get("type") != "private":
            return
        text = msg.get("text")
        sender = msg.get("from")
        if not text or not sender or sender.get("is_bot"):
            return
        uid, chat_id = sender["id"], msg["chat"]["id"]
        if text.startswith("/"):
            head, _, arg = text.partition(" ")
            name = head[1:].split("@")[0].lower()
            handler = self.commands.get(name)
            ctx = self._ctx(uid, chat_id, arg)
            if handler is None:
                self.send(chat_id, "Не знаю такой команды. /help — список команд.")
                return
            if name not in ("cancel",):
                self.store.clear_state(uid)
            handler(ctx)
            return
        ctx = self._ctx(uid, chat_id, text)
        state = self.store.get_state(uid)
        if state and state.get("step") == "date":
            self._on_date(ctx, state, text)
        else:
            self.send(chat_id, "Я понимаю команды. /add — добавить срок, /my — мои сроки, /help — всё остальное.")

    def _on_callback(self, cq: dict) -> None:
        self.api.call("answerCallbackQuery", callback_query_id=cq["id"])
        msg = cq.get("message") or {}
        if msg.get("chat", {}).get("type") != "private":
            return
        uid, chat_id = cq["from"]["id"], msg["chat"]["id"]
        ctx = self._ctx(uid, chat_id)
        action, _, value = (cq.get("data") or "").partition(":")
        handlers = {
            "menu": self._cb_menu,
            "add": self._cb_add,
            "ck": self._cb_checklist,
            "del": self._cb_delete,
            "delyes": self._cb_delete_yes,
            "upd": self._cb_update,
            "forget": self._cb_forget,
        }
        handler = handlers.get(action)
        if handler:
            handler(ctx, value)

    # ---------- команды ----------
    def cmd_start(self, ctx: Ctx) -> None:
        self.send(ctx.chat_id, texts.welcome(self.catalog), _kb([[("➕ Добавить срок", "menu:add")]]))

    def cmd_help(self, ctx: Ctx) -> None:
        self.send(ctx.chat_id, texts.help_text(self.cfg.free_profiles, self.cfg.premium_profiles))

    def cmd_cancel(self, ctx: Ctx) -> None:
        self.store.clear_state(ctx.uid)
        self.send(ctx.chat_id, "Отменено.")

    def cmd_add(self, ctx: Ctx) -> None:
        rows = [[(s.title, f"add:{s.id}")] for s in self.catalog.documents()]
        rows += [[(s.title, f"add:{s.id}")] for s in self.catalog.trips()]
        self.send(ctx.chat_id, "Что добавить?", _kb(rows))

    def cmd_my(self, ctx: Ctx) -> None:
        profiles = self.store.list_profiles(ctx.uid)
        if not profiles:
            self.send(ctx.chat_id, "Пока ничего нет. /add — добавить срок.")
            return
        for p in profiles:
            status = self.catalog.statuses.get(p["status_id"])
            if status is None:
                continue
            expiry = date.fromisoformat(p["expiry"])
            left = days_left(expiry, ctx.today)
            kb = _kb([
                [("☑ Чек-лист", f"ck:{status.id}"), ("✏️ Новая дата", f"upd:{p['id']}")],
                [("🗑 Удалить", f"del:{p['id']}")],
            ])
            self.send(ctx.chat_id, texts.profile_line(status, expiry, left), kb)

    def cmd_checklist(self, ctx: Ctx) -> None:
        rows = [[(s.title, f"ck:{s.id}")] for s in self.catalog.statuses.values()]
        self.send(ctx.chat_id, "По какому статусу показать чек-лист?", _kb(rows))

    def cmd_sources(self, ctx: Ctx) -> None:
        self.send(ctx.chat_id, texts.sources_text(self.catalog))

    def cmd_tz(self, ctx: Ctx) -> None:
        if not ctx.arg:
            self.send(
                ctx.chat_id,
                f"Сейчас: <b>{texts.esc(ctx.tz)}</b>. Чтобы сменить, пришлите например /tz Europe/Moscow "
                "или /tz Asia/Kuala_Lumpur.",
            )
            return
        try:
            get_zone(ctx.arg)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            self.send(ctx.chat_id, "Не нашёл такой часовой пояс. Нужен формат вроде Europe/Moscow или Asia/Kuala_Lumpur.")
            return
        self.store.set_tz(ctx.uid, ctx.arg)
        self.send(ctx.chat_id, f"Часовой пояс: <b>{texts.esc(ctx.arg)}</b>. Напоминания приходят в {self.cfg.send_hour:02d}:00.")

    def cmd_premium(self, ctx: Ctx) -> None:
        until = self.store.premium_until(ctx.uid)
        active = until if until and until >= ctx.today else None
        self.send(
            ctx.chat_id,
            texts.premium_text(
                active, self.cfg.free_profiles, self.cfg.premium_profiles, self.cfg.tax_tracker_premium, self.cfg.contact
            ),
        )

    def cmd_forget(self, ctx: Ctx) -> None:
        self.send(
            ctx.chat_id,
            "Удалить все ваши данные: сроки, записи въездов и настройки? Это необратимо.",
            _kb([[("Да, удалить всё", "forget:yes"), ("Нет", "forget:no")]]),
        )

    # ---------- админ ----------
    def _is_admin(self, ctx: Ctx) -> bool:
        return ctx.uid in self.cfg.admin_ids

    def cmd_grant(self, ctx: Ctx) -> None:
        if not self._is_admin(ctx):
            return
        try:
            target, days = (int(x) for x in ctx.arg.split())
            if not 1 <= days <= 3650:
                raise ValueError
        except ValueError:
            self.send(ctx.chat_id, "Формат: /grant <id пользователя> <дней>")
            return
        user = self.store.get_user(target)
        if user is None:
            self.send(ctx.chat_id, "Такого пользователя нет: он должен сначала написать боту /start.")
            return
        current = self.store.premium_until(target)
        base = current if current and current > ctx.today else ctx.today
        until = base + timedelta(days=days)
        self.store.set_premium_until(target, until)
        self.send(ctx.chat_id, f"Premium для {target} до {fmt_date(until)}.")

    def cmd_stats(self, ctx: Ctx) -> None:
        if not self._is_admin(ctx):
            return
        s = self.store.stats(ctx.today)
        self.send(
            ctx.chat_id,
            f"Пользователей: {s['users']}\nДокументов и поездок: {s['profiles']}\nPremium: {s['premium']}\nЗаписей въезд/выезд: {s['events']}",
        )

    # ---------- налоговый счётчик ----------
    def _tax_allowed(self, ctx: Ctx) -> bool:
        if self.cfg.tax_tracker_premium and not ctx.premium:
            self.send(ctx.chat_id, texts.tax_upsell(self.catalog))
            return False
        return True

    def _cmd_event(self, ctx: Ctx, kind: str) -> None:
        if not self._tax_allowed(ctx):
            return
        try:
            day = parse_date(ctx.arg) if ctx.arg else ctx.today
        except ValueError:
            self.send(ctx.chat_id, "Не понял дату. Формат: ДД.ММ.ГГГГ, например /in 15.03.2026. Без даты запишу сегодня.")
            return
        if day > ctx.today:
            self.send(ctx.chat_id, "Эта дата ещё не наступила: записывайте только случившееся.")
            return
        error = check_new_event(self.store.events(ctx.uid), kind, day)
        if error:
            hint = "Вы уже отмечены как находящийся в Малайзии." if kind == "in" else "Сначала запишите въезд (/in)."
            self.send(ctx.chat_id, f"Не получилось: {texts.esc(error)}. {hint} /undo отменяет последнюю запись.")
            return
        self.store.add_event(ctx.uid, kind, day, self.now().isoformat())
        word = "Въезд" if kind == "in" else "Выезд"
        total = self._year_total(ctx)
        self.send(ctx.chat_id, f"{word} {fmt_date(day)} записан. В этом году: {total} дн. /tax — подробности.")

    def _year_total(self, ctx: Ctx) -> int:
        stays = build_stays(self.store.events(ctx.uid))
        return days_in_year(stays, ctx.today.year, ctx.today)

    def cmd_tax(self, ctx: Ctx) -> None:
        if not self._tax_allowed(ctx):
            return
        events = self.store.events(ctx.uid)
        if not events:
            self.send(ctx.chat_id, "Записей пока нет. Отметьте въезд: /in 15.03.2026 (без даты запишу сегодня), выезд: /out.")
            return
        stays = build_stays(events)
        total = days_in_year(stays, ctx.today.year, ctx.today)
        ongoing = is_ongoing(stays)
        since = stays[-1].start if ongoing else None
        projected = threshold_date(total, self.catalog.tax.threshold_days, ctx.today, ongoing)
        self.send(ctx.chat_id, texts.tax_text(self.catalog, ctx.today.year, total, since, projected, events[-6:]))

    def cmd_undo(self, ctx: Ctx) -> None:
        if not self._tax_allowed(ctx):
            return
        removed = self.store.delete_last_event(ctx.uid)
        if removed is None:
            self.send(ctx.chat_id, "Нечего отменять.")
            return
        kind, day = removed
        self.send(ctx.chat_id, f"Удалил запись: {'въезд' if kind == 'in' else 'выезд'} {fmt_date(day)}.")

    # ---------- кнопки ----------
    def _cb_menu(self, ctx: Ctx, value: str) -> None:
        if value == "add":
            self.cmd_add(ctx)

    def _cb_add(self, ctx: Ctx, status_id: str) -> None:
        status = self.catalog.statuses.get(status_id)
        if status is None:
            return
        if status.kind == "document":
            limit = self._profile_limit(ctx)
            if self._count_kind(ctx.uid, "document") >= limit:
                msg = f"В вашем тарифе до {limit} документов. Удалите ненужный в /my"
                msg += " или оформите Premium: /premium." if not ctx.premium else "."
                self.send(ctx.chat_id, msg)
                return
        elif self._count_kind(ctx.uid, "trip") >= self.cfg.max_trips:
            self.send(ctx.chat_id, f"Одновременно можно держать до {self.cfg.max_trips} поездок. Удалите прошедшие в /my.")
            return
        self.store.set_state(ctx.uid, {"step": "date", "status_id": status_id})
        self.send(
            ctx.chat_id,
            f"Пришлите {texts.esc(status.date_prompt)} в формате ДД.ММ.ГГГГ, например {fmt_date(ctx.today + timedelta(days=60))}. /cancel — отмена.",
        )

    def _cb_update(self, ctx: Ctx, value: str) -> None:
        profile = self._own_profile(ctx, value)
        if profile is None:
            return
        status = self.catalog.statuses[profile["status_id"]]
        self.store.set_state(ctx.uid, {"step": "date", "status_id": status.id, "replace_pid": profile["id"]})
        self.send(
            ctx.chat_id,
            f"Пришлите новую дату: {texts.esc(status.date_prompt)}, в формате ДД.ММ.ГГГГ. Старую запись заменю. /cancel — отмена.",
        )

    def _cb_checklist(self, ctx: Ctx, status_id: str) -> None:
        status = self.catalog.statuses.get(status_id)
        if status:
            self.send(ctx.chat_id, texts.checklist_text(status, self.catalog))

    def _cb_delete(self, ctx: Ctx, value: str) -> None:
        profile = self._own_profile(ctx, value)
        if profile is None:
            return
        status = self.catalog.statuses[profile["status_id"]]
        self.send(
            ctx.chat_id,
            f"Удалить «{texts.esc(status.title)}» и напоминания по нему?",
            _kb([[("Да, удалить", f"delyes:{profile['id']}"), ("Нет", "menu:none")]]),
        )

    def _cb_delete_yes(self, ctx: Ctx, value: str) -> None:
        profile = self._own_profile(ctx, value)
        if profile is None:
            return
        self.store.delete_profile(profile["id"])
        self.send(ctx.chat_id, "Удалено.")

    def _cb_forget(self, ctx: Ctx, value: str) -> None:
        if value == "yes":
            self.store.delete_user(ctx.uid)
            self.send(ctx.chat_id, "Все ваши данные удалены. Если захотите вернуться, пришлите /start.")
        else:
            self.send(ctx.chat_id, "Ничего не удалял.")

    def _own_profile(self, ctx: Ctx, value: str):
        try:
            profile = self.store.get_profile(int(value))
        except ValueError:
            return None
        if profile is None or profile["user_id"] != ctx.uid or profile["status_id"] not in self.catalog.statuses:
            return None
        return profile

    # ---------- ввод даты ----------
    def _on_date(self, ctx: Ctx, state: dict, text: str) -> None:
        status = self.catalog.statuses.get(state.get("status_id", ""))
        if status is None:
            self.store.clear_state(ctx.uid)
            return
        try:
            expiry = parse_date(text)
        except ValueError:
            self.send(ctx.chat_id, "Не понял дату. Нужен формат ДД.ММ.ГГГГ, например 15.03.2027. /cancel — отмена.")
            return
        if status.kind == "trip":
            low, high = ctx.today, ctx.today + timedelta(days=365)
        else:
            low, high = ctx.today - timedelta(days=30), ctx.today + timedelta(days=3650)
        if expiry < low:
            self.send(ctx.chat_id, "Эта дата уже в прошлом. Проверьте число и пришлите ещё раз.")
            return
        if expiry > high:
            self.send(ctx.chat_id, "Эта дата слишком далеко. Проверьте год и пришлите ещё раз.")
            return

        replace_pid = state.get("replace_pid")
        if replace_pid is not None:
            old = self.store.get_profile(replace_pid)
            if old is not None and old["user_id"] == ctx.uid:
                self.store.delete_profile(replace_pid)

        now_iso = self.now().isoformat()
        pid = self.store.add_profile(ctx.uid, status.id, expiry, now_iso)
        left = days_left(expiry, ctx.today)
        self.store.mark_sent(pid, skipped_on_create(left, status.leads), now_iso, skipped=True)
        self.store.clear_state(ctx.uid)
        pending = [lead for lead in status.leads if lead < left]
        self.send(
            ctx.chat_id,
            texts.profile_added(status, expiry, left, pending, self.cfg.send_hour),
            _kb([[("☑ Чек-лист", f"ck:{status.id}")]]),
        )

    # ---------- напоминания ----------
    def tick(self) -> int:
        """Отправляет все напоминания, срок которых наступил. Возвращает число отправленных."""
        now = self.now()
        now_iso = now.isoformat()
        sent_count = 0
        for row in self.store.profiles_with_tz():
            status = self.catalog.statuses.get(row["status_id"])
            if status is None:
                continue
            try:
                tz = get_zone(row["tz"])
            except (ZoneInfoNotFoundError, ValueError, OSError):
                tz = get_zone(self.cfg.default_tz)
            local = now.astimezone(tz)
            expiry = date.fromisoformat(row["expiry"])
            left = days_left(expiry, local.date())

            if status.kind == "trip" and left < 0:
                self.store.delete_profile(row["id"])  # поездка прошла
                continue
            if local.hour < self.cfg.send_hour:
                continue
            lead, eligible = pick_due(left, status.leads, self.store.sent_leads(row["id"]))
            if lead is None:
                continue
            try:
                self.send(row["user_id"], texts.reminder(status, expiry, lead, left, self.catalog))
            except TelegramError as e:
                if e.code == 403:  # пользователь заблокировал бота: не пытаемся снова
                    self.store.mark_sent(row["id"], eligible, now_iso, skipped=True)
                log.warning("не удалось отправить напоминание (профиль %s): %s", row["id"], e)
                continue
            self.store.mark_sent(row["id"], [lead], now_iso, skipped=False)
            self.store.mark_sent(row["id"], [x for x in eligible if x != lead], now_iso, skipped=True)
            sent_count += 1
        return sent_count

    # ---------- основной цикл ----------
    def run_forever(self) -> None:
        try:
            self.api.call(
                "setMyCommands",
                commands=[
                    {"command": "add", "description": "Добавить срок документа"},
                    {"command": "my", "description": "Мои сроки"},
                    {"command": "checklist", "description": "Что подготовить"},
                    {"command": "tax", "description": "Счётчик дней для налогов"},
                    {"command": "sources", "description": "Источники данных"},
                    {"command": "premium", "description": "Что входит в Premium"},
                    {"command": "help", "description": "Все команды"},
                ],
            )
        except TelegramError as e:
            log.warning("setMyCommands не удалось: %s", e)

        offset = self.store.get_offset()
        last_tick = 0.0
        log.info("бот запущен")
        while True:
            try:
                updates = self.api.call(
                    "getUpdates",
                    offset=offset,
                    timeout=self.cfg.poll_timeout,
                    allowed_updates=["message", "callback_query"],
                )
            except TelegramError as e:
                log.warning("getUpdates: %s", e)
                time.sleep(5)
                updates = []
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    self.handle_update(update)
                except Exception:
                    log.exception("ошибка при обработке обновления %s", update.get("update_id"))
                self.store.set_offset(offset)
            if time.monotonic() - last_tick >= self.cfg.tick_seconds:
                last_tick = time.monotonic()
                try:
                    self.tick()
                except Exception:
                    log.exception("ошибка рассылки напоминаний")
