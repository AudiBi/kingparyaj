# tests/test_services/test_phone_format.py
"""Téléphone haïtien à la création d'un utilisateur ou d'un agent (admin) : 3, 4 ou 5 au début."""

import pytest
from pydantic import ValidationError

from app.schemas.admin import AdminAgentCreate, AdminUserCreate


def _user(phone):
    return AdminUserCreate(phone=phone, password="Secret123!")


def _agent(phone):
    return AdminAgentCreate(phone=phone, password="Secret123!", first_name="Jean", last_name="Paul", bureau_id="b1")


@pytest.mark.parametrize("phone", ["34123456", "41234567", "51234567", "5123-4567", "51 23 45 67"])
@pytest.mark.parametrize("build", [_user, _agent])
def test_numbers_starting_with_3_4_or_5_are_accepted(build, phone):
    assert build(phone).phone == phone.replace(" ", "").replace("-", "")


@pytest.mark.parametrize("phone", ["61234567", "21234567", "5123456", "512345678", "5123456a"])
@pytest.mark.parametrize("build", [_user, _agent])
def test_other_numbers_are_refused(build, phone):
    with pytest.raises(ValidationError, match="Téléphone invalide"):
        build(phone)
