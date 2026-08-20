# tests/test_services/test_auth_service.py
"""Tests critiques d'authentification : inscription, connexion, verrouillage,
jetons JWT et blacklist au logout."""

from datetime import timedelta

import pytest

from app.core.exceptions import AppException, UnauthorizedException
from app.core.security import create_access_token, decode_token, hash_password, verify_password
from app.schemas.user import UserCreate, UserLogin
from app.services.auth_service import AuthService
from app.services.user_service import UserService


# ========== Hachage de mot de passe / JWT (logique pure, sans DB) ==========

def test_hash_password_roundtrip():
    hashed = hash_password("Secret123!")
    assert hashed != "Secret123!"
    assert verify_password("Secret123!", hashed) is True


def test_verify_password_rejects_wrong_password():
    hashed = hash_password("Secret123!")
    assert verify_password("WrongPassword", hashed) is False


def test_access_token_roundtrip():
    token = create_access_token({"sub": "user-123", "role": "player"})
    payload = decode_token(token)
    assert payload is not None
    assert payload["sub"] == "user-123"
    assert payload["type"] == "access"


def test_expired_token_is_rejected():
    token = create_access_token({"sub": "user-123"}, expires_delta=timedelta(seconds=-1))
    assert decode_token(token) is None


def test_tampered_token_is_rejected():
    token = create_access_token({"sub": "user-123"})
    assert decode_token(token + "tampered") is None


# ========== Inscription ==========

@pytest.mark.asyncio
async def test_register_creates_user_and_wallet(db_session, fake_redis):
    auth_service = AuthService(db_session, fake_redis)

    user = await auth_service.register(
        UserCreate(phone="40001111", password="Secret123!"), ip_address="127.0.0.1"
    )

    assert user.id is not None
    assert user.phone == "40001111"
    assert user.password_hash != "Secret123!"

    # Requête explicite plutôt que l'accès paresseux user.wallet : sous
    # AsyncSession, une relation lazy-load accédée hors requête explicite
    # plante avec MissingGreenlet (cf. app/api/v1/agent.py:430 qui a le même
    # défaut avec `user.wallet.balance`).
    from app.services.wallet_service import WalletService

    wallet = await WalletService(db_session, fake_redis).get_by_user_id(user.id)
    assert wallet is not None
    assert wallet.balance == 0


@pytest.mark.asyncio
async def test_register_rejects_duplicate_phone(db_session, fake_redis):
    auth_service = AuthService(db_session, fake_redis)
    data = UserCreate(phone="40002222", password="Secret123!")

    await auth_service.register(data, ip_address="127.0.0.1")

    with pytest.raises(AppException):
        await auth_service.register(data, ip_address="127.0.0.1")


# ========== Connexion ==========

@pytest.mark.asyncio
async def test_login_success_returns_tokens(db_session, fake_redis, make_user):
    user = await make_user(password="Secret123!")
    auth_service = AuthService(db_session, fake_redis)

    logged_in_user, tokens = await auth_service.login(
        UserLogin(phone=user.phone, password="Secret123!"),
        ip_address="127.0.0.1",
        user_agent="pytest",
    )

    assert logged_in_user.id == user.id
    assert tokens.access_token
    assert tokens.refresh_token
    payload = decode_token(tokens.access_token)
    assert payload["sub"] == user.id


@pytest.mark.asyncio
async def test_login_rejects_unknown_phone(db_session, fake_redis):
    auth_service = AuthService(db_session, fake_redis)

    with pytest.raises(UnauthorizedException):
        await auth_service.login(
            UserLogin(phone="40009999", password="whatever"),
            ip_address="127.0.0.1",
            user_agent="pytest",
        )


@pytest.mark.asyncio
async def test_login_rejects_wrong_password(db_session, fake_redis, make_user):
    user = await make_user(password="Secret123!")
    auth_service = AuthService(db_session, fake_redis)

    with pytest.raises(UnauthorizedException):
        await auth_service.login(
            UserLogin(phone=user.phone, password="WrongPassword"),
            ip_address="127.0.0.1",
            user_agent="pytest",
        )


@pytest.mark.asyncio
async def test_login_locks_account_after_max_failed_attempts(db_session, fake_redis, make_user):
    """UserService.MAX_LOGIN_ATTEMPTS = 5 : après 5 échecs, le compte est
    verrouillé, y compris pour une tentative ultérieure avec le bon mot de passe."""
    user = await make_user(password="Secret123!")
    auth_service = AuthService(db_session, fake_redis)

    for _ in range(5):
        with pytest.raises(UnauthorizedException):
            await auth_service.login(
                UserLogin(phone=user.phone, password="WrongPassword"),
                ip_address="127.0.0.1",
                user_agent="pytest",
            )

    with pytest.raises(UnauthorizedException, match="bloqué"):
        await auth_service.login(
            UserLogin(phone=user.phone, password="Secret123!"),
            ip_address="127.0.0.1",
            user_agent="pytest",
        )


@pytest.mark.asyncio
async def test_login_resets_failed_attempts_on_success(db_session, fake_redis, make_user):
    user = await make_user(password="Secret123!")
    auth_service = AuthService(db_session, fake_redis)

    for _ in range(3):
        with pytest.raises(UnauthorizedException):
            await auth_service.login(
                UserLogin(phone=user.phone, password="WrongPassword"),
                ip_address="127.0.0.1",
                user_agent="pytest",
            )

    # Connexion correcte : ne doit pas être bloquée malgré les 3 échecs précédents.
    logged_in_user, _ = await auth_service.login(
        UserLogin(phone=user.phone, password="Secret123!"),
        ip_address="127.0.0.1",
        user_agent="pytest",
    )
    assert logged_in_user.is_locked is False


@pytest.mark.asyncio
async def test_login_rejects_locked_account_even_with_correct_password(
    db_session, fake_redis, make_user
):
    user = await make_user(password="Secret123!")
    user_service = UserService(db_session, fake_redis)
    await user_service.block_user(user.id, reason="fraude suspectée", admin_id="admin-1")

    auth_service = AuthService(db_session, fake_redis)
    with pytest.raises(UnauthorizedException):
        await auth_service.login(
            UserLogin(phone=user.phone, password="Secret123!"),
            ip_address="127.0.0.1",
            user_agent="pytest",
        )


# ========== Déconnexion / blacklist ==========

@pytest.mark.asyncio
async def test_logout_blacklists_token(db_session, fake_redis, make_user):
    user = await make_user(password="Secret123!")
    auth_service = AuthService(db_session, fake_redis)

    _, tokens = await auth_service.login(
        UserLogin(phone=user.phone, password="Secret123!"),
        ip_address="127.0.0.1",
        user_agent="pytest",
    )

    assert await auth_service.verify_token(tokens.access_token) is not None

    await auth_service.logout(user.id, tokens.access_token)

    assert await auth_service.verify_token(tokens.access_token) is None


# ========== Changement de mot de passe ==========

@pytest.mark.asyncio
async def test_change_password_rejects_wrong_current_password(db_session, fake_redis, make_user):
    user = await make_user(password="Secret123!")
    auth_service = AuthService(db_session, fake_redis)

    with pytest.raises(AppException):
        await auth_service.change_password(
            user.id, current_password="WrongPassword", new_password="NewSecret456!", ip_address="127.0.0.1"
        )


@pytest.mark.asyncio
async def test_change_password_success_updates_hash_and_allows_new_login(
    db_session, fake_redis, make_user
):
    user = await make_user(password="Secret123!")
    auth_service = AuthService(db_session, fake_redis)

    await auth_service.change_password(
        user.id, current_password="Secret123!", new_password="NewSecret456!", ip_address="127.0.0.1"
    )

    with pytest.raises(UnauthorizedException):
        await auth_service.login(
            UserLogin(phone=user.phone, password="Secret123!"),
            ip_address="127.0.0.1",
            user_agent="pytest",
        )

    logged_in_user, _ = await auth_service.login(
        UserLogin(phone=user.phone, password="NewSecret456!"),
        ip_address="127.0.0.1",
        user_agent="pytest",
    )
    assert logged_in_user.id == user.id
