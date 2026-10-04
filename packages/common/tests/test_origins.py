"""voice_common.origins: the store and its consumers spell a place one way (D41)."""

from __future__ import annotations

import pytest

from voice_common.origins import normalise


def test_an_entry_without_a_scheme_means_https_and_its_default_port():
    assert normalise("HA.lan.") == "https://ha.lan:443"
    assert normalise("ha.lan") != normalise("http://ha.lan")


def test_a_broker_address_is_a_place_a_password_may_go():
    assert normalise("mqtt://broker") == "mqtt://broker:1883"
    assert normalise("mqtts://broker") == "mqtts://broker:8883"
    assert normalise("mqtt://user:pw@broker:1884", entry=False) == "mqtt://broker:1884"


def test_a_non_ascii_name_is_encoded_as_the_client_connects_to_it():
    assert normalise("https://faß.de/x", entry=False) == "https://xn--fa-hia.de:443"
    assert normalise("https://faß.de/x", entry=False) != normalise("fass.de")


@pytest.mark.parametrize("text", ["ftp://ha.lan", "https://ha.lan/api", "127.1",
                                  "https://ha.lan:99999", "ex‍ample.com", ""])
def test_what_is_not_a_place_is_refused(text):
    with pytest.raises(ValueError):
        normalise(text)


def test_a_target_must_name_its_scheme():
    with pytest.raises(ValueError):
        normalise("ha.lan", entry=False)
