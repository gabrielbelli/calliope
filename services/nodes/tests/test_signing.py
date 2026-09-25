"""Firmware signatures on the hub, and the contract with the script that makes
them (clients/korvo-node/scripts/firmware_signing.py): a signature the upload
script produces must pass here, and one the node would refuse must not.

Every key is a throwaway made in the test's temporary directory. No private key
is read from, or written to, anywhere else.
"""

from __future__ import annotations

import base64
import importlib.util
import shutil
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from app import signing

IMAGE = b"\xe9" + bytes(range(256)) * 4096  # shaped like an ESP32 image: first byte 0xE9
SCRIPTS = Path(__file__).resolve().parents[3] / "clients" / "korvo-node" / "scripts"


def pem_private(key) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def pem_public(key) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.PEM,
                                         serialization.PublicFormat.SubjectPublicKeyInfo)


def sign(key, image: bytes = IMAGE) -> bytes:
    return key.sign(image, ec.ECDSA(hashes.SHA256()))


def b64url(der: bytes) -> str:
    return base64.urlsafe_b64encode(der).rstrip(b"=").decode()


@pytest.fixture
def key():
    return ec.generate_private_key(ec.SECP256R1())


@pytest.fixture
def hub_key(key, tmp_path, monkeypatch):
    """The hub configured with the developer's public key, as a file path."""
    path = tmp_path / "firmware-signing.pub.pem"
    path.write_bytes(pem_public(key))
    monkeypatch.setenv(signing.ENV_KEY, str(path))
    return signing.load_public_key()


@pytest.fixture
def no_hub_key(monkeypatch):
    monkeypatch.delenv(signing.ENV_KEY, raising=False)


@pytest.fixture
def script():
    """The signing half the upload script uses, loaded from the firmware tree."""
    path = SCRIPTS / "firmware_signing.py"
    if not path.exists():
        pytest.skip("clients/korvo-node is not in this checkout")
    spec = importlib.util.spec_from_file_location("firmware_signing", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---- good ------------------------------------------------------------------------


def test_a_good_signature_is_kept_as_standard_base64_for_the_ota_message(key, hub_key):
    der = sign(key)
    stored = signing.accept_upload(IMAGE, b64url(der))
    assert stored == base64.b64encode(der).decode()
    assert base64.b64decode(stored, validate=True) == der


def test_a_signature_is_carried_unchecked_when_the_hub_has_no_key(key, no_hub_key):
    der = sign(key)
    assert signing.accept_upload(IMAGE, b64url(der)) == base64.b64encode(der).decode()


def test_base64url_and_padded_base64_are_the_same_signature(key):
    der = sign(key)
    assert signing.decode(b64url(der)) == der
    assert signing.decode(base64.b64encode(der).decode()) == der


def test_what_the_upload_script_signs_verifies_on_the_hub(key, tmp_path, script, hub_key):
    priv = tmp_path / "firmware-signing.pem"
    priv.write_bytes(pem_private(key))
    sig = script.sign_for_upload(IMAGE, str(priv), str(tmp_path / "firmware-signing.pub.pem"))
    assert signing.accept_upload(IMAGE, sig) is not None


@pytest.mark.skipif(shutil.which("openssl") is None, reason="no openssl CLI")
def test_the_openssl_fallback_makes_a_signature_the_hub_accepts(key, tmp_path, script, hub_key,
                                                                 monkeypatch):
    priv = tmp_path / "firmware-signing.pem"
    priv.write_bytes(pem_private(key))
    # PlatformIO's Python usually has no `cryptography`: hide it from the script.
    monkeypatch.setattr(script, "_cryptography_private_key", lambda path: None)
    der = script.sign(IMAGE, str(priv))
    assert signing.accept_upload(IMAGE, b64url(der)) is not None
    assert script.public_der_of_private(str(priv)) == key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def test_the_key_id_matches_the_one_the_firmware_build_compiles_in(key, script, hub_key):
    der = key.public_key().public_bytes(serialization.Encoding.DER,
                                        serialization.PublicFormat.SubjectPublicKeyInfo)
    script.check_p256_public_key(der)
    assert signing.key_id(hub_key) == script.key_id(der)
    assert len(signing.key_id(hub_key)) == 16


def test_the_hub_key_may_be_given_inline_rather_than_as_a_path(key, monkeypatch):
    monkeypatch.setenv(signing.ENV_KEY, pem_public(key).decode())
    assert signing.accept_upload(IMAGE, b64url(sign(key))) is not None


# ---- bad -------------------------------------------------------------------------


def test_a_signature_of_another_image_is_refused(key, hub_key):
    with pytest.raises(signing.SignatureError, match="does not match"):
        signing.accept_upload(IMAGE + b"\x00", b64url(sign(key)))


def test_a_signature_by_another_key_is_refused(hub_key):
    other = ec.generate_private_key(ec.SECP256R1())
    with pytest.raises(signing.SignatureError, match="does not match"):
        signing.accept_upload(IMAGE, b64url(sign(other)))


def test_a_signature_with_one_bit_flipped_is_refused(key, hub_key):
    der = bytearray(sign(key))
    der[-1] ^= 1
    with pytest.raises(signing.SignatureError):
        signing.accept_upload(IMAGE, b64url(bytes(der)))


def _der(r: bytes, s: bytes) -> bytes:
    body = bytes([2, len(r)]) + r + bytes([2, len(s)]) + s
    return bytes([0x30, len(body)]) + body


@pytest.mark.parametrize("text", [
    "not base64 at all!",
    base64.b64encode(b"\x30\x06\x02\x01\x01\x02\x01").decode(),        # cut short
    base64.b64encode(b"\x00" * 64).decode(),                           # raw r||s, not DER
    base64.b64encode(_der(b"\x01" * 32, b"\x01" * 32) + b"\x00").decode(),  # trailing byte
    base64.b64encode(_der(b"\x80" + b"\x01" * 31, b"\x01" * 32)).decode(),  # negative r
    base64.b64encode(_der(b"\x00\x01" + b"\x01" * 30, b"\x01")).decode(),  # padded r
    base64.b64encode(_der(b"\x00", b"\x01" * 32)).decode(),            # r = 0
    base64.b64encode(_der(b"\x01" * 34, b"\x01" * 32)).decode(),       # r longer than P-256
    base64.b64encode(b"\x30" + bytes([200]) + b"\x00" * 200).decode(),  # far too long
    "   ",
])
def test_malformed_signatures_are_refused_even_without_a_hub_key(text, no_hub_key):
    with pytest.raises(signing.SignatureError):
        signing.accept_upload(IMAGE, text)


# ---- missing ---------------------------------------------------------------------


def test_an_unsigned_image_is_accepted_by_a_hub_without_a_key(no_hub_key):
    assert signing.accept_upload(IMAGE, None) is None
    assert signing.accept_upload(IMAGE, "") is None


def test_an_unsigned_image_is_refused_by_a_hub_with_a_key(hub_key):
    with pytest.raises(signing.SignatureError, match="unsigned"):
        signing.accept_upload(IMAGE, None)


def test_the_upload_script_will_not_send_unsigned_to_a_build_that_trusts_a_key(key, tmp_path,
                                                                                 script):
    pub = tmp_path / "firmware-signing.pub.pem"
    pub.write_bytes(pem_public(key))
    with pytest.raises(script.SigningError, match="no signing key"):
        script.sign_for_upload(IMAGE, str(tmp_path / "absent.pem"), str(pub))


def test_the_upload_script_uploads_unsigned_only_when_nothing_asks_for_a_signature(tmp_path,
                                                                                    script):
    assert script.sign_for_upload(IMAGE, str(tmp_path / "absent.pem"),
                                  str(tmp_path / "absent.pub.pem")) is None


def test_the_upload_script_refuses_a_private_key_that_is_not_the_builds(key, tmp_path, script):
    pub = tmp_path / "firmware-signing.pub.pem"
    pub.write_bytes(pem_public(key))
    wrong = tmp_path / "wrong.pem"
    wrong.write_bytes(pem_private(ec.generate_private_key(ec.SECP256R1())))
    with pytest.raises(script.SigningError, match="not the private half"):
        script.sign_for_upload(IMAGE, str(wrong), str(pub))


# ---- the hub's own key -----------------------------------------------------------


def test_a_hub_key_the_node_could_not_use_is_refused_when_loaded(tmp_path, monkeypatch):
    for bad in (ec.generate_private_key(ec.SECP384R1()),
                rsa.generate_private_key(public_exponent=65537, key_size=2048)):
        monkeypatch.setenv(signing.ENV_KEY, pem_public(bad).decode())
        with pytest.raises(signing.SignatureError, match="P-256"):
            signing.load_public_key()


def test_a_hub_key_path_that_does_not_exist_is_named_in_the_error(tmp_path, monkeypatch):
    monkeypatch.setenv(signing.ENV_KEY, str(tmp_path / "missing.pem"))
    with pytest.raises(signing.SignatureError, match="missing.pem"):
        signing.load_public_key()


def test_the_build_script_refuses_a_public_key_the_node_could_not_use(script):
    rsa_der = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key(
    ).public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    with pytest.raises(ValueError, match="P-256"):
        script.check_p256_public_key(rsa_der)


# ---- which nodes an update is started on -------------------------------------------


def test_a_node_built_without_a_key_takes_signed_and_unsigned_images():
    assert signing.skip_reason({"mic": {}}, None) is None
    assert signing.skip_reason({}, "c2ln") is None


def test_a_node_that_requires_signatures_is_not_sent_an_unsigned_image():
    assert "unsigned" in signing.skip_reason({"ota_key": "0123456789abcdef"}, None)


def test_a_node_that_trusts_another_key_is_not_sent_an_image_it_would_refuse(key, hub_key):
    reason = signing.skip_reason({"ota_key": "0123456789abcdef"}, "c2ln", hub_key)
    assert "0123456789abcdef" in reason and signing.key_id(hub_key) in reason


def test_a_node_that_trusts_the_hubs_key_is_updated(key, hub_key):
    assert signing.skip_reason({"ota_key": signing.key_id(hub_key)}, "c2ln", hub_key) is None


def test_the_hub_runs_without_cryptography_when_no_key_is_set(no_hub_key, monkeypatch):
    # Unsigned and shape-checked uploads must not need the package at all.
    monkeypatch.setitem(sys.modules, "cryptography", None)
    der = _der(b"\x01" * 32, b"\x02" * 32)
    assert signing.accept_upload(IMAGE, b64url(der)) == base64.b64encode(der).decode()
    assert signing.accept_upload(IMAGE, None) is None
