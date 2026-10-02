"""/ui/fetch transcribes as the person who asked, and no request out of here repeats an inbound header.

The gateway hands /ui/fetch a delegation token beside the identity assertion
(D64). It goes on to the gateway's internal listener with this service's own
key and nothing else of the inbound request's, and the gateway re-checks the
person live there. Everything this service sends anywhere is built from named
values (D65): the assertion is never among them. And the downloader child is
told a URL, a kind, a cap and a language, so it learns nothing about who is
asking either.
"""

from __future__ import annotations

import json

import pytest
from voice_common.identity import ASSERTION_HEADER, DELEGATION_HEADER

from conftest import BOB, fetched

URL = "https://media.example/instant-talk"


@pytest.fixture
def finished(client):
    """A link Alice resolved, committed and the downloader finished."""
    api, gateway, fetches = client()
    fetched(api, URL)
    return api, gateway, fetches


def test_fetch_sends_the_delegation_and_the_service_key_and_no_identity(
        finished, sign, calliope_gateway):
    api, gateway, _ = finished
    headers = sign()
    response = api.post("/ui/fetch", json={"token": URL}, headers=headers)
    assert response.status_code == 200, response.text

    [sent] = gateway.transcriptions()
    assert (sent.url.scheme, sent.url.host, sent.url.port) == ("http", "voice-gateway", 8081)
    assert sent.headers[DELEGATION_HEADER] == headers[DELEGATION_HEADER]
    assert sent.headers["authorization"] == f"Bearer {calliope_gateway.service_key}"
    assert ASSERTION_HEADER.lower() not in sent.headers
    assert "cookie" not in sent.headers


def test_fetch_without_a_delegation_downloads_nothing_and_sends_nothing(finished, sign):
    """This service holds no scope that can transcribe on its own, so without
    the person's token there is nothing to do -- and the 131 MB upload is not
    the place to find that out."""
    api, gateway, _ = finished
    headers = {**sign(), DELEGATION_HEADER: ""}
    response = api.post("/ui/fetch", json={"token": URL}, headers=headers)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "delegation_refused"
    assert not gateway.seen


def test_a_delegation_signed_for_someone_else_is_not_passed_on(finished, sign,
                                                               calliope_gateway):
    """identity.install keeps a delegation only when its `sub` is the request's own."""
    api, gateway, _ = finished
    headers = {**sign(), DELEGATION_HEADER: calliope_gateway.delegation(sub=BOB)}
    response = api.post("/ui/fetch", json={"token": URL}, headers=headers)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "delegation_refused"
    assert not gateway.seen


def test_a_delegation_is_passed_on_from_fetch_and_from_nowhere_else(finished, sign):
    """Every request in this suite carries one; only /ui/fetch spends it."""
    api, gateway, _ = finished
    api.get("/ui/progress", params={"token": URL})
    api.get("/ui/media", params={"token": URL})
    api.post("/ui/captions", json={"token": URL})
    assert not gateway.seen


def test_a_rotated_service_key_is_read_again_and_the_fetch_retried_once(
        finished, calliope_gateway):
    """The gateway rotated the key on the volume after this service read it.

    The 401 is about this service, not the person, so it must not reach the
    page as a 401 -- the page would send them to sign in. The gateway checks
    the key before it counts the delegation, so the retry costs no use.
    """
    api, gateway, _ = finished
    old = calliope_gateway.service_key
    assert api.post("/ui/fetch", json={"token": URL}).status_code == 200
    fresh = "calliope_svc_" + "R" * 36
    (calliope_gateway.directory / "service.key").write_text(fresh + "\n")
    gateway.key = fresh

    response = api.post("/ui/fetch", json={"token": URL})
    assert response.status_code == 200, response.text
    keys = [r.headers["authorization"] for r in gateway.transcriptions()]
    assert keys == [f"Bearer {old}", f"Bearer {old}", f"Bearer {fresh}"]


def test_a_refused_service_key_that_has_not_changed_is_a_503_not_a_401(finished):
    api, gateway, _ = finished
    gateway.key = "calliope_svc_" + "N" * 36
    response = api.post("/ui/fetch", json={"token": URL})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_key_refused"
    assert "www-authenticate" not in response.headers
    assert len(gateway.transcriptions()) == 1, "retried with the same refused key"


def test_a_fresh_key_the_gateway_also_refuses_is_a_503_not_a_401(
        finished, calliope_gateway):
    """The key on the volume changed, but not to the one the listener holds.

    Still this service's fault and not the person's, so the page must not be
    told to send them to sign in.
    """
    api, gateway, _ = finished
    old = calliope_gateway.service_key
    assert api.post("/ui/fetch", json={"token": URL}).status_code == 200
    fresh = "calliope_svc_" + "F" * 36
    (calliope_gateway.directory / "service.key").write_text(fresh + "\n")
    gateway.key = "calliope_svc_" + "N" * 36

    response = api.post("/ui/fetch", json={"token": URL})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_key_refused"
    assert "www-authenticate" not in response.headers
    keys = [r.headers["authorization"] for r in gateway.transcriptions()]
    assert keys == [f"Bearer {old}", f"Bearer {old}", f"Bearer {fresh}"], \
        "not retried exactly once with the key read again"


def test_fetch_before_the_service_key_exists_is_a_503(finished, calliope_gateway):
    """The gateway writes the key within seconds of starting (§2.4); until then
    the honest answer is to try again, and nothing is downloaded meanwhile."""
    api, gateway, _ = finished
    (calliope_gateway.directory / "service.key").unlink()
    response = api.post("/ui/fetch", json={"token": URL})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "not_ready"
    assert not gateway.seen


def test_the_multipart_boundary_cannot_be_predicted(finished):
    """The file is somebody else's bytes. A fixed boundary written into a
    video's audio track would end the part early and open a field of the
    uploader's choosing; a random one per request cannot be written in advance."""
    api, gateway, _ = finished
    api.post("/ui/fetch", json={"token": URL})
    api.post("/ui/fetch", json={"token": URL})
    first, second = (r.headers["content-type"] for r in gateway.transcriptions())
    assert first != second
    assert all("boundary=calliope-" in value for value in (first, second))


def test_a_title_that_would_break_the_frame_is_scrubbed_from_the_filename(client):
    api, gateway, _ = client()
    from app import main
    from conftest import finished_job
    finished_job(main, URL, b"RIFF", ".opus", facts={"title": 'A "quoted"\r\ntitle\\'})
    api.post("/ui/fetch", json={"token": URL})
    [sent] = gateway.transcriptions()
    assert b'filename="A _quoted_title.opus"' in sent.content


def test_a_glossary_that_would_break_the_frame_is_refused(finished):
    api, gateway, _ = finished
    response = api.post("/ui/fetch", json={"token": URL},
                        params={"glossary": 'tech\r\n\r\nx'})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_glossary"
    assert not gateway.seen


def test_a_glossary_name_still_reaches_stt_for_it_to_judge(finished):
    """Which profiles exist, and which this person may see, is stt's to say."""
    api, gateway, _ = finished
    api.post("/ui/fetch", json={"token": URL}, params={"glossary": "home-assistant"})
    [sent] = gateway.transcriptions()
    assert b'name="glossary"\r\n\r\nhome-assistant' in sent.content


def test_the_downloader_is_told_nothing_about_who_is_asking(client, sign,
                                                           calliope_gateway):
    """The child's argv is a mode, a kind, a cap, a language and the link, and
    its environment is four fixed variables: no sub, no assertion, no
    delegation, no cookie and no key, and no path to the volume that holds one."""
    api, _, fetches = client()
    headers = {**sign(), "Cookie": "__Host-calliope_session=abc",
               "Authorization": "Bearer calliope_" + "x" * 36,
               "X-Calliope-Anything": "1"}
    fetched(api, URL, headers=headers)
    api.post("/ui/fetch", json={"token": URL}, headers=headers)
    [call] = fetches.calls()
    told = json.dumps(call)
    for secret in ("u_aaaaaaaaaaaaaaaa", headers[ASSERTION_HEADER],
                   headers[DELEGATION_HEADER], "calliope_session", "Bearer",
                   calliope_gateway.service_key, str(calliope_gateway.directory)):
        assert secret not in told, secret
    assert call["argv"] == ["fetch", "audio", str(500 * 2**20), "-", "--", URL]
