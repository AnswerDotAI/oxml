"""Marshal Python datetimes; the native review implementation validates timestamps."""
from datetime import datetime

def lexical(date):
    if date is None: return None
    if not isinstance(date, datetime): raise ValueError('date requires a timezone-aware datetime')
    return date.isoformat()
