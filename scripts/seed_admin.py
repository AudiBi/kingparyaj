import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.models.enums import UserRole, KYCStatus
from app.core.security import hash_password
from app.core.database import engine


async def seed_admin():
    async with AsyncSession(engine) as db:
        result = await db.execute(
            select(User).where(
                User.email == "admin@example.com"
            )
        )

        exists = result.scalar_one_or_none()

        if exists:
            print("Admin existe déjà.")
            return

        admin = User(
            email="admin@example.com",
            phone="+50912345678",
            first_name="System",
            last_name="Administrator",
            password_hash=hash_password("Admin123!"),
            role=UserRole.SUPER_ADMIN,
            kyc_status=KYCStatus.VERIFIED,
            is_active=True,
            is_locked=False,
            referral_code="ADMIN001",
        )
        db.add(admin)
        await db.commit()

        print("Admin créé avec succès.")


if __name__ == "__main__":
    asyncio.run(seed_admin())