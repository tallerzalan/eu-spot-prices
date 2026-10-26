"""Single source of truth for the bidding zones shown in the dashboard.

Only zones that publish day-ahead (spot) prices in EUR on the ENTSO-E
Transparency Platform are listed. Sub-areas, cable links and legacy aggregates
(DE_AT_LU, IT_SACO_AC, NO_2_NSL, ...) are deliberately left out, and so is
UA_IPS, which is quoted in UAH.
"""

# zone code (entsoe-py Area name) -> (display name, market region)
ZONES = {
    # CWE (+ Austria and Switzerland)
    "AT": ("Austria", "CWE"),
    "BE": ("Belgium", "CWE"),
    "CH": ("Switzerland", "CWE"),
    "DE_LU": ("Germany / Luxembourg", "CWE"),
    "FR": ("France", "CWE"),
    "NL": ("Netherlands", "CWE"),
    # Nordics
    "DK_1": ("West Denmark", "Nordics"),
    "DK_2": ("East Denmark", "Nordics"),
    "FI": ("Finland", "Nordics"),
    "NO_1": ("Norway 1 - Oslo", "Nordics"),
    "NO_2": ("Norway 2 - Kristiansand", "Nordics"),
    "NO_3": ("Norway 3 - Trondheim", "Nordics"),
    "NO_4": ("Norway 4 - Tromso", "Nordics"),
    "NO_5": ("Norway 5 - Bergen", "Nordics"),
    "SE_1": ("Sweden 1 - Lulea", "Nordics"),
    "SE_2": ("Sweden 2 - Sundsvall", "Nordics"),
    "SE_3": ("Sweden 3 - Stockholm", "Nordics"),
    "SE_4": ("Sweden 4 - Malmo", "Nordics"),
    # Baltics
    "EE": ("Estonia", "Baltics"),
    "LV": ("Latvia", "Baltics"),
    "LT": ("Lithuania", "Baltics"),
    # Eastern Europe
    "PL": ("Poland", "Eastern Europe"),
    "CZ": ("Czechia", "Eastern Europe"),
    "SK": ("Slovakia", "Eastern Europe"),
    "HU": ("Hungary", "Eastern Europe"),
    "RO": ("Romania", "Eastern Europe"),
    "BG": ("Bulgaria", "Eastern Europe"),
    "MD": ("Moldova", "Eastern Europe"),
    # Balkan
    "SI": ("Slovenia", "Balkan"),
    "HR": ("Croatia", "Balkan"),
    "RS": ("Serbia", "Balkan"),
    "ME": ("Montenegro", "Balkan"),
    "MK": ("North Macedonia", "Balkan"),
    "AL": ("Albania", "Balkan"),
    "XK": ("Kosovo", "Balkan"),
    "GR": ("Greece", "Balkan"),
    # Italy
    "IT_NORD": ("Italy North", "Italy"),
    "IT_CNOR": ("Italy Centre-North", "Italy"),
    "IT_CSUD": ("Italy Centre-South", "Italy"),
    "IT_SUD": ("Italy South", "Italy"),
    "IT_CALA": ("Italy Calabria", "Italy"),
    "IT_SICI": ("Italy Sicily", "Italy"),
    "IT_SARD": ("Italy Sardinia", "Italy"),
    # Iberia
    "ES": ("Spain", "Iberia"),
    "PT": ("Portugal", "Iberia"),
    # Everything else
    "IE_SEM": ("Ireland (SEM)", "Other"),
}

REGION_ORDER = ["CWE", "Nordics", "Baltics", "Eastern Europe", "Balkan", "Italy", "Iberia", "Other"]

ZONE_CODES = list(ZONES)
ZONE_NAME = {code: name for code, (name, _) in ZONES.items()}
ZONE_REGION = {code: region for code, (_, region) in ZONES.items()}
