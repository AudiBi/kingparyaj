"""
Gestion centralisée des fuseaux horaires.

Convention :
- Les timestamps en base sont stockés en UTC (naive, TIMESTAMP WITHOUT TIME ZONE).
- Les périodes métier ("aujourd'hui", un jour choisi dans un filtre, les
  regroupements par jour) sont calculées dans le fuseau local d'Haïti.
- Les bornes sont ensuite converties en UTC avant interrogation de PostgreSQL.
- L'affichage convertit l'UTC stocké en heure d'Haïti (filtre Jinja `local`).
"""

from datetime import date, datetime, time, timedelta
from typing import Optional, Union
from zoneinfo import ZoneInfo

from sqlalchemy import func, literal_column


HAITI_TZ_NAME = "America/Port-au-Prince"
HAITI_TZ = ZoneInfo(HAITI_TZ_NAME)
UTC_TZ = ZoneInfo("UTC")

DateLike = Union[str, date, datetime]


# ============================================================
# "Maintenant"
# ============================================================

def now_utc() -> datetime:
    """
    Retourne l'heure UTC sous forme naive.

    Compatible avec les colonnes SQLAlchemy DateTime
    actuellement utilisées dans BaseModel.
    Remplace datetime.utcnow() (déprécié depuis Python 3.12).
    """
    return datetime.now(UTC_TZ).replace(tzinfo=None)


def now_haiti() -> datetime:
    """Retourne l'heure actuelle en Haïti (aware)."""
    return datetime.now(HAITI_TZ)


def today_haiti() -> date:
    """Date civile du jour en Haïti."""
    return now_haiti().date()


# ============================================================
# Conversions
# ============================================================

def to_haiti(value: Optional[datetime]) -> Optional[datetime]:
    """
    Convertit un datetime stocké en UTC (naive) ou aware vers l'heure
    d'Haïti. Les objets `date` (sans heure) et None sont renvoyés tels quels.
    """
    if value is None or not isinstance(value, datetime):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC_TZ)
    return value.astimezone(HAITI_TZ)


def _to_local_date(value: DateLike) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(value, "%Y-%m-%d").date()


def _local_midnight_utc(d: date) -> datetime:
    return (
        datetime.combine(d, time.min, tzinfo=HAITI_TZ)
        .astimezone(UTC_TZ)
        .replace(tzinfo=None)
    )


def local_date_start_utc(value: DateLike) -> datetime:
    """
    Début (00:00 heure d'Haïti) d'une date civile, en UTC naive.
    Accepte 'YYYY-MM-DD', date ou datetime.
    """
    return _local_midnight_utc(_to_local_date(value))


def local_date_end_utc(value: DateLike) -> datetime:
    """
    Fin EXCLUSIVE (00:00 du lendemain, heure d'Haïti) d'une date civile,
    en UTC naive. À utiliser avec `colonne < fin`.
    """
    return _local_midnight_utc(_to_local_date(value) + timedelta(days=1))


def date_bounds_utc(date_value: DateLike) -> tuple[datetime, datetime]:
    """
    Retourne [début, fin) d'une date civile haïtienne
    convertie en UTC.
    """
    return local_date_start_utc(date_value), local_date_end_utc(date_value)


def today_bounds_utc() -> tuple[datetime, datetime]:
    """
    Retourne [début, fin) de la journée actuelle en Haïti,
    convertie en UTC et rendue naive pour PostgreSQL
    TIMESTAMP WITHOUT TIME ZONE.
    """
    return date_bounds_utc(today_haiti())


# ============================================================
# SQL
# ============================================================

def local_day(column):
    """
    Expression SQL : date civile haïtienne d'une colonne stockée en UTC naive.

    timezone('UTC', col)            -> timestamptz (on déclare la valeur UTC)
    timezone('America/...', <tz>)   -> timestamp local Haïti
    date(...)                       -> date civile locale

    Les noms de fuseaux sont des littéraux SQL (pas des paramètres liés) :
    l'expression est donc identique dans SELECT / GROUP BY / ORDER BY, ce qui
    évite l'erreur PostgreSQL « must appear in the GROUP BY clause ».
    """
    return func.date(
        func.timezone(
            literal_column(f"'{HAITI_TZ_NAME}'"),
            func.timezone(literal_column("'UTC'"), column),
        )
    )
