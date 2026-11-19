"""Optional direct-port pool.

Envs are routed through Traefik and publish no host port by default; a
``direct_port`` is an opt-in escape hatch for XML-RPC debugging. Allocation
is first-free in the configured range, and released ports return to the pool.
"""

from __future__ import annotations


class PortPoolError(ValueError):
    """Raised when no port is available or the range is invalid."""


class PortPool:
    def __init__(self, low: int, high: int) -> None:
        if not (0 < low < high < 65536):
            raise PortPoolError(f"invalid port range ({low}, {high})")
        self.low = low
        self.high = high

    def allocate(self, used: set[int]) -> int:
        """First free port in the range; raises PortPoolError if exhausted."""
        for port in range(self.low, self.high + 1):
            if port not in used:
                return port
        raise PortPoolError(f"port pool exhausted ({self.low}-{self.high}, {len(used)} in use)")

    def validate(self, port: int) -> int:
        """Check that a manually requested port is inside the range."""
        if not (self.low <= port <= self.high):
            raise PortPoolError(f"port {port} outside configured range {self.low}-{self.high}")
        return port
