"""Сквозные сценарии: настоящий диспетчер, настоящее хранилище, подставной Telegram и управляемые часы."""
from datetime import date, datetime, timedelta, timezone

import pytest

from relocbot.bot import Bot, Config
from relocbot.data import load_catalog
from relocbot.store import Store
from relocbot.tg import TelegramError

UTC = timezone.utc
START = datetime(2026, 11, 1, 2, 0, tzinfo=UTC)  # 10:00 по Куала-Лумпуру


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.fail_for = {}

    def call(self, method, **params):
        self.calls.append((method, params))
        if method == "sendMessage" and params["chat_id"] in self.fail_for:
            raise self.fail_for[params["chat_id"]]
        return {}

    def sent(self, chat_id=None):
        return [p for m, p in self.calls if m == "sendMessage" and (chat_id is None or p["chat_id"] == chat_id)]

    def last(self, chat_id=None):
        return self.sent(chat_id)[-1]["text"]

    def keyboard_data(self, chat_id=None):
        kb = self.sent(chat_id)[-1].get("reply_markup")
        return [b["callback_data"] for row in kb["inline_keyboard"] for b in row] if kb else []


class Clock:
    def __init__(self, dt):
        self.dt = dt

    def __call__(self):
        return self.dt

    def advance(self, **kw):
        self.dt += timedelta(**kw)


@pytest.fixture
def env():
    api, clock = FakeAPI(), Clock(START)
    store = Store()
    cfg = Config(admin_ids=frozenset({1}), send_hour=9, contact="@admin")
    bot = Bot(api, store, load_catalog(), cfg, now_fn=clock)
    return bot, api, store, clock


def msg(uid, text, chat_type="private"):
    return {"update_id": 1, "message": {"chat": {"id": uid, "type": chat_type}, "from": {"id": uid, "is_bot": False}, "text": text}}


def cb(uid, data):
    return {"update_id": 1, "callback_query": {"id": "q", "from": {"id": uid}, "data": data, "message": {"chat": {"id": uid, "type": "private"}}}}


def add_profile(bot, uid, status_id, date_text):
    bot.handle_update(cb(uid, f"add:{status_id}"))
    bot.handle_update(msg(uid, date_text))


# ---------- основное ----------

def test_start_and_help(env):
    bot, api, *_ = env
    bot.handle_update(msg(5, "/start"))
    assert "Малайзия" in api.last(5)
    assert "menu:add" in api.keyboard_data(5)
    bot.handle_update(msg(5, "/help"))
    assert "/add" in api.last(5)


def test_unknown_command_and_plain_text(env):
    bot, api, *_ = env
    bot.handle_update(msg(5, "/nope"))
    assert "Не знаю" in api.last(5)
    bot.handle_update(msg(5, "привет"))
    assert "/add" in api.last(5)


def test_group_chats_are_ignored(env):
    bot, api, *_ = env
    bot.handle_update(msg(5, "/start", chat_type="group"))
    assert api.sent() == []


def test_add_flow_creates_profile(env):
    bot, api, store, _ = env
    bot.handle_update(msg(5, "/add"))
    assert "add:de_rantau" in api.keyboard_data(5)
    bot.handle_update(cb(5, "add:de_rantau"))
    assert store.get_state(5)["status_id"] == "de_rantau"
    bot.handle_update(msg(5, "15.03.2027"))
    profiles = store.list_profiles(5)
    assert len(profiles) == 1 and profiles[0]["expiry"] == "2027-03-15"
    text = api.last(5)
    assert "134 дня" in text or "134 дней" in text  # осталось 134 дня
    assert store.get_state(5) is None
    assert store.sent_leads(profiles[0]["id"]) == set()  # до всех сроков далеко


def test_add_with_close_date_skips_passed_leads(env):
    bot, api, store, _ = env
    add_profile(bot, 5, "de_rantau", "21.11.2026")  # 20 дней
    pid = store.list_profiles(5)[0]["id"]
    assert store.sent_leads(pid) == {120, 90, 60, 30}
    assert "за 14, 7, 1 дн." in api.last(5)


def test_bad_date_keeps_state_and_good_date_recovers(env):
    bot, api, store, _ = env
    bot.handle_update(cb(5, "add:mm2h"))
    bot.handle_update(msg(5, "скоро"))
    assert "Не понял дату" in api.last(5) and store.get_state(5) is not None
    bot.handle_update(msg(5, "01.01.2020"))
    assert "в прошлом" in api.last(5) and store.list_profiles(5) == []
    bot.handle_update(msg(5, "01.01.2099"))
    assert "далеко" in api.last(5) and store.list_profiles(5) == []
    bot.handle_update(msg(5, "01.06.2027"))
    assert len(store.list_profiles(5)) == 1


def test_cancel_and_command_clear_state(env):
    bot, api, store, _ = env
    bot.handle_update(cb(5, "add:mm2h"))
    bot.handle_update(msg(5, "/cancel"))
    assert store.get_state(5) is None
    bot.handle_update(cb(5, "add:mm2h"))
    bot.handle_update(msg(5, "/my"))
    assert store.get_state(5) is None


def test_free_limit_then_premium_grant(env):
    bot, api, store, _ = env
    add_profile(bot, 5, "de_rantau", "15.03.2027")
    bot.handle_update(cb(5, "add:mm2h"))
    assert "до 1 документов" in api.last(5) and "/premium" in api.last(5)
    assert store.get_state(5) is None

    bot.handle_update(msg(1, "/grant 5 30"))  # админ
    assert "до 01.12.2026" in api.last(1)
    bot.handle_update(cb(5, "add:mm2h"))
    bot.handle_update(msg(5, "01.06.2027"))
    assert len(store.list_profiles(5)) == 2


def test_grant_requires_admin_and_valid_target(env):
    bot, api, store, _ = env
    bot.handle_update(msg(7, "/start"))
    n = len(api.sent())
    bot.handle_update(msg(7, "/grant 7 365"))
    bot.handle_update(msg(7, "/stats"))
    assert len(api.sent()) == n  # молчит, не выдаёт себе Premium
    assert store.premium_until(7) is None
    bot.handle_update(msg(1, "/grant 999 30"))
    assert "нет" in api.last(1)
    bot.handle_update(msg(1, "/grant abc"))
    assert "Формат" in api.last(1)


def test_trips_do_not_use_document_limit_but_have_own_cap(env):
    bot, api, store, _ = env
    add_profile(bot, 5, "de_rantau", "15.03.2027")
    for i in range(3):
        add_profile(bot, 5, "mdac_trip", (date(2026, 12, 1) + timedelta(days=i)).strftime("%d.%m.%Y"))
    assert len(store.list_profiles(5)) == 4
    bot.handle_update(cb(5, "add:mdac_trip"))
    assert "до 3 поездок" in api.last(5)


def test_trip_date_must_not_be_in_past(env):
    bot, api, store, _ = env
    bot.handle_update(cb(5, "add:mdac_trip"))
    bot.handle_update(msg(5, "01.10.2026"))
    assert "в прошлом" in api.last(5)


def test_my_lists_profiles_with_ownership_checks(env):
    bot, api, store, _ = env
    add_profile(bot, 5, "de_rantau", "15.03.2027")
    bot.handle_update(msg(5, "/my"))
    assert "DE Rantau" in api.last(5)
    assert any(d.startswith("upd:") for d in api.keyboard_data(5))


def test_replace_date_after_renewal(env):
    bot, api, store, _ = env
    add_profile(bot, 5, "de_rantau", "15.03.2027")
    old = store.list_profiles(5)[0]["id"]
    bot.handle_update(cb(5, f"upd:{old}"))
    bot.handle_update(msg(5, "15.03.2028"))
    profiles = store.list_profiles(5)
    assert len(profiles) == 1 and profiles[0]["expiry"] == "2028-03-15" and profiles[0]["id"] != old
    assert store.sent_leads(old) == set()


def test_delete_flow_and_foreign_profile_is_protected(env):
    bot, api, store, _ = env
    add_profile(bot, 5, "de_rantau", "15.03.2027")
    pid = store.list_profiles(5)[0]["id"]
    bot.handle_update(msg(6, "/start"))
    bot.handle_update(cb(6, f"delyes:{pid}"))  # чужой пользователь
    assert len(store.list_profiles(5)) == 1
    bot.handle_update(cb(6, f"upd:{pid}"))
    assert store.get_state(6) is None
    bot.handle_update(cb(5, f"del:{pid}"))
    assert f"delyes:{pid}" in api.keyboard_data(5)
    bot.handle_update(cb(5, f"delyes:{pid}"))
    assert store.list_profiles(5) == []
    bot.handle_update(cb(5, "delyes:abc"))  # мусор в callback не роняет бота
    bot.handle_update(cb(5, "unknown:1"))


def test_checklist_buttons(env):
    bot, api, *_ = env
    bot.handle_update(msg(5, "/checklist"))
    assert "ck:de_rantau" in api.keyboard_data(5)
    bot.handle_update(cb(5, "ck:de_rantau"))
    assert "Чек-лист" in api.last(5) and "☐" in api.last(5)
    bot.handle_update(cb(5, "ck:нет_такого"))  # не падает


def test_sources_and_premium_texts(env):
    bot, api, store, _ = env
    bot.handle_update(msg(5, "/sources"))
    assert "imigresen-online.imi.gov.my" in "".join(p["text"] for p in api.sent(5))
    bot.handle_update(msg(5, "/premium"))
    assert "@admin" in api.last(5)
    store.set_premium_until(5, date(2027, 1, 1))
    bot.handle_update(msg(5, "/premium"))
    assert "активен до 01.01.2027" in api.last(5)


def test_forget_deletes_everything(env):
    bot, api, store, _ = env
    add_profile(bot, 5, "de_rantau", "15.03.2027")
    bot.handle_update(msg(5, "/forget"))
    assert "forget:yes" in api.keyboard_data(5)
    bot.handle_update(cb(5, "forget:no"))
    assert store.get_user(5) is not None
    bot.handle_update(cb(5, "forget:yes"))
    assert store.get_user(5) is None and store.list_profiles(5) == []


def test_tz_command(env):
    bot, api, store, _ = env
    bot.handle_update(msg(5, "/tz"))
    assert "Asia/Kuala_Lumpur" in api.last(5)
    bot.handle_update(msg(5, "/tz Mars/Olympus"))
    assert "Не нашёл" in api.last(5) and store.get_user(5)["tz"] == "Asia/Kuala_Lumpur"
    try:
        bot.handle_update(msg(5, "/tz Asia/Kuala_Lumpur"))
    except Exception:
        pytest.skip("в системе нет базы часовых поясов")
    assert "Часовой пояс" in api.last(5)


# ---------- налоговый счётчик ----------

def test_tax_tracker_is_gated_for_free_users(env):
    bot, api, store, _ = env
    bot.handle_update(msg(5, "/in 01.10.2026"))
    assert "Premium" in api.last(5)
    assert store.events(5) == []
    bot.handle_update(msg(5, "/tax"))
    assert "Premium" in api.last(5)


def test_tax_tracker_flow(env):
    bot, api, store, _ = env
    bot.handle_update(msg(5, "/start"))
    store.set_premium_until(5, date(2027, 1, 1))

    bot.handle_update(msg(5, "/out 05.10.2026"))
    assert "Не получилось" in api.last(5)  # выезд без въезда
    bot.handle_update(msg(5, "/in 01.10.2026"))
    assert "32 дн." in api.last(5)  # 1 октября по 1 ноября включительно
    bot.handle_update(msg(5, "/in 02.10.2026"))
    assert "Не получилось" in api.last(5)  # уже в стране
    bot.handle_update(msg(5, "/out 05.10.2026"))
    assert "5 дн." in api.last(5)
    bot.handle_update(msg(5, "/in 01.12.2026"))
    assert "ещё не наступила" in api.last(5)
    bot.handle_update(msg(5, "/in вчера"))
    assert "Не понял дату" in api.last(5)

    bot.handle_update(msg(5, "/tax"))
    assert "5 из 182" in api.last(5) and "вне Малайзии" in api.last(5)
    bot.handle_update(msg(5, "/in"))  # без даты: сегодня
    bot.handle_update(msg(5, "/tax"))
    assert "6 из 182" in api.last(5) and "Сейчас в Малайзии" in api.last(5)

    bot.handle_update(msg(5, "/undo"))
    assert "въезд 01.11.2026" in api.last(5)
    bot.handle_update(msg(5, "/undo"))
    bot.handle_update(msg(5, "/undo"))
    bot.handle_update(msg(5, "/undo"))
    assert "Нечего отменять" in api.last(5)
    bot.handle_update(msg(5, "/tax"))
    assert "Записей пока нет" in api.last(5)


def test_tax_tracker_can_be_made_free(env):
    bot, api, store, _ = env
    bot.cfg.tax_tracker_premium = False
    bot.handle_update(msg(5, "/in 01.10.2026"))
    assert len(store.events(5)) == 1


# ---------- напоминания ----------

def test_reminders_fire_once_at_the_right_time(env):
    bot, api, store, clock = env
    add_profile(bot, 5, "de_rantau", "02.12.2026")  # 31 день
    n = len(api.sent(5))
    assert bot.tick() == 0  # 31 дней: ближайший срок 30 ещё не наступил

    clock.advance(days=1)  # осталось 30
    assert bot.tick() == 1
    assert "30 дней" in api.last(5) and "DE Rantau" in api.last(5)
    assert bot.tick() == 0  # повтор в тот же день не отправляется
    assert len(api.sent(5)) == n + 1

    clock.dt = datetime(2026, 11, 27, 2, 0, tzinfo=UTC)  # осталось 5, бот «простаивал»
    assert bot.tick() == 1
    assert "5 дней" in api.last(5)
    assert bot.tick() == 0
    pid = store.list_profiles(5)[0]["id"]
    assert {14, 7} <= store.sent_leads(pid)  # более ранние сроки закрыты без отправки


def test_reminders_wait_for_send_hour(env):
    bot, api, store, clock = env
    add_profile(bot, 5, "de_rantau", "01.12.2026")  # 30 дней
    clock.dt = datetime(2026, 11, 1, 0, 30, tzinfo=UTC)  # 08:30 в Куала-Лумпуре
    assert bot.tick() == 0
    clock.dt = datetime(2026, 11, 1, 1, 0, tzinfo=UTC)  # 09:00
    assert bot.tick() == 0  # lead 30 пропущен при создании (остаток уже 30), следующий срок 14
    clock.dt = datetime(2026, 11, 18, 0, 30, tzinfo=UTC)  # осталось 13, но 08:30
    assert bot.tick() == 0
    clock.dt = datetime(2026, 11, 18, 1, 0, tzinfo=UTC)
    assert bot.tick() == 1


def test_expiry_day_and_overdue_reminders(env):
    bot, api, store, clock = env
    add_profile(bot, 5, "de_rantau", "05.11.2026")  # 4 дня
    clock.dt = datetime(2026, 11, 5, 2, 0, tzinfo=UTC)
    assert bot.tick() == 1
    assert "истекает сегодня" in api.last(5)
    clock.dt = datetime(2026, 11, 8, 2, 0, tzinfo=UTC)
    assert bot.tick() == 1
    assert "истёк 3 дня назад" in api.last(5)
    clock.dt = datetime(2026, 11, 20, 2, 0, tzinfo=UTC)
    assert bot.tick() == 0  # больше не беспокоим


def test_trip_reminders_and_cleanup(env):
    bot, api, store, clock = env
    add_profile(bot, 5, "mdac_trip", "10.11.2026")  # 9 дней
    clock.dt = datetime(2026, 11, 7, 2, 0, tzinfo=UTC)  # осталось 3
    assert bot.tick() == 1
    assert "MDAC" in api.last(5) and "бесплатно" in api.last(5)
    clock.dt = datetime(2026, 11, 12, 2, 0, tzinfo=UTC)  # поездка прошла
    assert bot.tick() == 0
    assert store.list_profiles(5) == []


def test_blocked_user_is_not_retried(env):
    bot, api, store, clock = env
    add_profile(bot, 5, "de_rantau", "05.11.2026")
    clock.dt = datetime(2026, 11, 5, 2, 0, tzinfo=UTC)
    api.fail_for[5] = TelegramError("sendMessage", "Forbidden: bot was blocked by the user", 403)
    assert bot.tick() == 0
    api.fail_for.clear()
    assert bot.tick() == 0  # напоминание закрыто, повторов нет


def test_temporary_error_is_retried_next_tick(env):
    bot, api, store, clock = env
    add_profile(bot, 5, "de_rantau", "05.11.2026")
    clock.dt = datetime(2026, 11, 5, 2, 0, tzinfo=UTC)
    api.fail_for[5] = TelegramError("sendMessage", "сетевая ошибка", None)
    assert bot.tick() == 0
    api.fail_for.clear()
    assert bot.tick() == 1


def test_reminders_follow_user_timezone(env):
    bot, api, store, clock = env
    add_profile(bot, 5, "de_rantau", "01.12.2026")  # 30 дней
    try:
        bot.handle_update(msg(5, "/tz Europe/Moscow"))
    except Exception:
        pytest.skip("в системе нет базы часовых поясов")
    if store.get_user(5)["tz"] != "Europe/Moscow":
        pytest.skip("в системе нет базы часовых поясов")
    clock.dt = datetime(2026, 11, 18, 5, 0, tzinfo=UTC)  # 08:00 в Москве, 13:00 в Куала-Лумпуре
    assert bot.tick() == 0
    clock.dt = datetime(2026, 11, 18, 6, 30, tzinfo=UTC)  # 09:30 в Москве
    assert bot.tick() == 1


def test_run_loop_survives_handler_errors(env, monkeypatch):
    """Ошибка в одном обновлении не должна останавливать бота и не должна терять смещение."""
    bot, api, store, clock = env
    calls = {"n": 0}

    def fake_call(method, **params):
        if method == "getUpdates":
            calls["n"] += 1
            if calls["n"] == 1:
                return [{"update_id": 10, "message": {"chat": {"id": 5, "type": "private"}, "from": {"id": 5}, "text": "/boom"}}]
            raise KeyboardInterrupt
        return {}

    bot.api.call = fake_call
    bot.commands["boom"] = lambda ctx: 1 / 0
    with pytest.raises(KeyboardInterrupt):
        bot.run_forever()
    assert store.get_offset() == 11
