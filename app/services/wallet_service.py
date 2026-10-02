# app/services/wallet_service.py
"""Service de gestion du portefeuille et transactions"""

from datetime import datetime, date
from decimal import Decimal
from typing import Optional, List, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_
import redis.asyncio as redis

from app.core.exceptions import AppException, InsufficientBalanceException
from app.core.timezone import today_haiti, today_bounds_utc, to_haiti
from app.core.logger import get_logger
from app.models.wallet import Wallet, WalletStatus
from app.models.user import User
from app.models.transaction import Transaction, TransactionType, TransactionStatus, PaymentMethod
from app.services.base import BaseService
from app.services.audit_service import AuditService, AuditAction
from app.schemas.wallet import DepositRequest, WithdrawRequest
from app.payments.base import get_gateway

# AuditLog.action est NOT NULL : on associe toujours un type de transaction
# à une action d'audit existante (jamais None), pour ne pas faire échouer le
# flush de l'audit log (et donc toute la transaction wallet) sur une contrainte.
_DEBIT_AUDIT_ACTIONS = {"BET": AuditAction.BET_PLACED, "WITHDRAWAL": AuditAction.WITHDRAWAL}
_CREDIT_AUDIT_ACTIONS = {"DEPOSIT": AuditAction.DEPOSIT, "WIN": AuditAction.BET_SETTLED, "REFUND": AuditAction.BET_SETTLED}

# Méthodes de paiement réglées via une passerelle mobile money (cycle
# pending -> confirmé/échoué par webhook). Les autres (cash au bureau,
# virement bancaire, crypto) n'ont pas de passerelle intégrée : réglées
# immédiatement, comme avant.
_GATEWAY_PAYMENT_METHODS = ("moncash", "natcash")

# Sentinelle pour update_limits() : distingue "paramètre non fourni, ne pas
# toucher" de "explicitement mis à None, effacer la limite" — deux cas que
# `None` seul ne peut pas différencier (cf. SetLimitRequest.limit_amount,
# documenté "null pour supprimer").
_UNSET = object()


class WalletService(BaseService[Wallet, None, None]):
    """Service de gestion du portefeuille"""

    def __init__(self, db: AsyncSession, redis_client: redis.Redis):
        super().__init__(db, Wallet)
        self.redis = redis_client
        self.audit_service = AuditService(db, redis_client)
        self.logger = get_logger("WalletService")

    async def get_or_create(self, user_id: str) -> Wallet:
        """Récupère ou crée un portefeuille pour un utilisateur"""
        result = await self.db.execute(
            select(Wallet).where(Wallet.user_id == user_id)
        )
        wallet = result.scalar_one_or_none()

        if not wallet:
            wallet = Wallet(user_id=user_id, balance=Decimal("0"))
            self.db.add(wallet)
            await self.db.flush()
            self.logger.info(f"Wallet created for user: {user_id}")

        return wallet

    async def get_for_update(self, user_id: str) -> Wallet:
        """Récupère (ou crée) le portefeuille en le verrouillant jusqu'à la fin
        de la transaction DB (SELECT … FOR UPDATE).

        Empêche deux opérations concurrentes (deux paris, un pari et un
        retrait…) de lire le même solde puis de l'écraser chacune. Sans effet
        sur SQLite (tests), qui n'a pas de verrou de ligne.
        """
        result = await self.db.execute(
            select(Wallet).where(Wallet.user_id == user_id).with_for_update()
        )
        wallet = result.scalar_one_or_none()
        if not wallet:
            wallet = await self.get_or_create(user_id)
        return wallet

    async def get_by_user_id(self, user_id: str) -> Optional[Wallet]:
        """Récupère le portefeuille d'un utilisateur"""
        result = await self.db.execute(
            select(Wallet).where(Wallet.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def get_balance(self, user_id: str) -> Decimal:
        """Récupère le solde d'un utilisateur"""
        wallet = await self.get_by_user_id(user_id)
        return wallet.balance if wallet else Decimal("0")

    async def get_by_external_reference(self, external_reference: str) -> Optional[Transaction]:
        """Retrouve la transaction correspondant à la référence d'un
        fournisseur de paiement (utilisé par les webhooks/simulate)."""
        result = await self.db.execute(
            select(Transaction).where(Transaction.external_reference == external_reference)
        )
        return result.scalar_one_or_none()

    async def debit(
        self,
        user_id: str,
        amount: Decimal,
        transaction_type: str,
        reference_id: str = None,
        description: str = None,
        status: TransactionStatus = TransactionStatus.COMPLETED,
        payment_method: str = None,
        external_reference: str = None,
        reference: str = None,
        bet_id: str = None,
        draw_id: str = None,
        ticket_id: str = None,
    ) -> Transaction:
        """Débite le portefeuille d'un utilisateur.

        `bet_id`/`draw_id`/`ticket_id` (ou, pour compatibilité, `reference_id`
        comme bet_id) sont enregistrés sur la transaction pour la traçabilité.
        Pour un débit de type BET, les règles de jeu responsable sont
        vérifiées (cf. _assert_can_bet).

        `status`/`payment_method`/`external_reference`/`reference` existent
        pour les retraits réglés via une passerelle mobile money : les fonds
        sont réservés (débités) tout de suite avec status=PENDING, en
        attendant la confirmation du virement externe (cf. withdraw() /
        confirm_withdrawal() / fail_withdrawal() plus bas).
        """
        wallet = await self.get_for_update(user_id)
        self._reset_daily_counters_if_new_day(wallet)

        if transaction_type == "BET":
            await self._assert_can_bet(wallet, user_id, amount)

        if wallet.balance < amount:
            raise InsufficientBalanceException(float(amount), float(wallet.balance))

        old_balance = wallet.balance
        wallet.balance -= amount
        wallet.updated_at = datetime.utcnow()

        # Mettre à jour les compteurs journaliers
        await self._update_daily_counters(wallet, amount, is_debit=True, transaction_type=transaction_type)

        transaction = Transaction(
            user_id=user_id,
            wallet_id=wallet.id,
            reference=reference or Transaction.generate_reference("TX"),
            transaction_type=transaction_type,
            payment_method=payment_method,
            amount=amount,
            balance_before=old_balance,
            balance_after=wallet.balance,
            status=status,
            external_reference=external_reference,
            completed_at=datetime.utcnow() if status == TransactionStatus.COMPLETED else None,
            bet_id=bet_id or reference_id,
            draw_id=draw_id,
            ticket_id=ticket_id,
        )

        self.db.add(transaction)
        await self.db.flush()

        await self.audit_service.log(
            user_id=user_id,
            action=_DEBIT_AUDIT_ACTIONS.get(transaction_type, AuditAction.WITHDRAWAL),
            resource_type="wallet",
            resource_id=wallet.id,
            new_values={"balance": float(wallet.balance), "debited": float(amount), "status": status.value}
        )

        self.logger.info(f"Debited {amount} from user {user_id}. New balance: {wallet.balance}")

        return transaction

    async def credit(
        self,
        user_id: str,
        amount: Decimal,
        transaction_type: str,
        reference_id: str = None,
        description: str = None,
        status: TransactionStatus = TransactionStatus.COMPLETED,
        payment_method: str = None,
        external_reference: str = None,
        reference: str = None,
        bet_id: str = None,
        draw_id: str = None,
        ticket_id: str = None,
    ) -> Transaction:
        """Crédite le portefeuille d'un utilisateur"""
        wallet = await self.get_for_update(user_id)
        self._reset_daily_counters_if_new_day(wallet)

        old_balance = wallet.balance
        wallet.balance += amount

        if transaction_type == "WIN":
            wallet.total_won += amount

        wallet.updated_at = datetime.utcnow()

        # Mettre à jour les compteurs journaliers
        await self._update_daily_counters(wallet, amount, is_debit=False, transaction_type=transaction_type)

        transaction = Transaction(
            user_id=user_id,
            wallet_id=wallet.id,
            reference=reference or Transaction.generate_reference("TX"),
            transaction_type=transaction_type,
            payment_method=payment_method,
            amount=amount,
            balance_before=old_balance,
            balance_after=wallet.balance,
            status=status,
            external_reference=external_reference,
            completed_at=datetime.utcnow() if status == TransactionStatus.COMPLETED else None,
            bet_id=bet_id or reference_id,
            draw_id=draw_id,
            ticket_id=ticket_id,
        )

        self.db.add(transaction)
        await self.db.flush()

        await self.audit_service.log(
            user_id=user_id,
            action=_CREDIT_AUDIT_ACTIONS.get(transaction_type, AuditAction.DEPOSIT),
            resource_type="wallet",
            resource_id=wallet.id,
            new_values={"balance": float(wallet.balance), "credited": float(amount), "status": status.value}
        )

        self.logger.info(f"Credited {amount} to user {user_id}. New balance: {wallet.balance}")

        return transaction

    async def deposit(
        self,
        user_id: str,
        request: DepositRequest,
        ip_address: str = None
    ) -> Dict[str, Any]:
        """Effectue un dépôt.

        Espèces/virement bancaire : réglé immédiatement (un agent a déjà
        physiquement reçu l'argent). MonCash/NatCash : la transaction reste
        `pending`, le solde n'est crédité qu'à la confirmation du fournisseur
        (cf. confirm_deposit(), appelée par le webhook ou /simulate en dev).
        """
        wallet = await self.get_or_create(user_id)
        amount = Decimal(str(request.amount))

        if wallet.daily_deposit_limit:
            today_deposits = await self._get_today_deposits(user_id)
            if today_deposits + amount > wallet.daily_deposit_limit:
                raise AppException(400, f"Limite de dépôt journalière atteinte ({wallet.daily_deposit_limit} HTG)")

        # .value, pas str() : pour un enum `class X(str, Enum)`, str(membre)
        # rend "X.NOM" (pas la valeur) sur ces versions de Python, ce qui
        # cassait à la fois l'aiguillage passerelle et l'écriture en base.
        payment_method = request.payment_method.value

        if payment_method not in _GATEWAY_PAYMENT_METHODS:
            transaction = await self.credit(
                user_id=user_id,
                amount=amount,
                transaction_type="DEPOSIT",
                payment_method=payment_method,
            )
            wallet.total_deposited += amount
            await self.db.flush()

            return {
                "success": True,
                "transaction_id": transaction.id,
                "reference": transaction.reference,
                "amount": float(amount),
                "status": "completed",
                "new_balance": float(wallet.balance),
                "payment_url": None,
                "message": f"Dépôt de {amount} HTG effectué avec succès",
            }

        gateway = get_gateway(payment_method)
        reference = Transaction.generate_reference("DEP")
        initiation = await gateway.create_payment(amount=amount, reference=reference, phone=request.phone)

        transaction = Transaction(
            user_id=user_id,
            wallet_id=wallet.id,
            reference=reference,
            transaction_type="DEPOSIT",
            payment_method=payment_method,
            amount=amount,
            balance_before=wallet.balance,
            balance_after=wallet.balance,  # inchangé : crédité seulement à la confirmation
            status=TransactionStatus.PENDING,
            external_reference=initiation.external_reference,
            external_status=initiation.status,
            ip_address=ip_address,
        )
        self.db.add(transaction)
        await self.db.flush()

        await self.audit_service.log(
            user_id=user_id,
            action=AuditAction.DEPOSIT,
            resource_type="wallet",
            resource_id=wallet.id,
            ip_address=ip_address,
            new_values={"amount": float(amount), "status": "pending", "provider": payment_method},
        )

        self.logger.info(f"Deposit initiated: {transaction.reference} ({payment_method}, {amount} HTG, pending)")

        return {
            "success": True,
            "transaction_id": transaction.id,
            "reference": transaction.reference,
            "amount": float(amount),
            "status": "pending",
            "new_balance": float(wallet.balance),
            "payment_url": initiation.payment_url,
            "message": f"Dépôt de {amount} HTG initié, en attente de confirmation {payment_method}",
        }

    async def withdraw(
        self,
        user_id: str,
        request: WithdrawRequest,
        ip_address: str = None
    ) -> Dict[str, Any]:
        """Effectue un retrait.

        MonCash/NatCash : les fonds sont réservés (débités) immédiatement
        pour empêcher une double dépense pendant que le virement externe est
        en cours, puis remboursés si celui-ci échoue (cf. fail_withdrawal()).
        """
        wallet = await self.get_or_create(user_id)
        amount = Decimal(str(request.amount))

        if wallet.balance < amount:
            raise InsufficientBalanceException(float(amount), float(wallet.balance))

        # Vérifier KYC pour retraits importants
        user = await self.db.get(User, user_id)
        if amount >= 10000 and user.kyc_status != "verified":
            raise AppException(400, "Veuillez compléter votre vérification KYC avant de retirer")

        # .value, pas str() : pour un enum `class X(str, Enum)`, str(membre)
        # rend "X.NOM" (pas la valeur) sur ces versions de Python, ce qui
        # cassait à la fois l'aiguillage passerelle et l'écriture en base.
        payment_method = request.payment_method.value

        if payment_method not in _GATEWAY_PAYMENT_METHODS:
            transaction = await self.debit(
                user_id=user_id,
                amount=amount,
                transaction_type="WITHDRAWAL",
                payment_method=payment_method,
            )
            wallet.total_withdrawn += amount
            await self.db.flush()

            return {
                "success": True,
                "transaction_id": transaction.id,
                "reference": transaction.reference,
                "amount": float(amount),
                "status": "completed",
                "new_balance": float(wallet.balance),
                "message": f"Retrait de {amount} HTG effectué",
            }

        reference = Transaction.generate_reference("WD")
        transaction = await self.debit(
            user_id=user_id,
            amount=amount,
            transaction_type="WITHDRAWAL",
            status=TransactionStatus.PENDING,
            payment_method=payment_method,
            reference=reference,
        )

        gateway = get_gateway(payment_method)
        payout = await gateway.transfer_to_user(
            amount=amount, phone=request.phone or user.phone, reference=reference
        )
        transaction.external_reference = payout.external_reference
        transaction.external_status = payout.status
        await self.db.flush()

        self.logger.info(f"Withdrawal initiated: {transaction.reference} ({payment_method}, {amount} HTG, pending)")

        return {
            "success": True,
            "transaction_id": transaction.id,
            "reference": transaction.reference,
            "amount": float(amount),
            "status": "pending",
            "new_balance": float(wallet.balance),
            "message": f"Retrait de {amount} HTG initié, en attente de confirmation",
        }

    # ========== Confirmation / échec (webhooks passerelles) ==========

    async def confirm_deposit(self, external_reference: str) -> Transaction:
        """Le fournisseur confirme le paiement : on crédite le portefeuille.
        Idempotent (un webhook peut être livré plusieurs fois)."""
        transaction = await self.get_by_external_reference(external_reference)
        if not transaction:
            raise AppException(404, "Transaction introuvable pour cette référence")
        if transaction.status != TransactionStatus.PENDING:
            return transaction

        wallet = await self.get_for_update(transaction.user_id)
        self._reset_daily_counters_if_new_day(wallet)
        old_balance = wallet.balance
        wallet.add(transaction.amount)
        wallet.total_deposited += transaction.amount
        wallet.updated_at = datetime.utcnow()
        await self._update_daily_counters(wallet, transaction.amount, is_debit=False, transaction_type="DEPOSIT")

        transaction.balance_before = old_balance
        transaction.balance_after = wallet.balance
        transaction.external_status = "completed"
        transaction.complete()

        await self.db.flush()

        await self.audit_service.log(
            user_id=transaction.user_id,
            action=AuditAction.DEPOSIT,
            resource_type="wallet",
            resource_id=wallet.id,
            new_values={"balance": float(wallet.balance), "confirmed_deposit": float(transaction.amount)},
        )

        self.logger.info(f"Deposit confirmed: {transaction.reference} (+{transaction.amount} HTG)")
        return transaction

    async def fail_deposit(self, external_reference: str, reason: str = None) -> Transaction:
        """Le fournisseur signale un échec : rien à créditer, on marque
        juste la transaction (le solde n'a jamais bougé pour un dépôt)."""
        transaction = await self.get_by_external_reference(external_reference)
        if not transaction:
            raise AppException(404, "Transaction introuvable pour cette référence")
        if transaction.status != TransactionStatus.PENDING:
            return transaction

        transaction.external_status = "failed"
        transaction.fail(reason or "Paiement refusé par le fournisseur")
        await self.db.flush()

        await self.audit_service.log(
            user_id=transaction.user_id,
            action=AuditAction.DEPOSIT,
            resource_type="wallet",
            resource_id=transaction.wallet_id,
            reason=transaction.failure_reason,
        )

        self.logger.info(f"Deposit failed: {transaction.reference} ({transaction.failure_reason})")
        return transaction

    async def confirm_withdrawal(self, external_reference: str) -> Transaction:
        """Le virement externe a abouti : les fonds, déjà réservés à la
        demande, restent débités ; on solde juste la transaction."""
        transaction = await self.get_by_external_reference(external_reference)
        if not transaction:
            raise AppException(404, "Transaction introuvable pour cette référence")
        if transaction.status != TransactionStatus.PENDING:
            return transaction

        wallet = await self.get_by_user_id(transaction.user_id)
        wallet.total_withdrawn += transaction.amount
        wallet.updated_at = datetime.utcnow()

        transaction.external_status = "completed"
        transaction.complete()
        await self.db.flush()

        await self.audit_service.log(
            user_id=transaction.user_id,
            action=AuditAction.WITHDRAWAL,
            resource_type="wallet",
            resource_id=wallet.id,
            new_values={"confirmed_withdrawal": float(transaction.amount)},
        )

        self.logger.info(f"Withdrawal confirmed: {transaction.reference}")
        return transaction

    async def fail_withdrawal(self, external_reference: str, reason: str = None) -> Transaction:
        """Le virement externe a échoué : on rembourse le joueur, dont le
        solde avait été débité (réservé) dès la demande de retrait."""
        transaction = await self.get_by_external_reference(external_reference)
        if not transaction:
            raise AppException(404, "Transaction introuvable pour cette référence")
        if transaction.status != TransactionStatus.PENDING:
            return transaction

        wallet = await self.get_by_user_id(transaction.user_id)
        wallet.add(transaction.amount)
        wallet.updated_at = datetime.utcnow()

        transaction.external_status = "failed"
        transaction.fail(reason or "Échec du virement")
        await self.db.flush()

        await self.audit_service.log(
            user_id=transaction.user_id,
            action=AuditAction.WITHDRAWAL,
            resource_type="wallet",
            resource_id=wallet.id,
            reason=transaction.failure_reason,
            new_values={"refunded": float(transaction.amount)},
        )

        self.logger.info(f"Withdrawal failed and refunded: {transaction.reference}")
        return transaction

    async def get_transactions(
        self,
        user_id: str,
        skip: int = 0,
        limit: int = 50,
        transaction_type: str = None
    ) -> List[Transaction]:
        """Récupère l'historique des transactions"""
        query = select(Transaction).where(Transaction.user_id == user_id)

        if transaction_type:
            query = query.where(Transaction.transaction_type == transaction_type)

        query = query.order_by(Transaction.created_at.desc()).offset(skip).limit(limit)

        result = await self.db.execute(query)
        return result.scalars().all()

    async def update_limits(
        self,
        user_id: str,
        daily_deposit_limit: Optional[Decimal] = _UNSET,
        daily_loss_limit: Optional[Decimal] = _UNSET,
        weekly_deposit_limit: Optional[Decimal] = _UNSET,
        monthly_deposit_limit: Optional[Decimal] = _UNSET,
        single_bet_limit: Optional[Decimal] = _UNSET,
    ) -> Wallet:
        """Met à jour les limites du joueur.

        Un paramètre omis (valeur par défaut `_UNSET`) n'est pas touché ;
        passé explicitement à `None`, il efface la limite existante. Un
        appelant qui veut effacer UNE limite précise doit donc ne passer
        QUE ce paramètre-là (cf. app/api/v1/wallet.py:set_limit, qui ne
        transmet jamais qu'un seul kwarg à la fois).
        """
        wallet = await self.get_or_create(user_id)

        if daily_deposit_limit is not _UNSET:
            wallet.daily_deposit_limit = daily_deposit_limit
        if daily_loss_limit is not _UNSET:
            wallet.daily_loss_limit = daily_loss_limit
        if weekly_deposit_limit is not _UNSET:
            wallet.weekly_deposit_limit = weekly_deposit_limit
        if monthly_deposit_limit is not _UNSET:
            wallet.monthly_deposit_limit = monthly_deposit_limit
        if single_bet_limit is not _UNSET:
            wallet.single_bet_limit = single_bet_limit

        wallet.updated_at = datetime.utcnow()
        await self.db.flush()

        self.logger.info(f"Limits updated for user {user_id}")

        return wallet

    def _reset_daily_counters_if_new_day(self, wallet: Wallet) -> None:
        """Remet à zéro les compteurs journaliers au changement de jour
        (jour civil d'Haïti)."""
        today = today_haiti()
        last = to_haiti(wallet.last_reset_date).date() if wallet.last_reset_date else None
        if last != today:
            wallet.today_deposits = Decimal("0")
            wallet.today_losses = Decimal("0")
            wallet.today_bets = Decimal("0")
            wallet.last_reset_date = datetime.utcnow()

    async def _update_daily_counters(
        self,
        wallet: Wallet,
        amount: Decimal,
        is_debit: bool,
        transaction_type: str = None,
    ) -> None:
        """Met à jour les compteurs journaliers.

        - BET (débit)    : mises du jour + perte nette du jour
        - WIN (crédit)   : réduit la perte nette du jour
        - REFUND (crédit): annule la mise remboursée
        - DEPOSIT        : dépôts du jour
        Un retrait n'est ni une mise ni une perte.
        `today_losses` est une perte NETTE (mises - gains) et peut être négative.
        """
        self._reset_daily_counters_if_new_day(wallet)
        today_bets = wallet.today_bets or Decimal("0")
        today_losses = wallet.today_losses or Decimal("0")
        today_deposits = wallet.today_deposits or Decimal("0")

        if transaction_type == "BET" and is_debit:
            wallet.today_bets = today_bets + amount
            wallet.today_losses = today_losses + amount
        elif transaction_type == "WIN" and not is_debit:
            wallet.today_losses = today_losses - amount
        elif transaction_type == "REFUND" and not is_debit:
            wallet.today_bets = today_bets - amount
            wallet.today_losses = today_losses - amount
        elif transaction_type == "DEPOSIT" and not is_debit:
            wallet.today_deposits = today_deposits + amount

    async def _assert_can_bet(self, wallet: Wallet, user_id: str, amount: Decimal) -> None:
        """Règles de jeu responsable vérifiées avant chaque mise.

        Lève AppException (403) si : compte inactif/verrouillé, auto-exclusion
        active, portefeuille gelé, limite par pari ou limite de perte
        journalière dépassée. Le solde est vérifié séparément (400).
        """
        user = await self.db.get(User, user_id)
        if user is None or not user.is_active or user.is_locked:
            raise AppException(403, "Compte inactif ou verrouillé : mise impossible", "ACCOUNT_LOCKED")

        from app.services.responsible_service import ResponsibleService  # import local : évite un cycle
        exclusion = await ResponsibleService(self.db, self.redis).get_active_exclusion(user_id)
        if exclusion is not None:
            raise AppException(403, "Auto-exclusion active : les paris sont bloqués", "SELF_EXCLUDED")

        if wallet.status != WalletStatus.ACTIVE:
            raise AppException(403, "Portefeuille gelé : mise impossible", "WALLET_FROZEN")

        if wallet.single_bet_limit and amount > wallet.single_bet_limit:
            raise AppException(
                403, f"Mise supérieure à votre limite par pari ({wallet.single_bet_limit} HTG)", "BET_LIMIT"
            )

        if wallet.daily_loss_limit and (wallet.today_losses or Decimal("0")) + amount > wallet.daily_loss_limit:
            raise AppException(
                403, f"Limite de perte journalière atteinte ({wallet.daily_loss_limit} HTG)", "LOSS_LIMIT"
            )

    # ========== Raccourcis jeux ==========

    async def debit_for_bet(
        self,
        user_id: str,
        amount: Decimal,
        bet_id: str = None,
        draw_id: str = None,
        ticket_id: str = None,
        reference: str = None,
    ) -> Transaction:
        """Débite une mise (type BET) en la rattachant au pari / tirage."""
        return await self.debit(
            user_id=user_id,
            amount=Decimal(str(amount)),
            transaction_type="BET",
            bet_id=bet_id,
            draw_id=draw_id,
            ticket_id=ticket_id,
            reference=reference,
        )

    async def credit_for_win(
        self,
        user_id: str,
        amount: Decimal,
        bet_id: str = None,
        draw_id: str = None,
        ticket_id: str = None,
        reference: str = None,
    ) -> Transaction:
        """Crédite un gain (type WIN). Passer une `reference` déterministe
        (ex. WIN-<jeu>-<bet_id>) rend le paiement idempotent : la contrainte
        d'unicité sur transactions.reference refuse un second paiement."""
        return await self.credit(
            user_id=user_id,
            amount=Decimal(str(amount)),
            transaction_type="WIN",
            bet_id=bet_id,
            draw_id=draw_id,
            ticket_id=ticket_id,
            reference=reference,
        )

    async def credit_refund(
        self,
        user_id: str,
        amount: Decimal,
        bet_id: str = None,
        draw_id: str = None,
        ticket_id: str = None,
        reference: str = None,
    ) -> Transaction:
        """Rembourse une mise (type REFUND), ex. manche annulée."""
        return await self.credit(
            user_id=user_id,
            amount=Decimal(str(amount)),
            transaction_type="REFUND",
            bet_id=bet_id,
            draw_id=draw_id,
            ticket_id=ticket_id,
            reference=reference,
        )

    async def _get_today_deposits(self, user_id: str) -> Decimal:
        """Récupère le total des dépôts du jour"""
        today_start, _ = today_bounds_utc()
        result = await self.db.execute(
            select(func.sum(Transaction.amount))
            .where(
                and_(
                    Transaction.user_id == user_id,
                    Transaction.transaction_type == "DEPOSIT",
                    Transaction.created_at >= today_start,
                    Transaction.status == TransactionStatus.COMPLETED
                )
            )
        )
        return result.scalar() or Decimal("0")
