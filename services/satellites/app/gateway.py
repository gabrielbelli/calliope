"""The hub's own credential, and the one place it is ever sent (D6, D7).

    CREDENTIALS                         /run/calliope: identity.pub and service.key
    await gateway.request(client, "POST", url, ...)   the key added for the gateway only

THE SERVICE KEY GOES TO THE GATEWAY'S INTERNAL LISTENER AND NOWHERE ELSE.
Speech-to-text, speech and the secret store are all reached through
http://voice-gateway:8081, which checks the key and forwards the call with an
assertion that says svc:satellites. request() adds the key only when the URL is
that listener, by scheme, host and port. SATELLITES_STT_URL and
SATELLITES_TTS_URL can still name anything; one that names another host is
sent no key, so a typo or a hostile value can never carry the hub's credential
anywhere else.

A 401 FROM THE GATEWAY MEANS THE KEY MAY HAVE BEEN ROTATED (§2.4). The key is
read again from its file, and the request is made once more, only if the file
now holds a different key: a gateway that refuses the current one gets one
request, not a loop.

The same Credentials object verifies the assertions on every request this
service answers (identity.install in main.py), so the volume is read by one
reader.
"""

from __future__ import annotations

import httpx
from voice_common import identity

CREDENTIALS = identity.Credentials()

_INTERNAL = httpx.URL(identity.GATEWAY_INTERNAL)


class NotReady(Exception):
    """The gateway has not written this service's key yet."""


def is_gateway(url: str | httpx.URL) -> bool:
    """Is `url` on the gateway's internal listener? httpx leaves the port None
    when it is the scheme's default, on both sides, so the comparison is
    exact either way."""
    try:
        target = httpx.URL(url)
    except httpx.InvalidURL:
        return False
    return (target.scheme, target.host, target.port) == (_INTERNAL.scheme, _INTERNAL.host,
                                                         _INTERNAL.port)


def _headers(key: str, given: dict[str, str] | None) -> dict[str, str]:
    return {**(given or {}), **identity.outbound_headers(authorization=f"Bearer {key}")}


async def request(client: httpx.AsyncClient, method: str, url: str, *,
                  headers: dict[str, str] | None = None, **kwargs) -> httpx.Response:
    """`client.request`, with the service key when `url` is the gateway's
    internal listener. Raises NotReady while the key file is missing."""
    if not is_gateway(url):
        return await client.request(method, url, headers=headers, **kwargs)
    key = CREDENTIALS.service_key()
    if key is None:
        raise NotReady("the gateway has not written this service's key yet "
                       f"({identity.SERVICE_KEY_FILE} in {CREDENTIALS.directory})")
    answer = await client.request(method, url, headers=_headers(key, headers), **kwargs)
    if answer.status_code == 401:
        again = CREDENTIALS.reload_service_key()
        if again is not None and again != key:
            await answer.aclose()
            answer = await client.request(method, url, headers=_headers(again, headers), **kwargs)
    return answer
