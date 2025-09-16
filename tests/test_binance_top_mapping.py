from pathlib import Path
import yaml

from cryptostorm.universe.binance import _update_config_symbols_and_mapping


def test_update_config_symbols_and_mapping(tmp_path: Path):
    # Prepare a minimal config file
    cfgp = tmp_path / "cfg.yaml"
    cfgp.write_text(yaml.safe_dump({"run": {"run_id": "test", "artifacts_root": "./artifacts"}}))

    symbols = ["BTCUSDT", "ETHUSDT", "1000PEPEUSDT"]
    # Provide a base mapping (simulate exchangeInfo)
    base_map = {"BTCUSDT": "BTC", "ETHUSDT": "ETH", "1000PEPEUSDT": "1000PEPE"}

    _update_config_symbols_and_mapping(cfgp, symbols, base_map)

    # Verify YAML updated
    cfg = yaml.safe_load(cfgp.read_text())
    assert cfg["universe"]["symbols"] == symbols
    s2c = cfg.get("conventions", {}).get("symbol_to_coin", {})
    assert s2c.get("BTCUSDT") == "BTC"
    assert s2c.get("ETHUSDT") == "ETH"
    assert s2c.get("1000PEPEUSDT") == "1000PEPE"

