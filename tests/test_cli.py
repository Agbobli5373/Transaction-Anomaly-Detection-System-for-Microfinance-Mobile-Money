from pathlib import Path

import pandas as pd
import pytest
from conftest import DEFAULT_CONFIG

from txrisk.cli import main


@pytest.fixture
def small_config_file(tmp_path: Path) -> Path:
    text = (
        DEFAULT_CONFIG.read_text(encoding="utf-8")
        .replace("days = 120", "days = 7")
        .replace("wallets = 10000", "wallets = 100")
    )
    path = tmp_path / "small.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_generate_writes_the_dataset(
    small_config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("TXRISK_PSEUDONYM_KEY", "cli-test-key")
    out = tmp_path / "out"

    exit_code = main(
        ["generate", "--config", str(small_config_file), "--seed", "3", "--out", str(out)]
    )

    assert exit_code == 0
    assert len(pd.read_parquet(out / "wallets.parquet")) == 100
    assert len(pd.read_parquet(out / "transactions.parquet")) > 0


def test_generate_refuses_to_run_without_a_pseudonym_key(
    small_config_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.delenv("TXRISK_PSEUDONYM_KEY", raising=False)
    out = tmp_path / "out"

    exit_code = main(["generate", "--config", str(small_config_file), "--out", str(out)])

    assert exit_code != 0
    assert "TXRISK_PSEUDONYM_KEY" in capsys.readouterr().err
    assert not out.exists()
