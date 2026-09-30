import json

from app.config import Settings
from app.pipeline import fx


def test_convert_uses_cached_rates(tmp_path):
    settings = Settings(data_dir=tmp_path)
    (tmp_path / "fx_rates.json").write_text(
        json.dumps({"rates": {"USD": 1, "PHP": 56.0, "CNY": 7.0, "INR": 84.0}}), encoding="utf-8"
    )
    assert fx.convert(100, "USD", "PHP", settings) == 5600.0
    assert fx.convert(700, "RMB", "USD", settings) == 100.0  # RMB is CNY
    assert fx.convert(8400, "INR", "PHP", settings) == 5600.0
    assert fx.convert(5, "XYZ", "USD", settings) is None  # unknown currency: no guess
    assert fx.convert(None, "USD", "PHP", settings) is None


def test_no_rates_means_no_conversion(tmp_path):
    assert fx.convert(100, "USD", "PHP", Settings(data_dir=tmp_path)) is None
