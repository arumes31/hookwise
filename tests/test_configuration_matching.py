from dataclasses import FrozenInstanceError

import pytest

from hookwise.services.configuration_matching import (
    ConfigurationHints,
    extract_configuration_hints,
    select_configuration,
)
from hookwise.services.routing import apply_mapping


def test_extracts_greenbone_ip_from_host_port_and_url_once():
    title = "Alert: CVE-2010-2687 S:7.5 - Site2Nite Boat Classifieds on 10.70.10.20:7090/tcp [v1:9e2e50a7a]"
    description = """
Evidence
Vulnerable URL: http://10.70.10.20:7090/products/boat-webdesign/www/detail.asp?ID=999999

Greenbone context
Asset: 10.70.10.20:7090/tcp
"""

    hints = extract_configuration_hints({}, title=title, description=description)

    assert hints.ip_addresses == ("10.70.10.20",)


def test_extracts_canonical_bracketed_ipv6_and_rejects_non_device_addresses():
    hints = extract_configuration_hints(
        {},
        title=(
            "Target [2001:0db8:0000:0000:0000:0000:0000:0010]:443/tcp; "
            "ignore [::]:80/tcp, [::1]:80/tcp, and [ff02::1]:80/udp"
        ),
    )

    assert hints.ip_addresses == ("2001:db8::10",)


def test_explicit_mapped_hints_override_discovered_values_by_kind():
    payload = {
        "device": {
            "ip_address": "192.0.2.20:443/tcp",
            "mac_address": "00-15-5D-65-66-88",
            "serial_number": " discovered-serial ",
            "asset_tag": " discovered-tag ",
            "device_id": " discovered-device ",
            "hostname": "discovered-host",
        }
    }
    mapped = {
        "configuration_id": "137",
        "configuration_ip": "192.168.100.229:7090/tcp",
        "configuration_mac": "00:11:22:33:44:55",
        "configuration_serial": " MAPPED-SERIAL ",
        "configuration_tag": "MAPPED-TAG",
        "configuration_device_id": "MAPPED-DEVICE",
        "configuration_name": " DEXTER ",
    }

    hints = extract_configuration_hints(payload, mapped)

    assert hints.configuration_ids == (137,)
    assert hints.ip_addresses == ("192.168.100.229",)
    assert hints.mac_addresses == ("001122334455",)
    assert hints.serial_numbers == ("mapped-serial",)
    assert hints.tag_numbers == ("mapped-tag",)
    assert hints.device_identifiers == ("mapped-device",)
    assert hints.names == ("dexter",)


def test_discovers_only_explicitly_labelled_common_fields():
    payload = {
        "nested": [
            {
                "ipAddress": "192.168.100.229:7090/tcp",
                "mac-address": "00-15-5D-65-66-88",
                "serialNumber": "SN-123",
                "tag_number": "TAG-8",
                "deviceIdentifier": "DEVICE-9",
                "host_name": "DEXTER",
            },
            {"notes": "192.168.100.230 00:11:22:33:44:55 SN-ignored"},
        ]
    }

    hints = extract_configuration_hints(payload)

    assert hints.ip_addresses == ("192.168.100.229",)
    assert hints.mac_addresses == ("00155d656688",)
    assert hints.serial_numbers == ("sn-123",)
    assert hints.tag_numbers == ("tag-8",)
    assert hints.device_identifiers == ("device-9",)
    assert hints.names == ("dexter",)


def test_treats_structured_asset_and_target_as_ip_endpoints_only():
    payload = {
        "asset": "10.70.10.20:7090/tcp",
        "target": "[2001:db8::20]:443/tcp",
        "nested": {"asset": "DEXTER", "target": "scanner.example.com:443"},
    }

    hints = extract_configuration_hints(payload)

    assert hints.ip_addresses == ("10.70.10.20", "2001:db8::20")
    assert hints.names == ()


def test_bounds_payload_traversal_and_candidates():
    payload = {"items": [{"ip": f"10.0.0.{index}"} for index in range(1, 40)]}

    hints = extract_configuration_hints(payload)

    assert len(hints.ip_addresses) == 16
    assert hints.ip_addresses[0] == "10.0.0.1"
    assert hints.ip_addresses[-1] == "10.0.0.16"


def test_candidate_bound_applies_across_multiple_labelled_collections():
    payload = {
        "ip": [f"10.0.0.{index}" for index in range(1, 11)],
        "target": [f"10.0.1.{index}" for index in range(1, 17)],
    }

    hints = extract_configuration_hints(payload)

    assert len(hints.ip_addresses) == 16


def test_handles_a_cyclic_non_json_mapping_without_recursing_forever():
    payload: dict[str, object] = {"ip": "192.168.1.20"}
    payload["self"] = payload

    hints = extract_configuration_hints(payload)

    assert hints.ip_addresses == ("192.168.1.20",)


def test_rejects_invalid_or_unsafe_identifiers_and_unlabelled_values():
    payload = {
        "message": "serial ABC and MAC 00:11:22:33:44:55",
        "arbitrary_number": 137,
        "ip": "127.0.0.1:443/tcp",
        "mac": "ff:ff:ff:ff:ff:ff",
        "serial": "abc' OR '1'='1",
        "asset_tag": "bad\nvalue",
        "device_id": True,
        "hostname": "server",
    }

    hints = extract_configuration_hints(payload)

    assert not hints.has_identifiers


def test_extracts_other_identifiers_only_from_explicit_text_labels():
    description = """
Device ID: DEVICE-9
Serial number: SN-123
MAC address: 00-15-5D-65-66-88
Asset tag: TAG-8
Hostname: DEXTER
"""

    hints = extract_configuration_hints({}, description=description)

    assert hints.device_identifiers == ("device-9",)
    assert hints.serial_numbers == ("sn-123",)
    assert hints.mac_addresses == ("00155d656688",)
    assert hints.tag_numbers == ("tag-8",)
    assert hints.names == ("dexter",)


def test_accepts_apostrophes_in_exact_configuration_names():
    hints = extract_configuration_hints({"hostname": "O'Malley's Laptop"})

    assert hints.names == ("o'malley's laptop",)


def test_extracted_hints_are_immutable():
    hints = extract_configuration_hints({"ip": "192.168.1.20"})

    with pytest.raises(FrozenInstanceError):
        hints.ip_addresses = ("192.168.1.21",)  # type: ignore[misc]


def test_routing_mapping_resolves_explicit_configuration_hints():
    data = {
        "device": {
            "cw_id": 137,
            "id": "device-9",
            "serial": "SN-123",
            "mac": "00:11:22:33:44:55",
            "tag": "TAG-8",
            "ip": "10.70.10.20:7090/tcp",
            "hostname": "DEXTER",
        }
    }
    raw_mapping = """{
        "configuration_id": "$.device.cw_id",
        "configuration_device_id": "$.device.id",
        "configuration_serial": "$.device.serial",
        "configuration_mac": "$.device.mac",
        "configuration_tag": "$.device.tag",
        "configuration_ip": "$.device.ip",
        "configuration_name": "$.device.hostname"
    }"""

    mapped = apply_mapping(data, raw_mapping)

    assert mapped == {
        "configuration_id": "137",
        "configuration_device_id": "device-9",
        "configuration_serial": "SN-123",
        "configuration_mac": "00:11:22:33:44:55",
        "configuration_tag": "TAG-8",
        "configuration_ip": "10.70.10.20:7090/tcp",
        "configuration_name": "DEXTER",
    }


def test_selector_returns_no_identifiers_without_searchable_hints():
    result = select_configuration([], 42, ConfigurationHints())

    assert result.status == "no_identifiers"
    assert result.configuration_id is None
    assert result.match_type is None


def test_selector_matches_unique_active_ip_only_inside_final_company():
    hints = ConfigurationHints(ip_addresses=("10.70.10.20",))
    configurations = [
        {
            "id": 137,
            "name": "DEXTER",
            "company": {"id": 42},
            "activeFlag": True,
            "ipAddress": "10.70.10.20:7090/tcp",
        },
        {
            "id": 138,
            "name": "OTHER-COMPANY",
            "company": {"id": 99},
            "activeFlag": True,
            "ipAddress": "10.70.10.20",
        },
        {
            "id": 139,
            "name": "INACTIVE",
            "company": {"id": 42},
            "activeFlag": False,
            "ipAddress": "10.70.10.20",
        },
    ]

    result = select_configuration(configurations, 42, hints)

    assert result.status == "matched"
    assert result.configuration_id == 137
    assert result.match_type == "ip"


def test_selector_returns_ambiguous_for_duplicate_ip_in_company():
    hints = ConfigurationHints(ip_addresses=("10.70.10.20",))
    configurations = [
        {"id": 137, "company": {"id": 42}, "activeFlag": True, "ipAddress": "10.70.10.20"},
        {"id": 138, "company": {"id": 42}, "activeFlag": True, "ipAddress": "10.70.10.20"},
    ]

    result = select_configuration(configurations, 42, hints)

    assert result.status == "ambiguous"
    assert result.configuration_id is None
    assert result.match_type == "ip"


def test_selector_returns_conflict_when_serial_and_ip_point_to_different_assets():
    hints = ConfigurationHints(serial_numbers=("sn-123",), ip_addresses=("10.70.10.20",))
    configurations = [
        {
            "id": 137,
            "company": {"id": 42},
            "activeFlag": True,
            "serialNumber": "SN-123",
            "ipAddress": "10.70.10.21",
        },
        {
            "id": 138,
            "company": {"id": 42},
            "activeFlag": True,
            "serialNumber": "SN-999",
            "ipAddress": "10.70.10.20",
        },
    ]

    result = select_configuration(configurations, 42, hints)

    assert result.status == "conflict"
    assert result.configuration_id is None
    assert result.match_type is None


def test_selector_uses_corroborating_serial_to_disambiguate_duplicate_ip():
    hints = ConfigurationHints(serial_numbers=("sn-123",), ip_addresses=("10.70.10.20",))
    configurations = [
        {
            "id": 137,
            "company": {"id": 42},
            "activeFlag": True,
            "serialNumber": "SN-123",
            "ipAddress": "10.70.10.20",
        },
        {
            "id": 138,
            "company": {"id": 42},
            "activeFlag": True,
            "serialNumber": "SN-999",
            "ipAddress": "10.70.10.20",
        },
    ]

    result = select_configuration(configurations, 42, hints)

    assert result.status == "matched"
    assert result.configuration_id == 137
    assert result.match_type == "serial"


@pytest.mark.parametrize(
    ("hint", "field", "api_value", "match_type"),
    [
        (ConfigurationHints(device_identifiers=("device-9",)), "deviceIdentifier", "DEVICE-9", "device_identifier"),
        (ConfigurationHints(serial_numbers=("sn-123",)), "serialNumber", "SN-123", "serial"),
        (ConfigurationHints(mac_addresses=("00155d656688",)), "macAddress", "00-15-5D-65-66-88", "mac"),
        (ConfigurationHints(tag_numbers=("tag-8",)), "tagNumber", "TAG-8", "tag"),
        (ConfigurationHints(names=("dexter",)), "name", "DEXTER", "name"),
    ],
)
def test_selector_normalizes_each_exact_api_identity(hint, field, api_value, match_type):
    configuration = {"id": 137, "company": {"id": 42}, "activeFlag": True, field: api_value}

    result = select_configuration([configuration], 42, hint)

    assert result.status == "matched"
    assert result.configuration_id == 137
    assert result.match_type == match_type


def test_selector_verifies_explicit_configuration_id_company_and_active_state():
    hints = ConfigurationHints(configuration_ids=(137,))

    wrong_company = select_configuration(
        [{"id": 137, "company": {"id": 99}, "activeFlag": True}],
        42,
        hints,
    )
    inactive = select_configuration(
        [{"id": 137, "company": {"id": 42}, "activeFlag": False}],
        42,
        hints,
    )
    matched = select_configuration(
        [{"id": 137, "company": {"id": 42}, "activeFlag": True}],
        42,
        hints,
    )

    assert wrong_company.status == "no_match"
    assert inactive.status == "no_match"
    assert matched.status == "matched"
    assert matched.configuration_id == 137
    assert matched.match_type == "configuration_id"


def test_selector_ignores_malformed_external_configuration_records():
    hints = ConfigurationHints(names=("dexter",))
    configurations = [
        None,
        {},
        {"id": True, "company": {"id": 42}, "activeFlag": True, "name": "DEXTER"},
        {"id": 137, "company": {"id": "42"}, "activeFlag": True, "name": "DEXTER"},
        {"id": 138, "company": {"id": 42}, "activeFlag": "true", "name": "DEXTER"},
    ]

    result = select_configuration(configurations, 42, hints)

    assert result.status == "no_match"


def test_selector_never_claims_unique_match_when_configuration_input_is_truncated():
    configurations = [
        {"id": 137, "company": {"id": 42}, "activeFlag": True, "ipAddress": "10.70.10.20"},
        *[
            {"id": config_id, "company": {"id": 42}, "activeFlag": True, "ipAddress": "10.70.10.99"}
            for config_id in range(200, 328)
        ],
    ]

    result = select_configuration(configurations, 42, ConfigurationHints(ip_addresses=("10.70.10.20",)))

    assert result.status == "ambiguous"
    assert result.configuration_id is None


def test_match_result_is_immutable():
    result = select_configuration(
        [{"id": 137, "company": {"id": 42}, "activeFlag": True, "name": "DEXTER"}],
        42,
        ConfigurationHints(names=("dexter",)),
    )

    with pytest.raises(FrozenInstanceError):
        result.status = "no_match"  # type: ignore[misc]
