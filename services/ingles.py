"""English text for the public weather page.

The forecast data (services/analytics.py) is built with Spanish labels, and in option A it is built
inside the ClimateDbV2 Lambda. These filters turn those labels into English in the portal itself,
so the page is in English without redeploying any Lambda. English text passes through unchanged.
"""
import re
from datetime import date, datetime, timedelta

DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
LONG_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September",
               "October", "November", "December")
SPANISH_MONTHS = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")

WORDS = {
    # conditions (forecast and readings)
    "Despejado": "Clear", "Nubes y claros": "Partly cloudy", "Nublado": "Cloudy", "Niebla": "Fog",
    "Llovizna": "Drizzle", "Lluvia": "Rain", "Posible lluvia": "Chance of rain", "Tormenta": "Storm",
    "Nieve": "Snow", "Parcialmente nublado": "Partly cloudy",
    # icon names (used as aria-label)
    "despejado": "clear", "nubes-claros": "partly cloudy", "nublado": "cloudy", "niebla": "fog",
    "lluvia-posible": "chance of rain", "lluvia": "rain", "tormenta": "storm", "nieve": "snow",
    # UV level and sunscreen
    "Bajo": "Low", "Moderado": "Moderate", "Alto": "High", "Muy alto": "Very high", "Extremo": "Extreme",
    "no": "none",
    # wind
    "Norte": "North", "Nornoreste": "North-northeast", "Noreste": "Northeast", "Estenoreste": "East-northeast",
    "Este": "East", "Estesureste": "East-southeast", "Sureste": "Southeast", "Sursureste": "South-southeast",
    "Sur": "South", "Sursuroeste": "South-southwest", "Suroeste": "Southwest", "Oestesuroeste": "West-southwest",
    "Oeste": "West", "Oestenoroeste": "West-northwest", "Noroeste": "Northwest", "Nornoroeste": "North-northwest",
}

_SHORT_DATE = re.compile(r"^(\d{1,2}) ([a-z]{3}) (\d{4}), (\d{2}:\d{2})$")   # "18 nov 2026, 17:00"


def en(text):
    """Spanish label -> English label. Unknown text is returned as is."""
    if text is None:
        return ""
    text = str(text)
    if text in WORDS:
        return WORDS[text]
    match = _SHORT_DATE.match(text)
    if match and match.group(2) in SPANISH_MONTHS:
        day, month, year, time = match.groups()
        return f"{MONTHS[SPANISH_MONTHS.index(month)]} {int(day)}, {year}, {time}"
    return text


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def day_name(day, today):
    """'Today', 'Tomorrow' or the weekday."""
    try:
        day, today = _as_date(day), _as_date(today)
    except (TypeError, ValueError):
        return en(day)
    if day == today:
        return "Today"
    if day == today + timedelta(days=1):
        return "Tomorrow"
    return DAYS[day.weekday()]


def short_date(day):
    """2026-11-23 -> 'Nov 23'."""
    try:
        day = _as_date(day)
    except (TypeError, ValueError):
        return en(day)
    return f"{MONTHS[day.month - 1]} {day.day}"


def long_date(day):
    """2026-11-23 -> 'Monday, November 23'."""
    try:
        day = _as_date(day)
    except (TypeError, ValueError):
        return en(day)
    return f"{DAYS[day.weekday()]}, {LONG_MONTHS[day.month - 1]} {day.day}"


def registrar(app):
    app.jinja_env.filters.update(en=en, day_name=day_name, short_date=short_date, long_date=long_date)
