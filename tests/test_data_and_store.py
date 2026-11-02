import json
import re
from datetime import date

import pytest

from relocbot import texts
from relocbot.data import DEFAULT_PATH, load_catalog
from relocbot.store import Store


@pytest.fixture(scope="module")
def catalog():
    return load_catalog()


def test_catalog_loads_and_has_expected_statuses(catalog):
    assert {"de_rantau", "student_pass", "employment_pass", "dependant_pass", "mm2h", "visa_free", "mdac_trip"} <= set(
        catalog.statuses
    )
    assert catalog.tax.threshold_days == 182
    assert [s.id for s in catalog.trips()] == ["mdac_trip"]


def test_every_status_is_consistent(catalog):
    for s in catalog.statuses.values():
        assert list(s.leads) == sorted(set(s.leads), reverse=True), s.id
        assert 0 in s.leads, f"{s.id}: нет напоминания в день окончания"
        assert set(s.lead_notes) <= set(s.leads), s.id
        assert s.facts and s.checklist and s.source_ids, s.id
        for sid in s.source_ids:
            assert sid in catalog.sources


def test_every_document_has_overdue_warning(catalog):
    for s in catalog.documents():
        assert -1 in s.leads, f"{s.id}: нет напоминания о просрочке"


def test_every_source_is_used_and_has_https_url(catalog):
    used = {sid for s in catalog.statuses.values() for sid in s.source_ids} | set(catalog.tax.source_ids)
    assert used == set(catalog.sources), f"неиспользуемые источники: {set(catalog.sources) - used}"
    for src in catalog.sources.values():
        assert src.url.startswith("https://"), src.id
        assert " " not in src.url, f"{src.id}: пробел в URL"


def test_loader_rejects_bad_data(tmp_path):
    raw = json.loads(DEFAULT_PATH.read_text(encoding="utf-8"))
    raw["statuses"][0]["leads"] = [30, 60]  # не убывает
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError):
        load_catalog(p)

    raw = json.loads(DEFAULT_PATH.read_text(encoding="utf-8"))
    raw["statuses"][0]["source_ids"] = ["нет_такого"]
    p.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError):
        load_catalog(p)


def _balanced(html: str) -> bool:
    return all(html.count(f"<{t}>") == html.count(f"</{t}>") for t in ("b", "i"))


def test_texts_fit_telegram_limits_and_html_is_balanced(catalog):
    for s in catalog.statuses.values():
        for text in (
            texts.status_card(s),
            texts.checklist_text(s, catalog),
            texts.profile_added(s, date(2027, 3, 15), 134, [l for l in s.leads if l < 134], 9),
            texts.reminder(s, date(2027, 3, 15), s.leads[0], 10, catalog),
        ):
            assert len(text) < 4096, s.id
            assert _balanced(text), s.id
            assert not re.search(r"<(?!/?(b|i|a)[ >])", text), f"неэкранированный < в {s.id}"
    for part in texts.chunk(texts.sources_text(catalog)):
        assert len(part) <= 3900


def test_chunk_splits_long_text():
    long = "\n\n".join(["абзац " * 200] * 10)
    parts = texts.chunk(long, limit=3900)
    assert len(parts) > 1
    assert all(len(p) <= 3900 for p in parts)
    assert texts.chunk("коротко") == ["коротко"]


# ---------- хранилище ----------

NOW = "2026-11-01T02:00:00+00:00"


def test_store_profiles_and_sent():
    st = Store()
    st.ensure_user(1, NOW, "Asia/Kuala_Lumpur")
    pid = st.add_profile(1, "de_rantau", date(2027, 3, 15), NOW)
    assert [p["status_id"] for p in st.list_profiles(1)] == ["de_rantau"]
    st.mark_sent(pid, [90, 60], NOW, skipped=True)
    st.mark_sent(pid, [60, 30], NOW, skipped=False)  # повтор 60 не ломает и не дублирует
    assert st.sent_leads(pid) == {90, 60, 30}
    st.delete_profile(pid)
    assert st.list_profiles(1) == []
    assert st.sent_leads(pid) == set()


def test_store_events_order_and_undo():
    st = Store()
    st.ensure_user(1, NOW, "Asia/Kuala_Lumpur")
    st.add_event(1, "out", date(2026, 2, 1), NOW)
    st.add_event(1, "in", date(2026, 1, 1), NOW)
    assert st.events(1) == [("in", date(2026, 1, 1)), ("out", date(2026, 2, 1))]
    assert st.delete_last_event(1) == ("in", date(2026, 1, 1))  # последняя по времени записи, а не по дате
    assert st.delete_last_event(1) == ("out", date(2026, 2, 1))
    assert st.delete_last_event(1) is None


def test_store_state_offset_premium():
    st = Store()
    st.ensure_user(1, NOW, "Asia/Kuala_Lumpur")
    assert st.get_state(1) is None
    st.set_state(1, {"step": "date", "status_id": "mm2h"})
    st.set_state(1, {"step": "date", "status_id": "visa_free"})  # перезапись
    assert st.get_state(1)["status_id"] == "visa_free"
    st.clear_state(1)
    assert st.get_state(1) is None
    assert st.get_offset() == 0
    st.set_offset(42)
    st.set_offset(43)
    assert st.get_offset() == 43
    assert st.premium_until(1) is None
    st.set_premium_until(1, date(2027, 1, 1))
    assert st.premium_until(1) == date(2027, 1, 1)


def test_delete_user_removes_everything():
    st = Store()
    st.ensure_user(1, NOW, "Asia/Kuala_Lumpur")
    st.ensure_user(2, NOW, "Asia/Kuala_Lumpur")
    pid = st.add_profile(1, "mm2h", date(2027, 1, 1), NOW)
    st.mark_sent(pid, [90], NOW, skipped=False)
    st.add_event(1, "in", date(2026, 1, 1), NOW)
    st.set_state(1, {"step": "date"})
    other = st.add_profile(2, "mm2h", date(2027, 1, 1), NOW)
    st.delete_user(1)
    assert st.get_user(1) is None
    assert st.list_profiles(1) == [] and st.events(1) == [] and st.get_state(1) is None
    assert st.sent_leads(pid) == set()
    assert st.get_profile(other) is not None  # чужие данные не тронуты
