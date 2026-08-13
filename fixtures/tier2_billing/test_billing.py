from billing import invoice


def test_invoice_applies_config_tax_rate():
    assert invoice(100) == 118.0, "invoice 100 should be 118"
