#!/usr/bin/env python
import asyncio
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.database import AsyncSessionLocal
from app.core.security import hash_password
from app.models.user import User, UserRole
from app.models.bureau import Bureau
from app.models.lucky import LuckyWheelConfig
from app.models.keno import KenoDraw
from app.models.promotion import Promotion, PromotionType, PromotionStatus
from datetime import datetime, timedelta


async def seed():
    async with AsyncSessionLocal() as db:
        print("🌱 Seeding database...")
        
        # 1. Créer le bureau principal
        bureau = Bureau(
            name="Bureau Principal",
            code="PRINCIPAL",
            city="Port-au-Prince",
            address="123 Rue Capois, Port-au-Prince",
            phone="+509 1234 5678",
            is_active=True
        )
        db.add(bureau)
        await db.flush()
        print(f"✅ Bureau créé: {bureau.name}")
        
        # 2. Créer l'admin
        admin = User(
            phone="34123456",
            email="admin@parierkeno.ht",
            first_name="Admin",
            last_name="System",
            password_hash=hash_password("Admin123!"),
            role=UserRole.SUPER_ADMIN,
            is_active=True,
            kyc_status="verified"
        )
        db.add(admin)
        await db.flush()
        print(f"✅ Admin créé: {admin.phone}")
        
        # 3. Créer un agent
        agent = User(
            phone="34987654",
            first_name="Jean",
            last_name="Agent",
            password_hash=hash_password("Agent123!"),
            role=UserRole.AGENT,
            bureau_id=bureau.id,
            is_active=True
        )
        db.add(agent)
        await db.flush()
        print(f"✅ Agent créé: {agent.phone}")
        
        # 4. Créer un joueur test
        player = User(
            phone="34123456",
            first_name="Test",
            last_name="Joueur",
            password_hash=hash_password("Test123!"),
            role=UserRole.PLAYER,
            is_active=True
        )
        db.add(player)
        await db.flush()
        print(f"✅ Joueur créé: {player.phone}")
        
        # 5. Configurer la roue Lucky
        wheel_config = LuckyWheelConfig.get_default_config()
        db.add(wheel_config)
        await db.flush()
        print(f"✅ Configuration roue créée: {wheel_config.name}")
        
        # 6. Créer des tirages Keno
        now = datetime.utcnow()
        for i in range(5):
            draw = KenoDraw(
                draw_number=1000 + i,
                draw_time=now + timedelta(minutes=5 * (i + 1)),
                status="PENDING"
            )
            db.add(draw)
        print(f"✅ 5 tirages Keno programmés")
        
        # 7. Créer une promotion
        promotion = Promotion(
            name="Bonus de Bienvenue",
            code="WELCOME100",
            description="100% de bonus jusqu'à 5000 HTG",
            type=PromotionType.DEPOSIT_BONUS,
            config={
                "min_deposit": 100,
                "bonus_percent": 100,
                "max_bonus": 5000,
                "wagering": 10
            },
            start_date=now,
            end_date=now + timedelta(days=365),
            status=PromotionStatus.ACTIVE,
            new_users_only=True,
            first_deposit_only=True,
            created_by=admin.id
        )
        db.add(promotion)
        print(f"✅ Promotion créée: {promotion.name}")
        
        await db.commit()
        print("\n🎉 Seeding completed successfully!")


if __name__ == "__main__":
    asyncio.run(seed())