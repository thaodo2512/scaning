import tempfile
from pathlib import Path

from cryptostorm.api.server import _build_app
import pytest

# FastAPI TestClient depends on httpx; skip if unavailable
try:  # pragma: no cover
    from fastapi.testclient import TestClient  # type: ignore
except Exception:  # pragma: no cover
    pytest.skip("httpx not installed; skipping API client tests", allow_module_level=True)


def _mini_cfg(tmp: Path):
    # Minimal config dict with run and universe
    return {
        "run": {"run_id": "testrun", "artifacts_root": str(tmp / "artifacts")},
        "universe": {"symbols": ["BTCUSDT"]},
    }


def test_api_serves_price_and_plotly_reports(tmp_path: Path):
    # Arrange a fake reports dir with files
    reports = tmp_path / "reports"
    reports.mkdir(parents=True)
    (reports / "BTCUSDT_price_alert.html").write_text("<html>price</html>", encoding="utf-8")
    (reports / "BTCUSDT_plotly.html").write_text("<html>plotly</html>", encoding="utf-8")
    (reports / "index.html").write_text("<html>index</html>", encoding="utf-8")

    data_root = tmp_path / "data"
    features_root = tmp_path / "features"
    artifacts_root = tmp_path / "artifacts" / "testrun"
    artifacts_root.mkdir(parents=True, exist_ok=True)

    cfg = _mini_cfg(tmp_path)
    eff = type("E", (), {"symbols": ["BTCUSDT"], "run_id": "testrun"})()

    app = _build_app(
        cfg,
        eff,
        data_root=data_root,
        features_root=features_root,
        artifacts_root=artifacts_root,
        reports_root=reports,
        api_token=None,
    )

    # Act via TestClient
    client = TestClient(app)
    # Root should serve index
    r = client.get("/")
    assert r.status_code == 200
    assert "index" in r.text
    # Price report
    r = client.get("/price/BTCUSDT")
    assert r.status_code == 200
    assert "price" in r.text
    # Plotly report
    r = client.get("/plotly/BTCUSDT")
    assert r.status_code == 200
    assert "plotly" in r.text
    # Static mount should serve the raw file
    r = client.get("/reports/BTCUSDT_price_alert.html")
    assert r.status_code == 200
    assert "price" in r.text
