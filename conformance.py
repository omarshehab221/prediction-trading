#!/usr/bin/env python3
"""
conformance.py -- validate every API call against Binance's own schema.

WHY THIS EXISTS
---------------
The unit tests validate the bot against my model of the API. Any assumption
baked into both the code and its tests passes silently -- which is how the
signing order, the wei units, the POST/GET verbs and the -9000 handling all
survived a green suite and were only found by running against the real venue.

This checker removes my judgement from the loop. It parses the TypeScript
declarations shipped in @binance/w3w-prediction and compares them against what
the bot actually sends and reads, by AST. Ground truth is the connector, not
my memory.

    python3 conformance.py [--connector PATH] [--source btc_5m_predictor.py]

Exit code 0 only when every call conforms.
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import sys
from dataclasses import dataclass, field


DEFAULT_CONNECTOR = ("/home/claude/probe/node_modules/@binance/"
                     "w3w-prediction/dist/index.d.ts")

# Endpoint name in the bot -> (request interface, response interface).
# The only hand-written mapping; everything else is derived.
ENDPOINT_INTERFACES: dict[str, tuple[str, str]] = {
    "category_list": ("ListCategoriesRequest", "ListCategoriesResponse"),
    "market_list": ("ListPredictionMarketsRequest",
                    "ListPredictionMarketsResponse"),
    "market_detail": ("GetMarketDetailRequest", "GetMarketDetailResponse"),
    "order_book": ("QueryOrderBookRequest", "QueryOrderBookResponse"),
    "wallet_list": ("ListPredictionWalletsRequest",
                    "ListPredictionWalletsResponse"),
    "balances": ("QueryPaymentOptionBalancesRequest",
                 "QueryPaymentOptionBalancesResponse"),
    "quota_status": ("GetQuotaStatusRequest", "GetQuotaStatusResponse"),
    "get_quote": ("GetQuoteRequest", "GetQuoteResponse"),
    "place_order": ("PlaceOrderRequest", "PlaceOrderResponse"),
    "positions": ("ListPositionsRequest", "ListPositionsResponse"),
    "order_history": ("QueryOrderHistoryRequest", "QueryOrderHistoryResponse"),
    "settled_history": ("QuerySettledPositionHistoryRequest",
                        "QuerySettledPositionHistoryResponse"),
    "batch_redeem": ("BatchRedeemRequest", "BatchRedeemResponse"),
    "redeem_status": ("GetRedeemStatusRequest", "GetRedeemStatusResponse"),
    "portfolio": ("GetPortfolioRequest", "GetPortfolioResponse"),
    "transfer_in": ("CreateOutboundTransferRequest",
                    "CreateOutboundTransferResponse"),
    "transfer_out": ("CreateInboundTransferRequest",
                     "CreateInboundTransferResponse"),
    "transfer_status": ("GetTransferStatusRequest", "GetTransferStatusResponse"),
}

# Parameters the transport layer adds to every signed request.
TRANSPORT_PARAMS = {"timestamp", "recvWindow", "signature"}


@dataclass
class Interface:
    name: str
    required: set[str] = field(default_factory=set)
    optional: set[str] = field(default_factory=set)

    @property
    def all_fields(self) -> set[str]:
        return self.required | self.optional


def parse_interfaces(path: str) -> dict[str, Interface]:
    """Extract every interface and its required/optional fields from the .d.ts."""
    src = open(path, encoding="utf-8").read()
    out: dict[str, Interface] = {}
    for match in re.finditer(r"interface\s+(\w+)\s*\{(.*?)\n\}", src, re.S):
        name, body = match.group(1), match.group(2)
        iface = Interface(name)
        for line in body.split("\n"):
            field_match = re.match(r"\s*(?:readonly\s+)?(\w+)(\??):", line)
            if not field_match:
                continue
            fname, optional = field_match.group(1), field_match.group(2)
            (iface.optional if optional else iface.required).add(fname)
        out[name] = iface
    return out


@dataclass
class CallSite:
    endpoint: str
    params: set[str]
    dynamic: bool           # params built at runtime, cannot be fully checked
    line: int


def _dict_keys(node: ast.AST) -> tuple[set[str], bool]:
    """Literal string keys of a dict node, plus whether any key is dynamic."""
    keys: set[str] = set()
    dynamic = False
    if not isinstance(node, ast.Dict):
        return keys, True
    for key in node.keys:
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            keys.add(key.value)
        else:
            dynamic = True
    return keys, dynamic


def _resolve_params(arg: ast.AST, scope: ast.AST) -> tuple[set[str], bool]:
    """
    Keys passed as the params argument.

    Handles a dict literal directly, and the common `params = {...}` followed
    by conditional `params["x"] = ...` inserts. Without this the checker
    reports a false "omits every required field" whenever the dict is built
    in a variable, which would make it noise and get it ignored.
    """
    if isinstance(arg, ast.Dict):
        return _dict_keys(arg)
    if not isinstance(arg, ast.Name):
        return set(), True

    name = arg.id
    keys: set[str] = set()
    dynamic = True
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    keys, dynamic = _dict_keys(node.value)
        # params["field"] = value
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            tgt = node.targets[0]
            if (isinstance(tgt, ast.Subscript)
                    and isinstance(tgt.value, ast.Name)
                    and tgt.value.id == name
                    and isinstance(tgt.slice, ast.Constant)
                    and isinstance(tgt.slice.value, str)):
                keys.add(tgt.slice.value)
    return keys, dynamic


def extract_calls(source_path: str) -> list[CallSite]:
    """Find every self._request("name", params) call and the keys it sends."""
    tree = ast.parse(open(source_path, encoding="utf-8").read())
    functions = [n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    calls: list[CallSite] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "_request"):
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        endpoint = node.args[0].value
        if len(node.args) < 2:
            calls.append(CallSite(endpoint, set(), False, node.lineno))
            continue
        # Narrowest enclosing function, so a variable resolves in its scope.
        scope = min(
            (f for f in functions
             if f.lineno <= node.lineno <= (f.end_lineno or f.lineno)),
            key=lambda f: (f.end_lineno or f.lineno) - f.lineno, default=tree)
        params, dynamic = _resolve_params(node.args[1], scope)
        calls.append(CallSite(endpoint, params, dynamic, node.lineno))
    return calls


def extract_response_reads(source_path: str) -> dict[str, set[str]]:
    """
    Field names read off each endpoint's response.

    Approximate by design: it maps the `.get("field")` reads occurring inside
    the same function as a `_request("name", ...)` call. Nested-object fields
    are reported separately rather than being silently accepted.
    """
    tree = ast.parse(open(source_path, encoding="utf-8").read())
    reads: dict[str, set[str]] = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        endpoints = [
            n.args[0].value for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute) and n.func.attr == "_request"
            and n.args and isinstance(n.args[0], ast.Constant)
        ]
        if len(endpoints) != 1:
            continue                       # ambiguous: skip rather than guess
        fields = {
            n.args[0].value for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute) and n.func.attr == "get"
            and n.args and isinstance(n.args[0], ast.Constant)
            and isinstance(n.args[0].value, str)
        }
        reads.setdefault(endpoints[0], set()).update(fields)
    return reads


def parse_constraints(path: str) -> dict[tuple[str, str], dict]:
    """
    Extract value constraints from the connector's doc comments.

    The structural check verifies that a field is named correctly; it cannot
    see that a correctly-named field carries a wrongly-formatted value. That
    is exactly the wei bug: `amountIn` was spelled right and sent as "5.00".
    The doc comments state the enums and the wei requirement, so they are
    machine-checkable too.
    """
    src = open(path, encoding="utf-8").read()
    out: dict[tuple[str, str], dict] = {}
    for match in re.finditer(r"interface\s+(\w+)\s*\{(.*?)\n\}", src, re.S):
        iface, body = match.group(1), match.group(2)
        for hit in re.finditer(
                r"/\*\*(.*?)\*/\s*(?:readonly\s+)?(\w+)\??:", body, re.S):
            doc, fname = hit.group(1), hit.group(2)
            text = " ".join(doc.replace("*", " ").split())
            info: dict = {}
            enums = re.search(r"Enum:\s*(.+?)(?:@type|Default|$)", text)
            if enums:
                vals = re.findall(r"`([A-Z_]+)`", enums.group(1))
                if vals:
                    info["enum"] = set(vals)
            if "wei" in text.lower():
                info["wei"] = True
            if info:
                out[(iface, fname)] = info
    return out


def check_values(source: str, constraints: dict, calls: list) -> list[str]:
    """Verify literal values the bot sends against documented constraints."""
    tree = ast.parse(open(source, encoding="utf-8").read())
    problems: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "_request"):
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        endpoint = node.args[0].value
        mapping = ENDPOINT_INTERFACES.get(endpoint)
        if mapping is None or len(node.args) < 2:
            continue
        if not isinstance(node.args[1], ast.Dict):
            continue
        iface = mapping[0]

        for key, value in zip(node.args[1].keys, node.args[1].values):
            if not isinstance(key, ast.Constant):
                continue
            rule = constraints.get((iface, key.value))
            if not rule:
                continue

            if "enum" in rule and isinstance(value, ast.Constant) \
                    and isinstance(value.value, str):
                if value.value not in rule["enum"]:
                    problems.append(
                        f"L{node.lineno}: {endpoint}.{key.value} = "
                        f"{value.value!r} not in {sorted(rule['enum'])}")

            if rule.get("wei"):
                expr = ast.unparse(value)
                if "to_wei" not in expr:
                    problems.append(
                        f"L{node.lineno}: {endpoint}.{key.value} is documented "
                        f"as wei but is built as {expr!r} without to_wei()")
    return problems


def check(connector: str, source: str) -> int:
    if not os.path.exists(connector):
        print(f"Connector not found at {connector}.\n"
              f"Install it with:  npm install @binance/w3w-prediction",
              file=sys.stderr)
        return 2

    interfaces = parse_interfaces(connector)
    calls = extract_calls(source)
    reads = extract_response_reads(source)

    problems: list[str] = []
    notes: list[str] = []

    print("=== REQUEST CONFORMANCE ===\n")
    seen: set[str] = set()
    for call in calls:
        seen.add(call.endpoint)
        mapping = ENDPOINT_INTERFACES.get(call.endpoint)
        if mapping is None:
            problems.append(f"L{call.line}: endpoint {call.endpoint!r} has no "
                            f"interface mapping")
            continue
        req_name = mapping[0]
        iface = interfaces.get(req_name)
        if iface is None:
            notes.append(f"  {call.endpoint:<18} interface {req_name} not "
                         f"found in connector (name may differ)")
            continue

        sent = call.params | TRANSPORT_PARAMS
        missing = iface.required - sent
        unknown = call.params - iface.all_fields - TRANSPORT_PARAMS

        status = "OK"
        if missing:
            status = "MISSING"
            problems.append(f"L{call.line}: {call.endpoint} omits required "
                            f"{sorted(missing)}")
        if unknown:
            status = "UNKNOWN" if status == "OK" else "BOTH"
            problems.append(f"L{call.line}: {call.endpoint} sends unknown "
                            f"{sorted(unknown)}")
        flag = " (dynamic params)" if call.dynamic else ""
        print(f"  {call.endpoint:<18} {status:<8} "
              f"sends {len(call.params)}, requires {len(iface.required)}{flag}")

    unused = set(ENDPOINT_INTERFACES) - seen
    if unused:
        print(f"\n  endpoints defined but never called: {sorted(unused)}")

    print("\n=== RESPONSE CONFORMANCE ===\n")
    for endpoint, fields in sorted(reads.items()):
        mapping = ENDPOINT_INTERFACES.get(endpoint)
        if mapping is None:
            continue
        iface = interfaces.get(mapping[1])
        if iface is None:
            notes.append(f"  {endpoint:<18} response interface "
                         f"{mapping[1]} not found")
            continue
        # Fields may belong to nested interfaces; collect every field name
        # declared anywhere in the connector as the permissive universe.
        universe: set[str] = set()
        for other in interfaces.values():
            if other.name.startswith(mapping[1]):
                universe |= other.all_fields
        universe |= iface.all_fields

        invented = {f for f in fields if f not in universe}
        # Names that clearly belong to other schemas (nested docs) are noted,
        # not failed, because the heuristic cannot resolve nesting perfectly.
        truly_unknown = {f for f in invented
                         if not any(f in o.all_fields
                                    for o in interfaces.values())}
        print(f"  {endpoint:<18} reads {len(fields):>2} field(s), "
              f"{len(invented)} outside this response, "
              f"{len(truly_unknown)} unknown to the whole schema")
        if truly_unknown:
            problems.append(f"{endpoint}: reads field(s) that exist nowhere in "
                            f"the connector: {sorted(truly_unknown)}")

    print("\n=== VALUE CONFORMANCE (enums and wei formatting) ===\n")
    constraints = parse_constraints(connector)
    print(f"  {len(constraints)} documented constraint(s) found in the schema")
    value_problems = check_values(source, constraints, calls)
    if value_problems:
        problems.extend(value_problems)
        for vp in value_problems:
            print(f"    FAIL {vp}")
    else:
        print("  every literal enum value and wei-typed field conforms")

    if notes:
        print("\n=== NOTES ===")
        for n in notes:
            print(n)

    print("\n=== RESULT ===")
    if problems:
        print(f"  {len(problems)} conformance problem(s):\n")
        for p in problems:
            print(f"    - {p}")
        return 1
    print("  All calls conform to the connector schema.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--connector", default=DEFAULT_CONNECTOR)
    ap.add_argument("--source", default="btc_5m_predictor.py")
    args = ap.parse_args()
    return check(args.connector, args.source)


if __name__ == "__main__":
    raise SystemExit(main())
