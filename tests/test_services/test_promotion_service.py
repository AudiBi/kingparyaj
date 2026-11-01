# tests/test_services/test_promotion_service.py
"""Promotions et bonus : cycle de vie, éligibilité, calcul du bonus, mise requise (wagering)."""

import json
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.exceptions import AppException, NotFoundException
from app.models.enums import TransactionStatus, TransactionType
from app.models.promotion import PromotionStatus, UserPromotion
from app.models.transaction import Transaction
from app.models.wallet import Wallet
from app.schemas.promotion import ClaimPromotionRequest, PromotionCreate
from app.services.promotion_service import PromotionService


@pytest.fixture
def service(db_session, fake_redis):
    svc = PromotionService(db_session, fake_redis)
    svc.notification_service.create_notification = AsyncMock()  # table des notifications absente des tests
    return svc


def _data(**changes):
    base = dict(
        name="Bienvenue", code="BIENVENUE", description="Bonus", type="DEPOSIT_BONUS",
        config={"bonus_percent": 50, "min_deposit": 100},
        start_date=datetime.utcnow() - timedelta(days=1), end_date=datetime.utcnow() + timedelta(days=30),
        wagering_requirement=2,
    )
    base.update(changes)
    return PromotionCreate(**base)


async def _promo(service, activate=True, **changes):
    promotion = await service.create_promotion(_data(**changes), created_by="admin-1")
    if activate:
        await service.activate_promotion(promotion.id, "admin-1")
    return promotion


async def _tx(db_session, user, amount, kind=TransactionType.DEPOSIT, ref="DEP-1", status=TransactionStatus.COMPLETED, minutes_ago=0):
    wallet = (await db_session.execute(select(Wallet).where(Wallet.user_id == user.id))).scalar_one()
    db_session.add(Transaction(
        user_id=user.id, wallet_id=wallet.id, reference=ref, transaction_type=kind, amount=Decimal(amount),
        balance_before=Decimal(0), balance_after=Decimal(amount), status=status,
        created_at=datetime.utcnow() - timedelta(minutes=minutes_ago),
    ))
    await db_session.flush()


# ----------------------------------------------------------------------------- cycle de vie

@pytest.mark.asyncio
async def test_create_activate_and_deactivate(service):
    promotion = await service.create_promotion(_data(), created_by="admin-1")
    assert promotion.status == PromotionStatus.DRAFT
    with pytest.raises(AppException, match="déjà utilisé"):
        await service.create_promotion(_data(), created_by="admin-1")

    await service.activate_promotion(promotion.id, "admin-1")
    assert promotion.status == PromotionStatus.ACTIVE
    with pytest.raises(AppException, match="Impossible d'activer"):
        await service.activate_promotion(promotion.id, "admin-1")
    await service.deactivate_promotion(promotion.id, "admin-1")
    assert promotion.status == PromotionStatus.PAUSED
    with pytest.raises(NotFoundException):
        await service.activate_promotion("inconnu", "admin-1")


@pytest.mark.asyncio
async def test_promotion_type_is_validated_and_case_insensitive(service):
    assert (await _promo(service, code="A", type="cashback")).type.value == "cashback"
    assert (await _promo(service, code="B", type="FREE_BET")).type.value == "free_bet"
    with pytest.raises(AppException, match="Type de promotion invalide"):
        await service.create_promotion(_data(code="C", type="loterie"), created_by="admin-1")


@pytest.mark.asyncio
async def test_active_promotions_use_dates_and_cache(service, fake_redis):
    current = await _promo(service)
    await _promo(service, code="EXPIREE", end_date=datetime.utcnow() - timedelta(days=1))
    await _promo(service, code="BROUILLON", activate=False)
    assert [p.id for p in await service.get_active_promotions()] == [current.id]
    assert json.loads(await fake_redis.get("promotions:active")) == [current.id]  # mise en cache (JSON)
    assert [p.id for p in await service.get_active_promotions()] == [current.id]  # lue depuis le cache


# ----------------------------------------------------------------------------- éligibilité

@pytest.mark.asyncio
async def test_eligibility_rules(service, make_user, db_session):
    user = await make_user()
    future = await _promo(service, code="FUTUR", start_date=datetime.utcnow() + timedelta(days=1))
    assert (await service._check_eligibility(user.id, future)) == (False, "Cette promotion n'a pas encore commencé")
    old = await _promo(service, code="VIEUX", end_date=datetime.utcnow() - timedelta(seconds=1))
    assert "expirée" in (await service._check_eligibility(user.id, old))[1]

    broke = await _promo(service, code="BUDGET", total_budget=100)
    broke.used_budget = Decimal("100")
    assert "Budget" in (await service._check_eligibility(user.id, broke))[1]

    new_only = await _promo(service, code="NEUF", new_users_only=True)
    assert (await service._check_eligibility(user.id, new_only))[0] is True
    user.total_bets_count = 3
    assert "nouveaux joueurs" in (await service._check_eligibility(user.id, new_only))[1]

    first = await _promo(service, code="PREMIER", first_deposit_only=True)
    assert (await service._check_eligibility(user.id, first))[0] is True
    await _tx(db_session, user, "500")
    assert "premier dépôt" in (await service._check_eligibility(user.id, first))[1]

    ok = await _promo(service, code="LIBRE")
    assert await service._check_eligibility(user.id, ok) == (True, "")
    db_session.add(UserPromotion(user_id=user.id, promotion_id=ok.id, bonus_amount=Decimal(10), wagering_required=20,
                                 expires_at=datetime.utcnow() + timedelta(days=1)))
    await db_session.flush()
    assert "déjà réclamé" in (await service._check_eligibility(user.id, ok))[1]


# ----------------------------------------------------------------------------- calcul

@pytest.mark.asyncio
async def test_deposit_bonus_amount(service, make_user, db_session):
    user = await make_user()
    promotion = await _promo(service, max_bonus=100)
    assert await service._calculate_bonus(user.id, promotion) == 0  # aucun dépôt
    await _tx(db_session, user, "50", ref="D-small", minutes_ago=10)
    assert await service._calculate_bonus(user.id, promotion) == 0  # sous le dépôt minimum
    await _tx(db_session, user, "120", ref="D-ok")
    assert await service._calculate_bonus(user.id, promotion) == Decimal("60")  # 50 % de 120
    promotion.config = {**promotion.config, "max_bonus": 40}
    assert await service._calculate_bonus(user.id, promotion) == Decimal("40")  # plafonné


@pytest.mark.asyncio
async def test_cashback_and_fixed_bonus(service, make_user, db_session):
    user = await make_user()
    cashback = await _promo(service, code="CASH", type="CASHBACK", max_bonus=500,
                            config={"percentage": 10, "period_days": 7, "max_cashback": 30})
    assert await service._calculate_bonus(user.id, cashback) == 0  # pas de perte
    await _tx(db_session, user, "1000", kind=TransactionType.BET, ref="B1")
    await _tx(db_session, user, "200", kind=TransactionType.WIN, ref="W1")
    assert await service._calculate_bonus(user.id, cashback) == Decimal("30")  # 10 % de 800 = 80, plafonné à 30
    await _tx(db_session, user, "5000", kind=TransactionType.WIN, ref="W2")
    assert await service._calculate_bonus(user.id, cashback) == 0  # gagnant : rien à rembourser

    fixed = await _promo(service, code="FIXE", type="FREE_BET", config={"bonus_amount": 25})
    assert await service._calculate_bonus(user.id, fixed) == Decimal("25")


# ----------------------------------------------------------------------------- réclamation et suivi

@pytest.mark.asyncio
async def test_claim_bonus_credits_wallet_and_tracks_budget(service, make_user, db_session, fake_redis):
    from app.services.wallet_service import WalletService

    user = await make_user(balance=Decimal("0"))
    promotion = await _promo(service, code="FIXE", type="FREE_BET", config={"bonus_amount": 25}, total_budget=100)
    result = await service.claim_bonus(user.id, ClaimPromotionRequest(promotion_code="FIXE"), "1.2.3.4")
    assert result["success"] and result["bonus_amount"] == 25.0 and result["wagering_required"] == 50.0
    assert await WalletService(db_session, fake_redis).get_balance(user.id) == Decimal("25")
    assert (promotion.total_claims, promotion.total_bonus_given, promotion.used_budget) == (1, Decimal("25"), Decimal("25"))
    with pytest.raises(AppException, match="déjà réclamé"):
        await service.claim_bonus(user.id, ClaimPromotionRequest(promotion_code="FIXE"))


@pytest.mark.asyncio
async def test_claim_bonus_errors(service, make_user):
    user = await make_user()
    with pytest.raises(AppException, match="Aucune promotion active"):
        await service.claim_bonus(user.id, ClaimPromotionRequest())
    with pytest.raises(NotFoundException):
        await service.claim_bonus(user.id, ClaimPromotionRequest(promotion_code="INCONNU"))
    await _promo(service, code="ZERO")  # bonus de dépôt sans dépôt : bonus nul
    with pytest.raises(AppException, match="Bonus nul"):
        await service.claim_bonus(user.id, ClaimPromotionRequest(promotion_code="ZERO"))


@pytest.mark.asyncio
async def test_wagering_unlocks_the_bonus(service, make_user, db_session):
    user = await make_user()
    promotion = await _promo(service, code="WG")
    bonus = UserPromotion(user_id=user.id, promotion_id=promotion.id, bonus_amount=Decimal(10), wagering_required=100,
                          wagered_amount=Decimal(0), expires_at=datetime.utcnow() + timedelta(days=5))
    db_session.add(bonus)
    await db_session.flush()

    await service.update_wagering(user.id, Decimal("30"), "roulette")  # jeu non éligible
    assert bonus.wagered_amount == 0
    await service.update_wagering(user.id, Decimal("30"), "keno")
    assert bonus.wagered_amount == 30 and not bonus.is_completed
    await service.update_wagering(user.id, Decimal("80"), "lucky6")
    assert bonus.is_completed and bonus.completed_at is not None
    service.notification_service.create_notification.assert_awaited()


@pytest.mark.asyncio
async def test_user_bonuses_are_grouped(service, make_user, db_session):
    user = await make_user()
    promotion = await _promo(service, code="GR")
    now = datetime.utcnow()
    for expires, done, expired, wagered in (
        (now + timedelta(days=5), False, False, 20),   # actif
        (now + timedelta(days=5), True, False, 100),   # terminé
        (now - timedelta(days=1), False, False, 0),    # échu
        (now + timedelta(days=5), False, True, 0),     # marqué expiré
    ):
        db_session.add(UserPromotion(user_id=user.id, promotion_id=promotion.id, bonus_amount=Decimal(10),
                                     wagered_amount=Decimal(wagered), wagering_required=100, is_completed=done,
                                     is_expired=expired, expires_at=expires))
    await db_session.flush()
    report = await service.get_user_bonuses(user.id)
    assert (len(report["active"]), len(report["completed"]), len(report["expired"])) == (1, 1, 2)
    assert report["active"][0]["progress"] == 20.0 and report["total_active_bonus"] == 10.0
