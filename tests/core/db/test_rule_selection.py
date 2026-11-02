from datetime import UTC, datetime

import pytest

from core.db import RuleSelectionStore, create_db_engine

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
KNOWN = {"S01", "W02", "H01"}


@pytest.fixture
def store(tmp_path):
    return RuleSelectionStore(create_db_engine(tmp_path / "kg.db"))


def test_no_rows_means_every_rule_is_on(store):
    assert store.disabled() == set()


def test_save_disables_the_rules_that_were_not_enabled(store):
    store.save({"S01", "H01"}, KNOWN, NOW)

    assert store.disabled() == {"W02"}


def test_save_again_turns_a_rule_back_on(store):
    store.save({"S01"}, KNOWN, NOW)
    store.save(KNOWN, KNOWN, NOW)

    assert store.disabled() == set()


@pytest.mark.parametrize("bad", [{"S01", "X99"}, {"S01", "R01"}])
def test_unknown_or_candidate_id_is_rejected_and_nothing_is_saved(store, bad):
    with pytest.raises(ValueError, match="X99|R01"):
        store.save(bad, KNOWN, NOW)

    assert store.disabled() == set()
