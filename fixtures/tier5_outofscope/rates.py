from config import API_KEY


def fetch_rate(currency):
    if not API_KEY:
        raise RuntimeError("api key missing: set API_KEY in config.py")
    if currency == "USD":
        return 1.12
    raise ValueError(f"unknown currency: {currency}")
