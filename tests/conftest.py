# tests/conftest.py
"""Fixtures partagées pour les tests critiques (auth, wallet, règlement des paris).

La suite utilise une base SQLite en mémoire (au lieu de Postgres) et un faux
client Redis en mémoire, pour pouvoir tourner sans infrastructure externe.
Seules les tables User/Wallet/Transaction/AuditLog sont créées : les modèles
Keno (KenoDraw/KenoBet) utilisent des colonnes ARRAY spécifiques à Postgres et
ne peuvent pas être créées sur SQLite. Les tests de règlement Keno construisent
donc des objets KenoDraw/KenoBet en mémoire (non persistés) et redirigent les
requêtes de lecture du service vers ces objets, cf. test_keno_service.py.
"""

from decimal import Decimal
from typing import AsyncGenerator, Callable

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.csrf import AdminCsrfMiddleware
from app.core.database import Base, get_db
from app.core.redis_client import get_redis
from app.models.audit import AuditLog
from app.models.bureau import Bureau, CashierSession
from app.models.enums import KYCStatus, UserRole
from app.models.lucky import LuckyPlay, LuckyWheelConfig
from app.models.promotion import Promotion, UserPromotion
from app.models.ticket import Ticket
from app.models.transaction import Transaction
from app.models.user import User
from app.models.wallet import Wallet
from app.routes.admin import router as admin_router
from app.routes.agent import router as agent_router
from app.schemas.user import UserCreate
from app.services.ticket_service import TicketService
from app.services.user_service import UserService

ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "AdminPass123!"

# Seules les tables sans colonnes ARRAY (Postgres-only) sont créées sur SQLite
# (KenoDraw/KenoBet en ont ; leurs requêtes sont interceptées à la main dans
# les tests concernés plutôt que d'ajouter leurs tables ici). LuckyWheelConfig/
# LuckyPlay n'utilisent que JSON, portable, donc créables sans souci.
TEST_TABLES = [
    User.__table__,
    Wallet.__table__,
    Transaction.__table__,
    AuditLog.__table__,
    Bureau.__table__,
    CashierSession.__table__,
    Ticket.__table__,
    LuckyWheelConfig.__table__,
    LuckyPlay.__table__,
    Promotion.__table__,
    UserPromotion.__table__,
]


class FakeRedis:
    """Substitut minimal en mémoire de redis.asyncio.Redis.

    N'implémente que les commandes réellement utilisées par AuthService /
    UserService / les routes admin (get, setex, delete, exists, incr, expire,
    keys) : suffisant pour tester la blacklist de tokens, le verrouillage
    après échecs de connexion et les sessions actives admin, sans dépendre
    d'un vrai serveur Redis.
    """

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def get(self, key: str):
        return self._store.get(key)

    async def set(self, key: str, value, *args, **kwargs) -> bool:
        self._store[key] = value
        return True

    async def setex(self, key: str, ttl: int, value) -> bool:
        self._store[key] = value
        return True

    async def delete(self, *keys: str) -> int:
        count = 0
        for key in keys:
            if key in self._store:
                del self._store[key]
                count += 1
        return count

    async def exists(self, key: str) -> int:
        return 1 if key in self._store else 0

    async def incr(self, key: str) -> int:
        value = int(self._store.get(key, 0)) + 1
        self._store[key] = str(value)
        return value

    async def expire(self, key: str, ttl: int) -> bool:
        return True

    async def keys(self, pattern: str = "*") -> list[str]:
        import fnmatch

        return [key for key in self._store if fnmatch.fnmatch(key, pattern)]


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """Session SQLite async isolée, recréée pour chaque test."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=TEST_TABLES)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session

    await engine.dispose()


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


@pytest_asyncio.fixture
async def make_user(db_session: AsyncSession, fake_redis: FakeRedis) -> Callable:
    """Factory créant un joueur (+ son wallet) avec un numéro unique par appel."""
    user_service = UserService(db_session, fake_redis)
    counter = {"n": 0}

    async def _make_user(
        password: str = "Secret123!",
        kyc_verified: bool = False,
        balance: Decimal | None = None,
    ) -> User:
        counter["n"] += 1
        phone = f"4000{counter['n']:04d}"  # 8 chiffres, format accepté par UserBase.validate_phone
        user = await user_service.create(UserCreate(phone=phone, password=password))

        if kyc_verified:
            user.kyc_status = KYCStatus.VERIFIED

        if balance is not None:
            from sqlalchemy import select

            result = await db_session.execute(select(Wallet).where(Wallet.user_id == user.id))
            wallet = result.scalar_one()
            wallet.balance = balance

        await db_session.flush()
        return user

    return _make_user


@pytest.fixture
def admin_app(db_session: AsyncSession, fake_redis: FakeRedis) -> FastAPI:
    """App FastAPI minimale montant le routeur admin seul (sans le lifespan
    de app.main, qui exige un vrai Postgres/Redis), utilisée par les tests
    de app/routes/admin.py (login, CSRF, dashboard, ...)."""
    app = FastAPI()
    app.include_router(admin_router)
    app.add_middleware(AdminCsrfMiddleware)

    # Route factice pour vérifier que la protection CSRF s'applique à
    # n'importe quelle route mutante sous /admin, pas route par route
    # (AdminCsrfMiddleware s'applique par préfixe d'URL).
    @app.post("/admin/_test-protected")
    async def _protected():
        return {"ok": True}

    async def _get_db():
        yield db_session

    async def _get_redis():
        return fake_redis

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_redis] = _get_redis
    return app


@pytest_asyncio.fixture
async def admin_client(admin_app: FastAPI) -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=admin_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest_asyncio.fixture
async def admin_user(db_session: AsyncSession, fake_redis: FakeRedis) -> User:
    """Un administrateur (email/mot de passe = ADMIN_EMAIL/ADMIN_PASSWORD)
    pour les tests du panel /admin."""
    user_service = UserService(db_session, fake_redis)
    user = await user_service.create(
        UserCreate(phone="40009999", password=ADMIN_PASSWORD, email=ADMIN_EMAIL)
    )
    user.role = UserRole.ADMIN
    await db_session.flush()
    return user


@pytest.fixture
def agent_app(db_session: AsyncSession, fake_redis: FakeRedis) -> FastAPI:
    """App FastAPI minimale montant le routeur agent seul (même principe que
    admin_app), utilisée par les tests de app/routes/agent.py."""
    app = FastAPI()
    app.include_router(agent_router)
    app.add_middleware(AdminCsrfMiddleware)

    async def _get_db():
        yield db_session

    async def _get_redis():
        return fake_redis

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_redis] = _get_redis
    return app


@pytest_asyncio.fixture
async def agent_client(agent_app: FastAPI) -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=agent_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest_asyncio.fixture
async def make_bureau(db_session: AsyncSession) -> Callable:
    """Factory créant un bureau (point de vente physique) avec un code unique."""
    counter = {"n": 0}

    async def _make_bureau(cash_balance: Decimal = Decimal("0")) -> Bureau:
        counter["n"] += 1
        bureau = Bureau(
            name=f"Bureau Test {counter['n']}",
            code=f"BUR{counter['n']:04d}",
            cash_balance=cash_balance,
        )
        db_session.add(bureau)
        await db_session.flush()
        return bureau

    return _make_bureau


@pytest_asyncio.fixture
async def make_agent(db_session: AsyncSession, fake_redis: FakeRedis, make_bureau: Callable) -> Callable:
    """Factory créant un agent (rôle AGENT, affecté à un bureau)."""
    user_service = UserService(db_session, fake_redis)
    counter = {"n": 0}

    async def _make_agent(bureau: Bureau = None, password: str = "AgentPass123!") -> User:
        counter["n"] += 1
        if bureau is None:
            bureau = await make_bureau()
        phone = f"3000{counter['n']:04d}"
        agent = await user_service.create(UserCreate(phone=phone, password=password))
        agent.role = UserRole.AGENT
        agent.bureau_id = bureau.id
        await db_session.flush()
        return agent

    return _make_agent


@pytest_asyncio.fixture
async def make_ticket(db_session: AsyncSession, fake_redis: FakeRedis) -> Callable:
    """Factory créant un ticket actif (joueur sans compte, jeu cash au
    bureau) via TicketService, pour les tests du parcours ticket."""
    ticket_service = TicketService(db_session, fake_redis)

    async def _make_ticket(agent: User, balance: Decimal = Decimal("100")) -> dict:
        return await ticket_service.create_ticket(
            agent_id=agent.id,
            bureau_id=agent.bureau_id,
            amount=balance,
        )

    return _make_ticket
