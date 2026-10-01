import os
import tempfile

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["CRM_USER"], os.environ["CRM_PASSWORD"] = "u", "p"
os.environ.pop("BOT_TOKEN", None)

from fastapi.testclient import TestClient  # noqa: E402

import crm  # noqa: E402


def test_flow():
    with TestClient(crm.app) as c:
        assert c.get("/").status_code == 401
        a = ("u", "p")
        c.post("/lead", data={"name": "Анна", "contact": "+7", "request": "сайт", "tag": "Горячий"}, auth=a)
        bot_id = crm.add_lead("Борис", "@b", "бот", "bot", 42)
        c.post("/tag", data={"lead_id": bot_id, "tag": "холодный"}, auth=a)
        # личный Telegram: второе сообщение дописывается, а не плодит лида
        t1 = crm.upsert_tg_message(7, "Вера", "@v", "привет", "telegram")
        assert crm.upsert_tg_message(7, "Вера", "@v", "нужен бот", "telegram") == t1
        assert [r["name"] for r in crm.list_leads("горячий")] == ["Анна"]
        assert len(crm.list_leads()) == 3
        page = c.get("/?tag=холодный", auth=a).text
        assert "Борис" in page and "Анна" not in page.split("<table>")[1]
        c.post("/untag", data={"lead_id": bot_id, "tag": "холодный"}, auth=a)
        assert crm.list_leads("холодный") == []
