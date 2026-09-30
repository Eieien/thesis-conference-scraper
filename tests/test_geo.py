from app.pipeline.geo import canonical_country, continent_of


def test_real_country_spellings_from_the_sources():
    # Every spelling below was stored by a real source.
    assert canonical_country("ITALY") == "Italy"
    assert canonical_country("belgium") == "Belgium"
    assert canonical_country("China.") == "China"
    assert canonical_country("USA") == "United States"
    assert canonical_country("UK") == "United Kingdom"
    assert canonical_country("UAE") == "United Arab Emirates"
    assert canonical_country("Viet Nam") == "Vietnam"
    assert canonical_country("The Netherlands") == "Netherlands"
    assert canonical_country("Brasil") == "Brazil"
    assert canonical_country("Czech Republic") == "Czechia"
    assert canonical_country("MI") == "United States"  # "Ann Arbor, MI"
    assert canonical_country("Illinois") == "United States"
    assert canonical_country(None) is None


def test_continents():
    assert continent_of("Hong Kong") == "Asia"
    assert continent_of("Turkey") == "Asia"
    assert continent_of("Cyprus") == "Europe"
    assert continent_of("Mexico") == "North America"
    assert continent_of("Ecuador") == "South America"
    assert continent_of("New Zealand") == "Oceania"
    assert continent_of("ALGERIA") == "Africa"
    assert continent_of("Atlantis") is None
    # Codes that are also country codes are not read as US states.
    assert continent_of("CA") is None
    assert continent_of("DE") is None
