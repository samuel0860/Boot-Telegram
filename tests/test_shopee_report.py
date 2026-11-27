"""Resultado na Shopee: pedidos e comissão dos seus links (relatório de conversões da API)."""
from __future__ import annotations

import json
import re
import time
from datetime import timedelta
from decimal import Decimal

import httpx
from fastapi.testclient import TestClient

from core.audit import utcnow
from database import session_scope
from offers import shopee_report
from offers.config import TZ
from offers.shopee import ShopeeClient
from tests.fakes import make_bot
from tests.test_offers_shopee import APP_ID, SECRET, configure_shopee

NOW = int(time.time()) - 120


def _node(conv, order, status, commission, sub="", item="Air Fryer Mondial", ts=NOW):
    return {"purchaseTime": ts, "conversionId": conv, "totalCommission": commission, "utmContent": sub,
            "orders": [{"orderId": order, "orderStatus": status,
                        "items": [{"itemId": 2222, "itemName": item, "qty": 1, "itemTotalCommission": commission}]}]}


PAGES = [
    [_node(1, "A1", "PENDING", "9.50", "bot1-tg-h20"), _node(2, "A2", "COMPLETED", "4.80", "bot1-wa-h12", "Fone JBL")],
    [_node(3, "A3", "CANCELLED", "7.00", "bot1-tg-h20"), _node(4, "A4", "PENDING", "2.00", "", "Chinelo",
                                                                ts=NOW - 3 * 86400)],
]


class FakeReport:
    def __init__(self, pages=PAGES, fail=False):
        self.pages, self.fail, self.queries = pages, fail, []

    def handler(self, request: httpx.Request) -> httpx.Response:
        query = json.loads(request.content)["query"]
        self.queries.append(query)
        if self.fail:
            return httpx.Response(200, json={"errors": [{"message": "Invalid Signature", "extensions": {"code": 10020}}]})
        page = 1 if "scrollId:" in query else 0
        info = {"hasNextPage": page == 0, "scrollId": "SCROLL-2" if page == 0 else ""}
        return httpx.Response(200, json={"data": {"conversionReport": {"nodes": self.pages[page], "pageInfo": info}}})

    def factory(self):
        return lambda cfg=None: ShopeeClient(APP_ID, SECRET, transport=httpx.MockTransport(self.handler))


def test_report_paginates_and_summarizes():
    fake = FakeReport()
    with fake.factory()() as c:
        nodes = c.conversion_report(NOW - 86400 * 90, NOW + 60)
    assert len(nodes) == 4 and 'scrollId: "SCROLL-2"' in fake.queries[1]
    assert "purchaseTimeStart:" in fake.queries[0] and "limit: 500" in fake.queries[0]

    rows = shopee_report.to_rows(nodes)
    today = utcnow().astimezone(TZ).date()
    day = shopee_report.summarize(rows, today, today, {1: "Ofertas Tech"})
    assert day["orders"] == 2 and day["commission"] == Decimal("14.30") and day["cancelled"] == 1
    assert day["groups"][0] == (("Ofertas Tech", "Telegram", "20h"), 1, Decimal("9.50"))
    assert day["tracked"] and day["products"][0][0] == "Air Fryer Mondial"
    week = shopee_report.summarize(rows, today - timedelta(days=6), today)
    assert week["orders"] == 3 and week["commission"] == Decimal("16.30")
    assert shopee_report.decode_sub("", {}) == ("—", "—", "—")


def test_report_is_cached_for_15_minutes():
    configure_shopee()
    fake = FakeReport()
    with session_scope() as s:
        first = shopee_report.load(s, fake.factory())
    calls = len(fake.queries)
    with session_scope() as s:
        again = shopee_report.load(s, fake.factory())
        assert len(fake.queries) == calls and len(again.rows) == len(first.rows)     # veio do cache
        later = shopee_report.load(s, fake.factory(), now=utcnow() + timedelta(minutes=16))
        assert len(fake.queries) > calls and later.available


def test_panel_shows_shopee_result(fake_client):
    from admin.auth import hash_password
    from admin.views import offers as views
    from app import app
    from models import AdminUser

    configure_shopee()
    make_bot("Ofertas Tech")
    with session_scope() as s:
        s.add(AdminUser(username="admin", password_hash=hash_password("SenhaForte123"), is_superuser=True))
    old = views.shopee_client_factory
    views.shopee_client_factory = FakeReport().factory()
    try:
        with TestClient(app) as c:
            tok = lambda url: re.search(r'name="csrf_token" value="([^"]+)"', c.get(url).text).group(1)  # noqa: E731
            c.post("/admin/login", data={"username": "admin", "password": "SenhaForte123", "csrf_token": tok("/admin/login")})
            home = c.get("/admin/").text
            assert "Resultado na Shopee" in home and "Pedidos hoje" in home and "R$\xa014,30" in home
            assert "Receita do bot de vendas" in home
            stats = c.get("/admin/offers/stats?days=7").text
            assert "Ofertas Tech" in stats and "Telegram" in stats and "20h" in stats and "R$\xa016,30" in stats
            views.shopee_client_factory = FakeReport(fail=True).factory()
            r = c.post("/admin/offers/stats/shopee-refresh", data={"csrf_token": tok("/admin/offers/stats"),
                                                                    "back": "/admin/"})
            assert "Não consegui ler o relatório da Shopee" in r.text
    finally:
        views.shopee_client_factory = old


def test_finance_summary_and_page(fake_client):
    from admin.auth import hash_password
    from admin.views import offers as views
    from app import app
    from models import AdminUser
    from offers import finance

    rows = shopee_report.to_rows(PAGES[0] + PAGES[1])
    with session_scope() as s:
        f = finance.build(s, rows, "7", utcnow(), shopee_connected=True, other_stores={"Amazon": False})
    assert f["pending"] == Decimal("11.50") and f["confirmed"] == Decimal("4.80") and f["cancelled"] == Decimal("7.00")
    assert f["total"] == Decimal("16.30") and f["orders"] == 3 and f["chart"] and f["products"][0][0] == "Air Fryer Mondial"
    with session_scope() as s:
        month = finance.build(s, rows, "mes", utcnow())
    assert month["forecast"] is not None

    configure_shopee()
    with session_scope() as s:
        s.add(AdminUser(username="admin", password_hash=hash_password("SenhaForte123"), is_superuser=True))
    old = views.shopee_client_factory
    views.shopee_client_factory = FakeReport().factory()
    try:
        with TestClient(app) as c:
            tok = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/admin/login").text).group(1)
            c.post("/admin/login", data={"username": "admin", "password": "SenhaForte123", "csrf_token": tok})
            page = c.get("/admin/finance?period=7").text
            assert "Financeiro" in page and "Comissão garantida" in page and "R$\xa016,30" in page
            assert "Ganhos por dia" in page and "Air Fryer Mondial" in page
            assert c.get("/admin/finance?period=hoje").status_code == 200
    finally:
        views.shopee_client_factory = old
