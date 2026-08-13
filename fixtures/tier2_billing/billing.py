def invoice(amount):
    return amount * (1 + 0.15)  # BUG: hardcoded rate, ignores config.TAX_RATE
