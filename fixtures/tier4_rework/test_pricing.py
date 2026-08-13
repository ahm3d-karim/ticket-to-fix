from pricing import total


def test_total_100_with_config_tax_rate():
    assert total(100) == 118.0, "total 100 should be 118"


def test_total_50():
    assert total(50) == 59.0, "total 50 should be 59"


def test_total_200():
    assert total(200) == 236.0, "total 200 should be 236"


def test_total_1000():
    assert total(1000) == 1180.0, "total 1000 should be 1180"


def test_total_zero():
    assert total(0) == 0.0, "total 0 should be 0"
