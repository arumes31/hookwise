import ipaddress

import pytest

from hookwise.utils import parse_ip_network


def test_parse_ip_network_valid():
    network_str = "192.168.1.0/24"
    net = parse_ip_network(network_str)
    assert isinstance(net, (ipaddress.IPv4Network, ipaddress.IPv6Network))
    assert str(net) == network_str

    # Test caching
    net2 = parse_ip_network(network_str)
    assert net is net2

def test_parse_ip_network_ipv6():
    network_str = "2001:db8::/32"
    net = parse_ip_network(network_str)
    assert isinstance(net, ipaddress.IPv6Network)
    assert str(net) == network_str

def test_parse_ip_network_invalid():
    with pytest.raises(ValueError):
        parse_ip_network("invalid_ip")

def test_parse_ip_network_single_ip():
    network_str = "1.2.3.4"
    net = parse_ip_network(network_str)
    assert str(net) == "1.2.3.4/32"
