import pytest

from tests.service.checks.detectors.cases import MemoryObservations


@pytest.fixture
def obs():
    return MemoryObservations()
