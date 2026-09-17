from watcher import (
    extract_bedrooms,
    extract_price,
    extract_reference,
    extract_surface,
    is_available,
)


def test_structura_parser():
    text = """
    Magnifique appartement
    € 1.245 p/m
    Surface habitable
    85 m²
    Chambres
    2
    """
    assert extract_price(text) == 1245
    assert extract_bedrooms(text) == 2
    assert extract_surface(text) == 85
    assert is_available(text)


def test_living_stone_parser():
    text = """
    Appartement à louer in leuven
    € 1.375/Maand
    Aantal slaapkamers
    2
    Bewoonbare oppervlakte
    104 m²
    Réf. : #36582
    """
    assert extract_price(text) == 1375
    assert extract_bedrooms(text) == 2
    assert extract_surface(text) == 104
    assert extract_reference(text) == "36582"


def test_unavailable():
    assert not is_available("En option")
    assert not is_available("In optie/Maand")
    assert not is_available("VISITES COMPLÈTES")
