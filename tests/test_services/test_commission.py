# tests/test_services/test_commission.py
"""Commission des agents : % de leurs ventes (mises encaissées), taux par
défaut réglé par l'admin, taux personnel par agent, affichée à l'agent
(tableau de bord, rapports, profil) et comptée dans les finances admin."""

import re
from datetime import timedelta
from decimal import Decimal

import pytest

from app.core.exceptions import ValidationException
from app.core.timezone import now_utc
from app.models.enums import KenoBetStatus
from app.models.game import GameBet
from app.models.keno import KenoBet
from app.models.setting import SystemSetting
from app.services.commission_service import (
    DEFAULT_RATE_KEY, LEGACY_REDIS_KEY, CommissionService, freeze_commission, get_default_rate, parse_rate, set_default_rate,
)
from app.services.finance_report_service import FinanceReportService
from app.services.keno_service import KenoService
from app.services.lucky6_service import Lucky6Service
from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD


async def _sales(db, fake_redis, agent, player):
    """Ventes de l'agent : Keno 100 (+ 30 remboursé, ignoré), Lucky6 200 gagné
    + 50 en attente (comptés : la vente a eu lieu), Lucky6 70 annulé (ignoré)."""
    keno = KenoService(db, fake_redis)
    draw = await keno.generate_draw(draw_time=now_utc() + timedelta(minutes=5), config=await keno.get_config())
    for status, stake in ((KenoBetStatus.LOST, "100"), (KenoBetStatus.REFUNDED, "30")):
        db.add(KenoBet(draw_id=draw.id, user_id=player.id, agent_id=agent.id, picks=[1, 2], stake=Decimal(stake),
                       status=status, winnings=Decimal("0"), placed_at=now_utc()))
    race = await Lucky6Service(db, fake_redis).create_race()
    for status, stake in (("WON", "200"), ("PENDING", "50"), ("REFUNDED", "70")):
        db.add(GameBet(round_id=race.id, game_type="lucky6", user_id=player.id, agent_id=agent.id, bet_type="SIX",
                       selection=[1, 2, 3, 4, 5, 6], stake=Decimal(stake), odds=Decimal("6"), potential_payout=Decimal("0"),
                       status=status, winnings=Decimal("0"), placed_at=now_utc()))
    await db.flush()


def _range():
    return now_utc() - timedelta(days=1), now_utc() + timedelta(days=1)


def test_parse_rate():
    assert parse_rate("7,5") == Decimal("7.50") and parse_rate(0) == Decimal("0.00")
    for bad in ("-1", "51", "abc", "nan"):
        with pytest.raises(ValidationException):
            parse_rate(bad)


@pytest.mark.asyncio
async def test_default_rate_is_stored_in_database(db_session, fake_redis):
    assert await get_default_rate(db_session, fake_redis) == Decimal("0.00")  # .env par défaut
    await fake_redis.set(LEGACY_REDIS_KEY, "7")  # ancien réglage (Redis seul) : repris
    assert await get_default_rate(db_session, fake_redis) == Decimal("7.00")
    await set_default_rate(db_session, "10", by="admin")
    assert (await db_session.get(SystemSetting, DEFAULT_RATE_KEY)).value == "10.00"
    await fake_redis.delete(LEGACY_REDIS_KEY)  # Redis vidé : le taux ne disparaît plus
    assert await get_default_rate(db_session, None) == Decimal("10.00")


@pytest.mark.asyncio
async def test_commission_is_rate_times_sales(db_session, fake_redis, make_agent, make_user):
    agent = await make_agent()
    other = await make_agent()
    await _sales(db_session, fake_redis, agent, await make_user())
    await set_default_rate(db_session, "10")
    service = CommissionService(db_session, fake_redis)

    c = await service.for_agent(agent, *_range())
    assert c == {"rate": 10.0, "custom": False, "default_rate": 10.0, "sales": 350.0, "commission": 35.0}
    assert (await service.for_agent(other, *_range()))["commission"] == 0.0

    agent.commission_rate = Decimal("12.5")  # taux personnel
    c = await service.for_agent(agent, *_range())
    assert c["rate"] == 12.5 and c["custom"] and c["commission"] == 43.75

    rows = {r["agent_id"]: r for r in await service.by_agent(*_range())}
    assert rows[agent.id]["commission"] == 43.75 and rows[other.id]["sales"] == 0.0
    assert await service.totals(list(_range())) == [Decimal("43.75")]

    old = await service.for_agent(agent, now_utc() - timedelta(days=60), now_utc() - timedelta(days=30))
    assert old["sales"] == 0.0


@pytest.mark.asyncio
async def test_finance_net_revenue_deducts_commissions(db_session, fake_redis, make_agent, make_user):
    agent = await make_agent()
    await _sales(db_session, fake_redis, agent, await make_user())
    await set_default_rate(db_session, "10")
    p = await FinanceReportService(db_session, fake_redis).period(*_range())
    # paris réglés : Keno 100 perdu + Lucky6 200 gagné (gain 0 ici) -> revenu 300
    assert p["revenue"] == 300.0 and p["commissions"] == 35.0 and p["net_revenue"] == 265.0


async def _agent_login(client, agent):
    page = await client.get("/agent/login")
    token = re.search(r'name="csrf_token"\s+value="([^"]*)"', page.text).group(1)
    r = await client.post("/agent/login", data={"code": agent.phone, "password": "AgentPass123!", "csrf_token": token},
                          follow_redirects=False)
    assert r.status_code == 303


@pytest.mark.asyncio
async def test_agent_screens_show_commission(agent_client, db_session, fake_redis, make_agent, make_user):
    agent = await make_agent()
    await _sales(db_session, fake_redis, agent, await make_user())
    await set_default_rate(db_session, "10")
    await _agent_login(agent_client, agent)
    for path in ("/agent/dashboard", "/agent/reports", "/agent/profile"):
        r = await agent_client.get(path)
        assert r.status_code == 200, (path, r.text[:1500])
        assert "Commission (10 %)" in r.text and "35,00 HTG" in r.text, path
        assert "350,00 HTG de ventes" in r.text or "ventes 350,00 HTG" in r.text, path


async def _admin_login(client):
    page = await client.get("/admin/login")
    token = re.search(r'name="csrf_token"\s+value="([^"]*)"', page.text).group(1)
    r = await client.post("/admin/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "csrf_token": token},
                          follow_redirects=False)
    assert r.status_code == 303


async def _csrf(client):
    page = await client.get("/admin/login")
    return {"X-CSRFToken": re.search(r'name="csrf_token"\s+value="([^"]*)"', page.text).group(1)}


@pytest.mark.asyncio
async def test_admin_sets_default_and_agent_rate(admin_client, admin_user, db_session, fake_redis, make_agent, make_user):
    agent = await make_agent()
    await _sales(db_session, fake_redis, agent, await make_user())
    await _admin_login(admin_client)

    r = await admin_client.post("/admin/api/agents/commission-default", headers=await _csrf(admin_client), json={"rate": "8"})
    assert r.status_code == 200 and r.json()["rate"] == 8.0
    bad = await admin_client.post("/admin/api/agents/commission-default", headers=await _csrf(admin_client), json={"rate": "80"})
    assert bad.status_code == 400

    page = await admin_client.get("/admin/agents")
    assert page.status_code == 200 and "Commission par défaut" in page.text and "28,00 HTG ce mois" in page.text

    r = await admin_client.put(f"/admin/api/agents/{agent.id}", headers=await _csrf(admin_client), json={"commission_rate": "15"})
    assert r.status_code == 200
    await db_session.refresh(agent)
    assert agent.commission_rate == Decimal("15.00")
    assert "52,50 HTG ce mois" in (await admin_client.get("/admin/agents")).text

    edit = await admin_client.get(f"/admin/agents/{agent.id}/edit")
    assert edit.status_code == 200 and 'name="commission_rate"' in edit.text and "Taux par défaut : 8 %" in edit.text

    # vide = retour au taux par défaut ; champ absent = inchangé
    await admin_client.put(f"/admin/api/agents/{agent.id}", headers=await _csrf(admin_client), json={"commission_rate": ""})
    await db_session.refresh(agent)
    assert agent.commission_rate is None
    await admin_client.put(f"/admin/api/agents/{agent.id}", headers=await _csrf(admin_client), json={"first_name": "Jean"})
    await db_session.refresh(agent)
    assert agent.commission_rate is None

    fin = await admin_client.get("/admin/reports/financial")
    assert fin.status_code == 200 and "Commissions des agents" in fin.text and "28,00 HTG" in fin.text


@pytest.mark.asyncio
async def test_commission_is_frozen_at_sale_and_replays_are_not_sales(db_session, fake_redis, make_agent, make_user):
    """La commission est figée au taux du jour de la vente ; un gain rejoué sur le
    même ticket n'est pas une vente."""
    agent = await make_agent()
    player = await make_user()
    race = await Lucky6Service(db_session, fake_redis).create_race()
    await set_default_rate(db_session, "10")

    def bet(stake):
        b = GameBet(round_id=race.id, game_type="lucky6", user_id=player.id, agent_id=agent.id, bet_type="SIX",
                    selection=[1, 2, 3, 4, 5, 6], stake=Decimal(stake), odds=Decimal("6"), potential_payout=Decimal("0"),
                    status="LOST", winnings=Decimal("0"), placed_at=now_utc())
        db_session.add(b)
        return b

    sold = bet("100")
    await freeze_commission(db_session, fake_redis, agent, sold, "cash")
    replay = bet("300")
    await freeze_commission(db_session, fake_redis, agent, replay, "ticket")
    await db_session.flush()
    assert sold.commission == Decimal("10.00") and sold.counts_as_sale
    assert replay.commission == Decimal("0") and replay.counts_as_sale is False

    await set_default_rate(db_session, "20")  # nouveau taux : le passé ne change pas
    c = await CommissionService(db_session, fake_redis).for_agent(agent, *_range())
    assert c["sales"] == 100.0 and c["commission"] == 10.0 and c["rate"] == 20.0
