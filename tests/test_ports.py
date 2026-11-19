"""Direct-port pool behaviour."""

from __future__ import annotations

import pytest
from envd.tools.ports import PortPool, PortPoolError


def test_allocate_first_free():
    pool = PortPool(8100, 8103)
    assert pool.allocate(set()) == 8100
    assert pool.allocate({8100}) == 8101
    assert pool.allocate({8100, 8102}) == 8101
    with pytest.raises(PortPoolError):
        pool.allocate({8100, 8101, 8102, 8103})


def test_allocate_exhausted_raises():
    pool = PortPool(8100, 8101)
    with pytest.raises(PortPoolError):
        pool.allocate({8100, 8101})


def test_validate_range():
    pool = PortPool(8100, 8199)
    assert pool.validate(8150) == 8150
    with pytest.raises(PortPoolError):
        pool.validate(8069)
    with pytest.raises(PortPoolError):
        pool.validate(9000)


@pytest.mark.parametrize("lo,hi", [(0, 10), (8100, 8100), (70000, 70001), (9000, 100)])
def test_invalid_range(lo, hi):
    with pytest.raises(PortPoolError):
        PortPool(lo, hi)
