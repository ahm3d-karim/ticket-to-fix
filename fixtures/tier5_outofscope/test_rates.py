from rates import fetch_rate


def test_fetch_rate_with_configured_key():
    assert fetch_rate("USD") == 1.12, "api key missing"
