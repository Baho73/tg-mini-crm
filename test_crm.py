import os
import tempfile

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["CRM_USER"], os.environ["CRM_PASSWORD"] = "u", "p"
os.environ.pop("BOT_TOKEN", None)

from fastapi.testclient import TestClient  # noqa: E402

import crm  # noqa: E402

A = ("u", "p")
SAME = {"origin": "http://testserver"}


def test_flow():
    with TestClient(crm.app) as c:
        assert c.get("/").status_code == 401
        c.post("/lead", data={"name": "Анна", "contact": "+7", "request": "сайт", "tag": "Горячий"},
               auth=A, headers=SAME)
        bot_id = crm.add_lead("Борис", "@b", "бот", "bot", 42)
        c.post("/tag", data={"lead_id": bot_id, "tag": "холодный"}, auth=A, headers=SAME)
        # личный Telegram: второе сообщение дописывается, а не плодит лида
        t1 = crm.upsert_tg_message(7, "Вера", "@v", "привет", "telegram", "conn1")
        assert crm.upsert_tg_message(7, "Вера", "@v", "нужен бот", "telegram", "conn1") == t1
        # тот же клиент пишет другому менеджеру: отдельный лид
        assert crm.upsert_tg_message(7, "Вера", "@v", "и вам привет", "telegram", "conn2") != t1
        assert [r["name"] for r in crm.list_leads("горячий")] == ["Анна"]
        assert len(crm.list_leads()) == 4
        page = c.get("/?tag=холодный", auth=A).text
        assert "Борис" in page and "Анна" not in page.split("<table>")[1]
        c.post("/untag", data={"lead_id": bot_id, "tag": "холодный"}, auth=A, headers=SAME)
        assert crm.list_leads("холодный") == []


def test_review_fixes():
    with TestClient(crm.app) as c:
        lid = crm.add_lead("Гена", "", "", "manual")
        # CSRF: POST без своего Origin/Referer отклоняется
        assert c.post("/tag", data={"lead_id": lid, "tag": "x"}, auth=A).status_code == 403
        # несуществующий лид: 404, а не 500
        assert c.post("/tag", data={"lead_id": 999999, "tag": "x"}, auth=A, headers=SAME).status_code == 404
        # запятая не разваливает тег, спецсимволы кодируются в ссылке
        c.post("/tag", data={"lead_id": lid, "tag": "sales&vip,b"}, auth=A, headers=SAME)
        assert [r["name"] for r in crm.list_leads("sales&vip b")] == ["Гена"]
        assert "tag=sales%26vip+b" in c.get("/", auth=A).text
