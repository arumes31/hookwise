import ipaddress
from unittest.mock import patch

from hookwise.utils import parse_ip_network


def test_parse_ip_network_caching():
    # Clear cache if it exists (it might not yet if we haven't implemented it)
    if hasattr(parse_ip_network, "cache_clear"):
        parse_ip_network.cache_clear()

    with patch("ipaddress.ip_network", wraps=ipaddress.ip_network) as mock_net:
        range1 = "192.168.1.0/24"

        # First call
        net1 = parse_ip_network(range1)
        assert mock_net.call_count == 1

        # Second call with same range
        net2 = parse_ip_network(range1)
        assert mock_net.call_count == 1  # Should be cached
        assert net1 == net2

        # Call with different range
        range2 = "10.0.0.0/8"
        net3 = parse_ip_network(range2)
        assert mock_net.call_count == 2
        assert net3 != net1

def test_parse_ip_network_invalid():
    # Should still call the underlying function and raise ValueError if invalid
    try:
        parse_ip_network("invalid-ip")
    except ValueError:
        pass
    else:
        raise AssertionError("Should have raised ValueError")
