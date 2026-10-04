"""Every route's requirement, bound to the route itself (D3, D48, recheck M-1).

    rule("satellites:admin")                       a scope, Bearer or session
    rule("users:manage", step_up=True)             raises: step-up needs session_only
    rule("users:manage", session_only=True, step_up=True)
    PUBLIC                                         the five public rows (D49)

**A route without a rule cannot exist.** `bind(app, rules)` runs at import,
after every route is added, and raises for any route the table does not name.
A row that needs step-up must be session-only, and a row whose scope is
session-only must say so, or `rule()` raises: a key can never be a permanent
step-up bypass (D60).

**The requirement is found by the router's own match** (recheck M-1).
`resolve` walks `app.router.routes` in declaration order and takes the first
full match, exactly as Starlette will when it dispatches the request. A
separate regex or prefix table would be a second matcher, and the first time
it disagreed with the router, `GET /satellites/telemetry` would be checked as
`GET /satellites/{nid}` and a `satellites:read` key would get the telemetry
settings. HEAD is checked as its GET row.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from starlette.routing import BaseRoute, Match, WebSocketRoute
from starlette.types import Scope
from voice_common.scopes import SCOPES, SESSION_ONLY

RULE = "calliope_rule"
WEBSOCKET = "WS"


@dataclass(frozen=True)
class Reserved:
    """A requirement that depends on a path parameter's value.

    /glossaries/{name} when name is `home-assistant`: the reserved profile is
    reached with `glossaries:ha` or the `:all` form, never with `:own` (D33,
    D34). Any ONE scope in `any_of` is enough.
    """

    param: str
    value: str
    any_of: frozenset[str]


@dataclass(frozen=True)
class Rule:
    scopes: frozenset[str] = frozenset()   # every one is required
    session_only: bool = False
    step_up: bool = False
    restricted: bool = False               # reachable from a must-change session (D21)
    public: bool = False
    reserved: Reserved | None = None
    audit: str | None = None               # the audit action written when it is used

    def required(self, path_params: Mapping[str, Any]) -> tuple[frozenset[str], frozenset[str]]:
        """(every scope required, any-of alternatives) for this request's parameters."""
        if self.reserved and path_params.get(self.reserved.param) == self.reserved.value:
            return frozenset(), self.reserved.any_of
        return self.scopes, frozenset()


def rule(*scopes: str, session_only: bool = False, step_up: bool = False,
         restricted: bool = False, reserved: Reserved | None = None,
         audit: str | None = None) -> Rule:
    unknown = [s for s in scopes if s not in SCOPES]
    if unknown:
        raise ValueError(f"unknown scopes {unknown}")
    if step_up and not session_only:
        raise ValueError("a step-up row must be session_only (D60)")
    if set(scopes) & SESSION_ONLY and not session_only:
        raise ValueError(f"{sorted(set(scopes) & SESSION_ONLY)} is session-only, so the "
                         "row must be session_only (D60)")
    return Rule(scopes=frozenset(scopes), session_only=session_only, step_up=step_up,
                restricted=restricted, reserved=reserved, audit=audit)


PUBLIC = Rule(public=True)


def _keys(route: BaseRoute) -> list[tuple[str, str]]:
    path = getattr(route, "path", None)
    if isinstance(route, WebSocketRoute):
        return [(WEBSOCKET, path)]
    methods = getattr(route, "methods", None) or ()
    return [(method, path) for method in sorted(methods) if method != "HEAD"]


def bind(app: FastAPI, rules: Mapping[tuple[str, str], Rule]) -> None:
    """Attach a rule to every route of `app`. Raises for a route the table does not name."""
    for route in app.router.routes:
        keys = _keys(route)
        found = {rules[key] for key in keys if key in rules}
        missing = [key for key in keys if key not in rules]
        if missing or not keys:
            raise RuntimeError(
                f"{missing or getattr(route, 'path', route)} has no rule: add it to the "
                "route table with a scope, or to PUBLIC (D48)")
        if len(found) != 1:
            raise RuntimeError(f"{keys} carries more than one rule")
        setattr(route, RULE, found.pop())


def rules_of(app: FastAPI) -> Iterable[tuple[str, str, Rule]]:
    for route in app.router.routes:
        for method, path in _keys(route):
            yield method, path, getattr(route, RULE)


def resolve(app: FastAPI, scope: Scope) -> tuple[BaseRoute | None, Rule | None, dict[str, Any]]:
    """The route Starlette will dispatch this request to, its rule and its path parameters.

    None when no route matches in full: the router answers 404 or 405, and
    the caller still requires a credential first, so an unknown path tells an
    anonymous client nothing.
    """
    probe = scope
    if scope["type"] == "http" and scope["method"] == "HEAD":
        probe = {**scope, "method": "GET"}
    for route in app.router.routes:
        match, child = route.matches(probe)
        if match == Match.FULL:
            return route, getattr(route, RULE, None), dict(child.get("path_params", {}))
    return None, None, {}
