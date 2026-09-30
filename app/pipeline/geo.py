"""Country names to continents. Sources spell countries many ways ("USA", "ITALY", "China.",
"Viet Nam", even US states), so names are cleaned to one canonical form first."""

import re

CONTINENTS = ["Africa", "Asia", "Europe", "North America", "South America", "Oceania"]

_BY_CONTINENT = {
    "Africa": """Algeria, Angola, Benin, Botswana, Burkina Faso, Burundi, Cameroon, Cape Verde,
        Central African Republic, Chad, Comoros, Congo, Democratic Republic of the Congo, Djibouti,
        Egypt, Equatorial Guinea, Eritrea, Eswatini, Ethiopia, Gabon, Gambia, Ghana, Guinea,
        Guinea-Bissau, Ivory Coast, Kenya, Lesotho, Liberia, Libya, Madagascar, Malawi, Mali,
        Mauritania, Mauritius, Morocco, Mozambique, Namibia, Niger, Nigeria, Rwanda,
        Sao Tome and Principe, Senegal, Seychelles, Sierra Leone, Somalia, South Africa,
        South Sudan, Sudan, Tanzania, Togo, Tunisia, Uganda, Zambia, Zimbabwe""",
    "Asia": """Afghanistan, Armenia, Azerbaijan, Bahrain, Bangladesh, Bhutan, Brunei, Cambodia,
        China, Georgia, Hong Kong, India, Indonesia, Iran, Iraq, Israel, Japan, Jordan, Kazakhstan,
        Kuwait, Kyrgyzstan, Laos, Lebanon, Macau, Malaysia, Maldives, Mongolia, Myanmar, Nepal,
        North Korea, Oman, Pakistan, Palestine, Philippines, Qatar, Saudi Arabia, Singapore,
        South Korea, Sri Lanka, Syria, Taiwan, Tajikistan, Thailand, Timor-Leste, Turkey,
        Turkmenistan, United Arab Emirates, Uzbekistan, Vietnam, Yemen""",
    "Europe": """Albania, Andorra, Austria, Belarus, Belgium, Bosnia and Herzegovina, Bulgaria,
        Croatia, Cyprus, Czechia, Denmark, Estonia, Finland, France, Germany, Greece, Hungary,
        Iceland, Ireland, Italy, Kosovo, Latvia, Liechtenstein, Lithuania, Luxembourg, Malta,
        Moldova, Monaco, Montenegro, Netherlands, North Macedonia, Norway, Poland, Portugal,
        Romania, Russia, San Marino, Serbia, Slovakia, Slovenia, Spain, Sweden, Switzerland,
        Ukraine, United Kingdom, Vatican City""",
    "North America": """Antigua and Barbuda, Bahamas, Barbados, Belize, Canada, Costa Rica, Cuba,
        Dominica, Dominican Republic, El Salvador, Grenada, Guatemala, Haiti, Honduras, Jamaica,
        Mexico, Nicaragua, Panama, Puerto Rico, Saint Lucia, Trinidad and Tobago,
        United States""",
    "South America": """Argentina, Bolivia, Brazil, Chile, Colombia, Ecuador, Guyana, Paraguay,
        Peru, Suriname, Uruguay, Venezuela""",
    "Oceania": """Australia, Fiji, Kiribati, Marshall Islands, Micronesia, Nauru, New Zealand,
        Palau, Papua New Guinea, Samoa, Solomon Islands, Tonga, Tuvalu, Vanuatu""",
}

CONTINENT_OF: dict[str, str] = {
    country.strip(): continent
    for continent, names in _BY_CONTINENT.items()
    for country in re.split(r",\s*", " ".join(names.split()))
}

# Full state names, plus only the abbreviations that are not also ISO country codes
# ("CA" is Canada, "DE" Germany, "IN" India, "MD" Moldova, ...).
_US_STATES = """alabama alaska arizona arkansas california colorado connecticut delaware florida
    hawaii idaho illinois indiana iowa kansas kentucky louisiana maine maryland massachusetts
    michigan minnesota mississippi missouri montana nebraska nevada new-hampshire new-jersey
    new-mexico new-york north-carolina north-dakota ohio oklahoma oregon pennsylvania
    rhode-island south-carolina south-dakota tennessee texas utah vermont virginia washington
    west-virginia wisconsin wyoming mi ny nj tx fl oh wa dc ia nv ut wi""".split()

_ALIASES: dict[str, str] = {
    **{s.replace("-", " "): "United States" for s in _US_STATES},
    "usa": "United States",
    "us": "United States",
    "u.s": "United States",  # keys lose their trailing dot in canonical_country
    "u.s.a": "United States",
    "united states of america": "United States",
    "america": "United States",
    "d.c. national capital region": "United States",
    "uk": "United Kingdom",
    "u.k": "United Kingdom",
    "great britain": "United Kingdom",
    "britain": "United Kingdom",
    "england": "United Kingdom",
    "scotland": "United Kingdom",
    "wales": "United Kingdom",
    "northern ireland": "United Kingdom",
    "uae": "United Arab Emirates",
    "u.a.e": "United Arab Emirates",
    "viet nam": "Vietnam",
    "the netherlands": "Netherlands",
    "holland": "Netherlands",
    "czech republic": "Czechia",
    "brasil": "Brazil",
    "korea": "South Korea",
    "republic of korea": "South Korea",
    "korea, republic of": "South Korea",
    "prc": "China",
    "p.r. china": "China",
    "people's republic of china": "China",
    "hong kong sar": "Hong Kong",
    "macao": "Macau",
    "russian federation": "Russia",
    "türkiye": "Turkey",
    "turkiye": "Turkey",
    "côte d'ivoire": "Ivory Coast",
    "cote d'ivoire": "Ivory Coast",
    "macedonia": "North Macedonia",
    "swaziland": "Eswatini",
    "burma": "Myanmar",
    "ksa": "Saudi Arabia",
}
_CANONICAL = {name.casefold(): name for name in CONTINENT_OF}


def canonical_country(raw: str | None) -> str | None:
    """ "ITALY" -> "Italy", "USA" -> "United States", "China." -> "China". Unknown names pass."""
    if not raw:
        return None
    key = " ".join(raw.strip().strip(".,;").split()).casefold()
    if not key:
        return None
    return _ALIASES.get(key) or _CANONICAL.get(key) or raw.strip()


def continent_of(raw_country: str | None) -> str | None:
    country = canonical_country(raw_country)
    return CONTINENT_OF.get(country) if country else None
