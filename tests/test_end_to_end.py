"""Plant known defects in a realistic synthetic database and check the answer key."""
import json
from fractions import Fraction

import pytest
from sqlalchemy import create_engine

from reconciler.checks import run_all
from reconciler.cli import main
from reconciler.seed import generate


@pytest.fixture(scope="module")
def synthetic(tmp_path_factory):
    path = tmp_path_factory.mktemp("db") / "synthetic.db"
    url = f"sqlite:///{path}"
    truth = generate(url, n_customers=150, seed=7, defects_per_type=5)
    return url, truth


def found_keys(url):
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return {f.key() for f in run_all(conn, Fraction("0.0825"))}
    finally:
        engine.dispose()


def test_every_planted_defect_is_found_and_nothing_else(synthetic):
    url, truth = synthetic
    planted = {tuple(item) for item in truth["planted"]}
    found = found_keys(url)
    assert planted - found == set(), "missed defects"
    assert found - planted == set(), "false alarms"


def test_data_set_is_large_enough_to_mean_something(synthetic):
    _, truth = synthetic
    assert truth["usage_events"] > 10_000
    assert len(truth["planted"]) == 5 * 5 + 5  # five types, one of which plants two keys


def test_same_seed_gives_identical_data(tmp_path):
    a = generate(f"sqlite:///{tmp_path / 'a.db'}", n_customers=30, seed=3, defects_per_type=2)
    b = generate(f"sqlite:///{tmp_path / 'b.db'}", n_customers=30, seed=3, defects_per_type=2)
    assert a == b


def test_a_clean_database_has_no_findings(tmp_path):
    url = f"sqlite:///{tmp_path / 'clean.db'}"
    generate(url, n_customers=60, seed=11, defects_per_type=0)
    assert found_keys(url) == set()


def test_cli_reports_and_sets_exit_code(synthetic, tmp_path, capsys):
    url, truth = synthetic
    json_path, csv_path = tmp_path / "out.json", tmp_path / "out.csv"
    assert main(["--database-url", url, "--json", str(json_path), "--csv", str(csv_path)]) == 0
    assert main(["--database-url", url, "--fail-on-findings"]) == 1
    rows = json.loads(json_path.read_text(encoding="utf8"))
    assert len(rows) == len({tuple(i) for i in truth["planted"]})
    assert csv_path.read_text(encoding="utf8").startswith("check,customer_id,period,detail")
    assert "finding(s)" in capsys.readouterr().out
