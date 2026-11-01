# tests/test_services/test_responsible_service.py
"""Jeu responsable : auto-exclusion, pause, levée par un admin, limites du joueur,
détection de comportements à risque."""

from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.exceptions import AppException, NotFoundException
from app.models.enums import TransactionStatus, TransactionType
from app.models.responsible import ExclusionType, PlayerLimit
from app.models.transaction import Transaction
from app.models.wallet import Wallet
from app.schemas.responsible import PlayerLimitRequest, SelfExclusionRequest
from app.services.responsible_service import ResponsibleService


@pytest.fixture
def service(db_session, fake_redis):
    svc = ResponsibleService(db_session, fake_redis)
    svc.notification_service.create_notification = AsyncMock()  # table des notifications absente des tests
    return svc


def _request(kind, days=None):
    return SelfExclusionRequest(exclusion_type=kind, duration_days=days, reason="Je veux m'arrêter")


async def _transaction(db_session, user, kind, amount, minutes_ago=5, ref=None):
    wallet = (await db_session.execute(select(Wallet).where(Wallet.user_id == user.id))).scalar_one()
    tx = Transaction(
        user_id=user.id, wallet_id=wallet.id, reference=ref or f"T-{kind.value}-{minutes_ago}-{amount}",
        transaction_type=kind, amount=Decimal(amount), balance_before=Decimal(0), balance_after=Decimal(0),
        status=TransactionStatus.COMPLETED, created_at=datetime.utcnow() - timedelta(minutes=minutes_ago),
    )
    db_session.add(tx)
    await db_session.flush()
    return tx


# ----------------------------------------------------------------------------- auto-exclusion

@pytest.mark.asyncio
async def test_temporary_exclusion_locks_the_account(service, make_user, db_session):
    user = await make_user()
    exclusion = await service.create_self_exclusion(user.id, _request(ExclusionType.TEMPORARY, 7), "1.2.3.4")
    assert exclusion.is_active and exclusion.end_date - exclusion.start_date == timedelta(days=7)
    await db_session.refresh(user)
    assert user.is_locked and not user.is_active and "temporary" in user.lock_reason

    status = await service.check_exclusion_status(user.id)
    assert status["is_excluded"] and status["remaining_days"] in (6, 7) and status["exclusion_type"] == "temporary"
    assert (await service.get_active_exclusion(user.id)).id == exclusion.id


@pytest.mark.asyncio
async def test_exclusion_type_is_accepted_as_text_and_validated(service, make_user):
    user = await make_user()
    with pytest.raises(AppException, match="invalide"):
        await service.create_self_exclusion(user.id, SelfExclusionRequest(exclusion_type="inconnu"))
    exclusion = await service.create_self_exclusion(user.id, SelfExclusionRequest(exclusion_type="temporary", duration_days=2))
    assert exclusion.exclusion_type == ExclusionType.TEMPORARY  # texte reçu de l'API, comme le fait le schéma


@pytest.mark.asyncio
async def test_exclusion_rules(service, make_user):
    user = await make_user()
    with pytest.raises(AppException, match="Durée requise"):
        await service.create_self_exclusion(user.id, _request(ExclusionType.TEMPORARY))
    await service.create_self_exclusion(user.id, _request(ExclusionType.PERMANENT))
    with pytest.raises(AppException, match="déjà une exclusion"):
        await service.create_self_exclusion(user.id, _request(ExclusionType.TEMPORARY, 3))
    status = await service.check_exclusion_status(user.id)
    assert status["is_excluded"] and status["end_date"] is None and status["remaining_days"] is None


@pytest.mark.asyncio
async def test_no_exclusion_means_not_excluded(service, make_user):
    user = await make_user()
    assert await service.get_active_exclusion(user.id) is None
    assert (await service.check_exclusion_status(user.id))["is_excluded"] is False


@pytest.mark.asyncio
async def test_cooling_off_is_one_day_and_limited_to_48_hours(service, make_user):
    user = await make_user()
    with pytest.raises(AppException, match="48 heures"):
        await service.activate_cooling_off(user.id, hours=72)
    pause = await service.activate_cooling_off(user.id, hours=30)  # 2 jours demandés, plafonnés à 1
    assert pause.exclusion_type == ExclusionType.COOLING_OFF
    assert timedelta(hours=23) < pause.end_date - pause.start_date <= timedelta(days=1, minutes=1)


@pytest.mark.asyncio
async def test_expired_exclusion_unlocks_the_account(service, make_user, db_session):
    user = await make_user()
    exclusion = await service.create_self_exclusion(user.id, _request(ExclusionType.TEMPORARY, 1))
    exclusion.end_date = datetime.utcnow() - timedelta(minutes=1)
    await db_session.flush()
    # une exclusion échue n'est plus « active »
    assert await service.get_active_exclusion(user.id) is None
    # cas intermédiaire : toujours marquée active mais déjà échue -> check_exclusion_status la clôt
    exclusion.end_date = datetime.utcnow() + timedelta(seconds=30)
    await db_session.flush()
    service.get_active_exclusion = AsyncMock(return_value=exclusion)
    exclusion.end_date = datetime.utcnow() - timedelta(seconds=1)
    status = await service.check_exclusion_status(user.id)
    assert status["is_excluded"] is False and "terminée" in status["message"]
    await db_session.refresh(user)
    assert user.is_active and not user.is_locked and exclusion.is_active is False


@pytest.mark.asyncio
async def test_admin_can_lift_an_exclusion(service, make_user, db_session):
    user = await make_user()
    await service.create_self_exclusion(user.id, _request(ExclusionType.PERMANENT))
    lifted = await service.lift_exclusion(user.id, admin_id="admin-1", reason="Demande écrite")
    assert not lifted.is_active and lifted.lifted_by == "admin-1" and lifted.lift_reason == "Demande écrite"
    await db_session.refresh(user)
    assert user.is_active and not user.is_locked
    with pytest.raises(NotFoundException):
        await service.lift_exclusion(user.id, admin_id="admin-1")


# ----------------------------------------------------------------------------- limites

@pytest.mark.asyncio
async def test_limits_are_created_updated_and_copied_to_the_wallet(service, make_user, db_session):
    user = await make_user()
    request = PlayerLimitRequest(daily_deposit_limit=5000, daily_loss_limit=2000, weekly_deposit_limit=20000,
                                 monthly_deposit_limit=60000, single_bet_limit=500)
    limits = await service.set_player_limits(user.id, request, set_by="agent-1")
    assert len(limits) == 5 and {l.limit_type for l in limits} == {
        "DAILY_DEPOSIT", "DAILY_LOSS", "WEEKLY_DEPOSIT", "MONTHLY_DEPOSIT", "SINGLE_BET"}
    wallet = (await db_session.execute(select(Wallet).where(Wallet.user_id == user.id))).scalar_one()
    assert (wallet.daily_deposit_limit, wallet.daily_loss_limit, wallet.single_bet_limit) == (5000, 2000, 500)

    await service.set_player_limits(user.id, PlayerLimitRequest(daily_deposit_limit=3000))
    again = {l.limit_type: l for l in await service.get_player_limits(user.id)}
    assert again["DAILY_DEPOSIT"].limit_amount == 3000 and again["DAILY_DEPOSIT"].previous_limit == 5000
    assert len(again) == 5


@pytest.mark.asyncio
async def test_remove_limit_deactivates_it(service, make_user):
    user = await make_user()
    other = await make_user()
    (limit,) = await service.set_player_limits(user.id, PlayerLimitRequest(single_bet_limit=100))
    with pytest.raises(NotFoundException):  # la limite d'un autre joueur est introuvable
        await service.remove_limit(limit.id, other.id)
    assert await service.remove_limit(limit.id, user.id) is True
    assert await service.get_player_limits(user.id) == []
    assert (await service.set_player_limits(user.id, PlayerLimitRequest()) == [])


# ----------------------------------------------------------------------------- comportement

@pytest.mark.asyncio
async def test_healthy_player_is_low_risk(service, make_user):
    report = await service.check_problematic_behavior((await make_user()).id)
    assert report["risk_level"] == "low" and report["alerts"] == [] and "sain" in report["recommendation"]


@pytest.mark.asyncio
async def test_high_losses_raise_the_risk(service, make_user, db_session):
    user = await make_user()
    await _transaction(db_session, user, TransactionType.BET, "30000", ref="B1")
    assert (await service.check_problematic_behavior(user.id))["risk_level"] == "medium"
    await _transaction(db_session, user, TransactionType.BET, "30000", ref="B2")
    report = await service.check_problematic_behavior(user.id)
    assert report["risk_level"] == "high" and report["total_losses_24h"] == 60000
    assert any(a["type"] == "high_losses_24h" for a in report["alerts"]) and "pause" in report["recommendation"]


@pytest.mark.asyncio
async def test_bet_frequency_and_chasing_losses(service, make_user, db_session):
    user = await make_user()
    for i in range(101):
        await _transaction(db_session, user, TransactionType.BET, "10", minutes_ago=300, ref=f"F{i}")
    report = await service.check_problematic_behavior(user.id)
    assert report["risk_level"] == "medium" and report["total_bets_24h"] == 101
    assert any(a["type"] == "high_bet_frequency" for a in report["alerts"])

    chaser = await make_user()
    await _transaction(db_session, chaser, TransactionType.BET, "2000", minutes_ago=30, ref="C1")
    await _transaction(db_session, chaser, TransactionType.DEPOSIT, "2000", minutes_ago=10, ref="C2")
    report = await service.check_problematic_behavior(chaser.id)
    assert report["risk_level"] == "high" and any(a["type"] == "chasing_losses" for a in report["alerts"])
