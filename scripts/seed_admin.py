from sqlalchemy.orm import Session
from app.models.user import User
from app.models.enums import UserRole, KYCStatus
from app.core.security import get_password_hash


def seed_admin(bind):
    db = Session(bind=bind)

    exists = db.query(User).filter(
        User.email == "admin@example.com"
    ).first()

    if not exists:
        db.add(
            User(
                email="admin@example.com",
                phone="+50912345678",
                first_name="System",
                last_name="Administrator",
                password_hash=get_password_hash("ChangeMe123!"),
                role=UserRole.SUPER_ADMIN,
                kyc_status=KYCStatus.APPROVED,
                is_active=True,
                is_locked=False,
                referral_code="ADMIN001",
            )
        )
        db.commit()