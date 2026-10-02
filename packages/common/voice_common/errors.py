"""The OpenAI error envelope: all four fields, one argument order, every /v1 path.

openai-python reads `error.message` and shows it to the caller. Handed
anything else — FastAPI's own `{"detail": ...}` included — it reports a bare
"unknown error", which tells the caller nothing about the field it forgot.
Every service here claims to answer OpenAI's clients, so every /v1 error it
emits has to be in this shape.

There were four incompatible copies of that shape in the estate, and then
three more written to patch around this module's own gaps:

  stt-stack/app/auth.py        ApiError + its exception handler
  tts-stack/app/openai_api.py  error_response(status, message, type_, code)
  tts-long/app/main.py         _error(status, message, code, type_)
  tts-long/app/auth.py         unauthorised(), a hardcoded 401 body

Note the middle two: `code` and `type_` are swapped between sibling repos that
are otherwise copy-pasted from one another. Nothing catches that — both are
strings, both produce a valid-looking envelope, and the only symptom is a
client reading "invalid_api_key" out of the field that should name a category.
So here the two are keyword-only. A positional call cannot compile the swap in,
and a copied line from either repo fails loudly at the first test rather than
quietly on the wire. `param` is keyword-only for the same reason: a third
string in the same position would be a third chance to make that mistake.

Both shapes are provided because both call sites are real: `raise ApiError`
from a route handler, `return error_response(...)` from middleware, where
there is nothing above to catch a raise. The ApiError handler renders through
error_response, so the two cannot drift apart.

**`param` is the field this module used to omit.** OpenAI's `Error` schema
requires ALL FOUR of `type`, `message`, `param` and `code`, and `param` and
`code` are required-but-NULLABLE — present as JSON null, not absent. This
module built three keys, so `param` was missing from every error every service
had ever produced, and a client reading `err.param` to find out which field it
got wrong read `None` whether or not the server knew the answer. It usually
did: the validation handler already computed a field name to put in the
message string and then threw it away.

**404 and 405 under /v1 used to escape the envelope entirely.** install_errors
registered handlers for ApiError and RequestValidationError and not for
StarletteHTTPException, so `GET /v1/audio/speech` answered
`{"detail":"Method Not Allowed"}` and `POST /v1/audio/transcriptions` answered
`{"detail":"Not Found"}` — a shape openai-python reads no message off.

**An unhandled exception under /v1 used to be a plain-text 500.** Same silence,
at the moment the caller most needs a sentence.

Three services each vendored their own fix for those three gaps, because
voice-common was pinned by tarball SHA and a change here was not a change in
their images until the pin moved. That pin is gone — packages/common is a path
dependency now — and the three copies had already disagreed with each other on
the wire before anyone could reconcile them:

  * a 405 lost Starlette's `Allow` header in one copy, which RFC 9110 §15.5.6
    requires on every 405, because that copy dropped `exc.headers`;
  * a 405 was worded "Invalid method (…)" in one copy and "Invalid URL (…)" in
    the other two;
  * a 5xx HTTPException was typed `invalid_request_error` in two copies, which
    tells a client to fix a request that was fine;
  * only one copy collapsed pydantic's per-branch union errors, so the other
    two would have named a parameter `voice.str`, which is not a parameter.

The behaviour below is the correct one from each, not the majority one.

THE BOUNDARY IS /v1. Native routes keep FastAPI's `{"detail": ...}` and its
422 throughout, and an unhandled error on one is still a plain-text 500. The
one change on that side is that the 422 no longer repeats what was sent
(quiet_validation_response), because a refused body may hold a password.
Those routes have clients — bench/bench.py, the integration suite, Open WebUI
— and reshaping them to tidy up a compatibility layer those clients never
touch is how a working deployment breaks during a refactor. `v1_path` draws
that line and every handler here asks it first.

The RequestValidationError handler answers **400**, which fixes a genuine
wire-contract drift: stt-stack answered 422 to a bad /v1 body where the other
two — and OpenAI itself — answer 400, so a client written against the real API
mishandled the one service in the estate that claims to imitate it most
closely.

**Authentication and authorisation errors have one constructor each** (D55),
at the bottom of this file: the gateway's middleware, its routes and every
backend refuse with the same code, the same sentence and the same challenge
header, and a client branches on `code` alone.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping

from fastapi import FastAPI, Request, Response
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .scopes import SCOPE

__all__ = ["ApiError", "error_response", "http_error_response",
           "install_errors", "quiet_validation_response", "render", "v1_path",
           "validation_error_response",
           "unauthenticated", "invalid_api_key", "api_key_expired",
           "insufficient_scope", "session_required", "csrf", "wrong_host",
           "scope_not_grantable", "json_required", "locked"]

log = logging.getLogger("voice_common.errors")

# Codes openai-python surfaces as `.code` on the exception it raises. Kept
# distinct so a caller can tell "you left out `input`" from "9 is not a valid
# exaggeration" without parsing the message. `extra_forbidden` is here because
# an unknown property is a 400 in OpenAI's own API ("Unrecognized request
# argument supplied: stream"), and telling a client which field it invented is
# the whole point of `param`.
_VALIDATION_CODES = {
    "missing": "missing_required_parameter",
    "extra_forbidden": "unknown_parameter",
}

# The scalar names pydantic uses to tag a union branch in `loc`. A branch that
# is a model is tagged with the model's class name instead, which is why
# `_is_branch_tag` also accepts anything carrying an uppercase letter: every
# field name in an OpenAI body is lower-case snake_case, and every pydantic
# model in this estate is CamelCase, so the two sets cannot collide.
_SCALAR_TAGS = frozenset({"str", "int", "float", "bool", "bytes", "none",
                          "list", "dict", "tuple", "set", "literal"})


def error_response(status: int, message: str, *,
                   type_: str = "invalid_request_error",
                   code: str | None = None,
                   param: str | None = None,
                   headers: dict[str, str] | None = None,
                   extra: Mapping[str, str | None] | None = None) -> JSONResponse:
    """An error in OpenAI's envelope. `type_`, `code` and `param` are keyword-only.

    Keyword-only on purpose: see the module docstring. Two sibling repos passed
    `type_` and `code` positionally in opposite orders, and the resulting body
    is valid JSON either way, so the mistake reaches production intact.

    All four keys are always present. `param` and `code` are serialised even
    when null, because the schema marks them required-but-nullable: a client
    generated from it may read the key rather than test for its presence. The
    key order is OpenAI's own — message, type, param, code — so a body logged
    here and a body logged from the real API diff cleanly.

    `headers` is for the two the specification enumerates on this path and no
    others: Retry-After on a 503 or a 429, which an OpenAI client's backoff
    reads, and WWW-Authenticate on a 401. It is a mapping rather than a string,
    so it carries no risk of being confused with the three above.

    `extra` adds keys BESIDE `error`, never inside it: the four fields are
    exactly what the schema names, and a client generated from it may reject a
    fifth. The 503 `locked` body is the one user, for its `reason` and
    `variable`.

    Only /v1 routes should use this. The native routes' `{"detail": ...}`
    bodies are part of a contract that already has clients, and reshaping them
    would break callers to tidy up a compatibility layer they never touch.
    """
    content: dict[str, object] = {"error": {"message": message,
                                            "type": type_,
                                            "param": param,
                                            "code": code}}
    if extra:
        if "error" in extra:
            raise ValueError("extra must not replace the error object")
        content.update(extra)
    return JSONResponse(status_code=status, content=content, headers=headers)


class ApiError(Exception):
    """An error rendered in OpenAI's envelope instead of FastAPI's default.

    For route handlers, where raising is the natural control flow. Middleware
    has nothing above it to catch a raise and returns error_response instead;
    the handler installed below renders this THROUGH error_response so the two
    paths cannot produce different bodies.

    `headers` exists for the case that needs it: Retry-After on a 429 is the
    header the specification enumerates for this path, and the one an OpenAI
    client's backoff reads. It was carried by only one of the three vendored
    copies, and services/stt raises it from `_busy()`.
    """

    def __init__(self, status: int, message: str, *,
                 type_: str = "invalid_request_error",
                 code: str | None = None,
                 param: str | None = None,
                 headers: dict[str, str] | None = None,
                 extra: Mapping[str, str | None] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.type_ = type_
        self.code = code
        self.param = param
        self.headers = headers or {}
        self.extra = dict(extra or {})


def render(exc: ApiError) -> JSONResponse:
    """The response for an ApiError, from a handler or from middleware alike.

    The exception handler below calls this, and so does any middleware that
    refuses a request before routing, so a raised 401 and a returned one
    cannot differ by a byte.
    """
    response = error_response(exc.status, exc.message, type_=exc.type_,
                              code=exc.code, param=exc.param,
                              headers=exc.headers, extra=exc.extra)
    if exc.status == 401:
        # RFC 9110 wants a challenge on a 401. OpenAI's own API omits it;
        # sending it costs nothing and keeps generic HTTP clients honest. A
        # more specific challenge the error already carries is kept.
        response.headers.setdefault("WWW-Authenticate", "Bearer")
    return response


def v1_path(path: str) -> bool:
    """Is this path part of the OpenAI compatibility surface?

    `/v1` exactly, or anything below it. Written out rather than a bare
    startswith("/v1") because that also matches a future `/v1beta` or
    `/v1x`, and reshaping an unrelated route's errors is the sort of thing
    nobody notices until a client does.
    """
    return path == "/v1" or path.startswith("/v1/")


def _is_branch_tag(part: object) -> bool:
    return isinstance(part, str) and (part in _SCALAR_TAGS or part.lower() != part)


def _union_branches(errors: list[dict]) -> tuple[str, list[str]] | None:
    """(field, one message per branch) if `errors` are a union's, else None.

    A field declared as a union of shapes is reported by pydantic as ONE ERROR
    PER BRANCH, each carrying the branch's type name as an extra `loc` element:
    `voice` given `{"id": "x", "typo": 1}` produces `("body", "voice", "str")`
    and `("body", "voice", "CustomVoice", "typo")`. Reading the first error the
    way every other case is read named the parameter `voice.str`, which is not
    a parameter and is not something a client can act on — so the branches are
    collapsed back onto the field they all belong to, and every branch's
    complaint is kept, because a caller who mistyped a key inside the object
    needs to be told about the object form and not only about the string one.
    """
    first = errors[0]["loc"]
    depth = next((i for i, part in enumerate(first) if _is_branch_tag(part)), None)
    if depth is None or depth < 2:
        # depth 0 is "body" and depth 1 is the field itself; a tag can only
        # appear after the field it belongs to.
        return None
    prefix = tuple(first[:depth])
    branch_errors = [e for e in errors
                     if tuple(e["loc"][:depth]) == prefix
                     and len(e["loc"]) > depth and _is_branch_tag(e["loc"][depth])]
    if len(branch_errors) < 2:
        return None

    field = ".".join(str(part) for part in prefix[1:])
    messages: list[str] = []
    for error in branch_errors:
        # Anything past the tag is a path INSIDE that branch — `typo` for the
        # extra key above — and naming it is what makes the message actionable.
        inside = ".".join(str(part) for part in error["loc"][depth + 1:])
        message = f"{inside}: {error['msg']}" if inside else error["msg"]
        if message not in messages:
            messages.append(message)
    return field, messages


def validation_error_response(exc: RequestValidationError) -> Response:
    """A body pydantic rejected, in the envelope, with the field named in `param`.

    Split out from the handler so a service that wants the shaping without the
    registration — or wants to wrap it — does not have to reimplement it.
    """
    errors = exc.errors()
    if not errors:
        # Nothing pydantic could locate. Say so rather than guess at it.
        return error_response(400, "request body is not valid",
                              code="invalid_value")

    first = errors[0]
    if first["type"] == "json_invalid":
        # loc is ("body", <character offset>) here, and naming a parameter
        # "12" is worse than naming none at all.
        return error_response(400, "request body is not valid JSON",
                              code="invalid_value")

    branches = _union_branches(errors)
    if branches:
        # A field declared as a union of shapes — `voice`, which OpenAI's
        # VoiceIdsOrCustomVoice makes anyOf[string, {id}]. See _union_branches:
        # the naive first-error reading produced `param: "voice.str"`.
        field, messages = branches
        return error_response(400, f"{field}: " + " or ".join(messages),
                              code="invalid_value", param=field)

    # loc[0] is always "body"; the rest names the field, with ints for list
    # indices — segments.0.text.
    field = ".".join(str(part) for part in first["loc"][1:]) or None
    if first["type"] == "extra_forbidden":
        # Worded as OpenAI words it, because a client that greps its own logs
        # for this sentence is a client that has met the real API.
        return error_response(400,
                              f"Unrecognized request argument supplied: {field}",
                              code="unknown_parameter", param=field)
    return error_response(
        400, f"{field or 'body'}: {first['msg']}",
        code=_VALIDATION_CODES.get(first["type"], "invalid_value"),
        param=field)


def quiet_validation_response(exc: RequestValidationError) -> Response:
    """A refused native body answered with what was wrong, never with what was sent.

    FastAPI's own 422 carries pydantic's `input` for every error, which is the
    rejected value itself, with `ctx` and `url` beside it. The hub measured it
    (POST /satellites/ha/pipelines with a Home Assistant token pasted into the
    wrong field answered 422 with the token in the body), and a login or a
    password change must never do the same with a password. So every error is
    reduced to {type, loc, msg}: pydantic v2's `msg` says what was expected,
    not what arrived. The shape is still FastAPI's, so a client reading
    `detail[].loc` and `detail[].msg` sees no difference.
    """
    return JSONResponse(status_code=422, content={"detail": [
        {"type": error.get("type"), "loc": list(error.get("loc") or ()),
         "msg": error.get("msg")}
        for error in exc.errors()]})


def http_error_response(request: Request, exc: StarletteHTTPException, *,
                        unknown_url_hint: str | None = None) -> Response:
    """404, 405 and every other bare HTTPException, in OpenAI's envelope.

    The 404 and 405 messages mirror what api.openai.com answers an unrouted
    request with — "Invalid URL (POST /v1/nope)" — because that is the sentence
    openai-python will show, and a client comparing this service against the
    real one should not have to learn a second wording. One vendored copy said
    "Invalid method (…)" on a 405 instead; the wording is unified here and the
    two conditions stay distinguishable through `code`, which is the field a
    client branches on.

    `unknown_url_hint` is appended to the 404 message. It exists for
    services/gateway, whose 404 is the one in the estate with something useful
    to add: it routes a fixed table of paths and can name where that table is
    published.

    Callers on the native side of the boundary should not reach this. It does
    not check the path itself, so a service can use it for a route of its own
    choosing — see the gateway, which answers every path in this envelope by
    an older decision of its own.
    """
    if exc.status_code == 404:
        message = f"Invalid URL ({request.method} {request.url.path})"
        if unknown_url_hint:
            message = f"{message}. {unknown_url_hint}"
        code: str | None = "unknown_url"
    elif exc.status_code == 405:
        message = f"Invalid URL ({request.method} {request.url.path})"
        # Distinct from the 404's, because the two share a message wording and
        # `code` is then the only machine-readable thing telling them apart.
        code = "method_not_allowed"
    else:
        message = str(exc.detail)
        code = None

    response = error_response(
        exc.status_code, message,
        # A 5xx typed `invalid_request_error` tells the client to fix a request
        # that was fine. Two of the three vendored copies did exactly that.
        type_="server_error" if exc.status_code >= 500 else "invalid_request_error",
        code=code)
    # .items(), not the dict: a 405 carries {"Allow": "POST"} and iterating the
    # mapping itself unpacks the KEY, which is a 500. One vendored copy dropped
    # these headers entirely, so its 405 had no Allow — which RFC 9110 §15.5.6
    # requires on every one of them.
    for name, value in (getattr(exc, "headers", None) or {}).items():
        response.headers[name] = value
    return response


def install_errors(app: FastAPI) -> None:
    """Register the four handlers on the app. Call once, before serving.

    Order against identity.install does not matter: a middleware that refuses
    before routing renders through `render`, the same function the ApiError
    handler below uses, so its body is the same shape as every other error.
    """

    @app.exception_handler(ApiError)
    async def _render(request: Request, exc: ApiError) -> Response:
        del request
        return render(exc)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request,
                          exc: RequestValidationError) -> Response:
        """Put a rejected body into the OpenAI envelope, but only under /v1.

        A body pydantic rejects never reaches the handler, so every envelope
        that handler is careful to build was bypassed and the client got
        FastAPI's `{"detail": [...]}` with a 422. openai-python reads no
        message off that shape and raises a bare APIStatusError, which is the
        same silence a missing `input` deserved a sentence for.

        The native routes keep FastAPI's `{"detail": [...]}` and its 422,
        because something out there already parses `detail` on the routes
        that are not /v1 — but quietly: see quiet_validation_response.
        """
        if not v1_path(request.url.path):
            return quiet_validation_response(exc)
        return validation_error_response(exc)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> Response:
        """An unrouted path or a wrong method under /v1, in the envelope.

        Native routes keep FastAPI's own renderer: `{"detail": ...}` there is
        load-bearing for clients that predate this module.
        """
        if not v1_path(request.url.path):
            return await http_exception_handler(request, exc)
        return http_error_response(request, exc)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> Response:
        """An unhandled failure under /v1 as an envelope, not as bare text.

        Logged at exception level either way: this handler replaces Starlette's
        default, which is where the traceback used to come from. Starlette's
        ServerErrorMiddleware still re-raises afterwards, so a test running
        with raise_server_exceptions=True sees the original exception.

        The native side keeps Starlette's own plain-text body byte for byte.
        """
        log.exception("unhandled error on %s", request.url.path)
        if not v1_path(request.url.path):
            return Response("Internal Server Error", status_code=500,
                            media_type="text/plain")
        return error_response(500, f"internal error: {exc}",
                              type_="server_error", code="internal_error")


# ── Authentication and authorisation (D55) ────────────────────────────────────
#
# Each returns an ApiError: a route handler raises it, a middleware renders it
# with `render`. One constructor per code, so the wording and the challenge
# cannot differ between the gateway and a backend.

_REASON = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_VARIABLE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


def unauthenticated(message: str = "Authentication required: sign in, or send "
                                   "an API key as 'Authorization: Bearer <key>'."
                    ) -> ApiError:
    """401: no credential at all, or none this service can verify."""
    return ApiError(401, message, code="unauthenticated",
                    headers={"WWW-Authenticate": "Bearer"})


def invalid_api_key() -> ApiError:
    """401: a key was sent and it is not one. RFC 6750 names this invalid_token."""
    return ApiError(401, "Incorrect API key provided.", code="invalid_api_key",
                    headers={"WWW-Authenticate": 'Bearer error="invalid_token"'})


def api_key_expired() -> ApiError:
    """401: a real key past its expiry. Distinct, so a client can tell the user why."""
    return ApiError(401, "This API key has expired. Create a new one under "
                         "Account › API keys.", code="api_key_expired",
                    headers={"WWW-Authenticate": 'Bearer error="invalid_token"'})


def insufficient_scope(scopes: Iterable[str]) -> ApiError:
    """403 naming every missing scope, in the body and in the RFC 6750 challenge.

    The Home Assistant integration reads the challenge to tell its user which
    preset to create (§5.1), so the list is machine-readable there: scopes are
    space-separated, and the grammar admits no quote or backslash to escape.
    """
    needed = sorted(set(scopes))
    if not needed or not all(SCOPE.fullmatch(scope) for scope in needed):
        raise ValueError("insufficient_scope needs one or more well-formed scopes")
    listed = " ".join(needed)
    return ApiError(403, f"This credential lacks the scope it needs: {listed}.",
                    code="insufficient_scope",
                    headers={"WWW-Authenticate":
                             f'Bearer error="insufficient_scope", scope="{listed}"'})


def session_required() -> ApiError:
    """403: a session-only route (D60) asked with an API key, whatever it holds."""
    return ApiError(403, "This needs a signed-in session; an API key cannot do it.",
                    code="session_required")


def csrf() -> ApiError:
    """403: a cross-site or unverifiable browser request (D14, D15)."""
    return ApiError(403, "Cross-site request refused. Reload the page and try again.",
                    code="csrf")


def wrong_host() -> ApiError:
    """403: a cookie login on a host name other than the public origin (D61)."""
    return ApiError(403, "Sign in at Calliope's own address, not this one.",
                    code="wrong_host")


# How many refused scopes the 400 names before it only counts the rest, and
# the longest string it will name. Far above any real request (the longest
# scope is 20 characters) and far below one that makes the reply its echo.
MAX_NAMED_SCOPES = 10
MAX_NAMED_SCOPE_LENGTH = 64


def scope_not_grantable(scopes: Iterable[object]) -> ApiError:
    """400: a key asked for scopes it may never hold, or the owner's role lacks.

    The scopes are named because the caller chose them; the request is the
    caller's own and naming them discloses nothing. Only strings with a
    scope's shape are named, and only the first ten, so the message can never
    carry markup or grow with the request: anything else is counted, never
    repeated (recheck L9).
    """
    named: set[str] = set()
    unrecognised = 0
    for scope in scopes:
        if (isinstance(scope, str) and len(scope) <= MAX_NAMED_SCOPE_LENGTH
                and SCOPE.fullmatch(scope)):
            named.add(scope)
        else:
            unrecognised += 1
    parts = sorted(named)[:MAX_NAMED_SCOPES]
    if len(named) > len(parts):
        parts.append(f"{len(named) - len(parts)} more")
    if unrecognised:
        parts.append(f"{unrecognised} unrecognised "
                     f"{'scope' if unrecognised == 1 else 'scopes'}")
    message = (f"An API key cannot hold: {', '.join(parts)}." if parts
               else "An API key cannot hold the scopes requested.")
    return ApiError(400, message, code="scope_not_grantable", param="scopes")


def json_required() -> ApiError:
    """415: login takes JSON only, so a cross-site HTML form cannot submit it (D61)."""
    return ApiError(415, "Send this request as application/json.",
                    code="json_required")


def locked(reason: str, variable: str | None = None) -> ApiError:
    """503: the gateway is in locked mode (D63), with what to fix beside `error`.

    `variable` is the NAME of the setting to fix, never its value: a weak
    bootstrap password or a stale key list must not be read back from a
    public error page. Both are checked against a name's shape, so a value
    passed by mistake raises here instead of reaching a client.
    """
    if not _REASON.fullmatch(reason):
        raise ValueError("a locked reason is a lower-case code")
    if variable is not None and not _VARIABLE.fullmatch(variable):
        raise ValueError("a locked variable is an environment variable's name")
    message = f"Calliope is locked ({reason})."
    if variable:
        message += f" Fix {variable} and restart the gateway."
    return ApiError(503, message, type_="server_error", code="locked",
                    extra={"reason": reason, "variable": variable})
