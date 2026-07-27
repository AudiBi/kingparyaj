# app/seed/seed_admin.py
"""Script pour créer un administrateur par défaut"""

import os
import sys
from datetime import datetime
from sqlalchemy.exc import IntegrityError

# Ajouter le chemin du projet
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app import create_app
from app.extensions import db
from app.models.user import User
from app.models.enums import UserRole, KYCStatus
from app.core.security import hash_password 


def create_default_admin():
    """Crée un administrateur par défaut si aucun n'existe"""
    app = create_app()
    
    with app.app_context():
        # Vérifier s'il existe déjà un admin
        existing_admin = User.query.filter(
            User.role.in_([UserRole.ADMIN, UserRole.SUPER_ADMIN])
        ).first()
        
        if existing_admin:
            print(f"✅ Un administrateur existe déjà : {existing_admin.email}")
            return
        
        # Créer l'administrateur par défaut
        admin_data = {
            "email": "admin@keno365.com",
            "phone": "+22501010101",  # À adapter
            "first_name": "Super",
            "last_name": "Admin",
            "national_id": "ADMIN000001",
            "password_hash": hash_password("Admin@2024!"),  # Mot de passe sécurisé
            "role": UserRole.SUPER_ADMIN,
            "kyc_status": KYCStatus.VERIFIED,
            "is_active": True,
            "is_locked": False,
            "kyc_verified_at": datetime.utcnow(),
            "kyc_verified_by": "system",
            "referral_code": "SUPERADMIN",
            "total_bets_count": 0,
            "total_bets_amount": 0,
            "total_wins": 0,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow()
        }
        
        try:
            admin = User(**admin_data)
            db.session.add(admin)
            db.session.commit()
            print("✅ Administrateur par défaut créé avec succès !")
            print(f"   Email: {admin_data['email']}")
            print(f"   Mot de passe: Admin@2024!")
            print(f"   Rôle: {admin_data['role'].value}")
        except IntegrityError as e:
            db.session.rollback()
            print(f"❌ Erreur lors de la création de l'administrateur : {e}")
        except Exception as e:
            db.session.rollback()
            print(f"❌ Erreur inattendue : {e}")


if __name__ == "__main__":
    create_default_admin()