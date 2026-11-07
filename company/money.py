"""EUR accounting: nine decimal places, HALF_UP; never implicit FX conversion."""
from decimal import Decimal, ROUND_HALF_UP
from sqlalchemy import BigInteger, Numeric
from sqlalchemy.types import TypeDecorator

SCALE = Decimal('0.000000001')
FACTOR = Decimal(1_000_000_000)

def money(value, currency='EUR') -> Decimal:
    if currency != 'EUR':
        raise ValueError('Conversion explicite en EUR requise; aucune conversion implicite')
    value = Decimal(str(value))
    if not value.is_finite() or value < 0 or value >= Decimal('1000000000'):
        raise ValueError('Montant hors domaine EUR')
    return value.quantize(SCALE, rounding=ROUND_HALF_UP)

class Money(TypeDecorator):
    impl = Numeric(18, 9)
    cache_ok = True
    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(BigInteger() if dialect.name == 'sqlite' else Numeric(18, 9))
    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        value = money(value)
        return int(value * FACTOR) if dialect.name == 'sqlite' else value
    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return Decimal(value) / FACTOR if dialect.name == 'sqlite' else Decimal(value)
