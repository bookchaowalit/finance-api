"""SPEC-003: the edge bypass header goes to the gateway host only."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from finance_api import server
from finance_api.cells import CellClient

SECRET = "a" * 43


def _run(client: CellClient, cell: str) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"items": []})

    client.transport = httpx.MockTransport(handler)

    async def go() -> None:
        async with client.session() as http:
            await client.list_all(http, cell, "transactions", "tok")

    asyncio.run(go())
    return seen


def test_the_gateway_gets_the_header():  # AC-01
    client = CellClient(
        {"transactions": "https://gateway.bookchaowalit.com/v1/transactions"},
        edge_bypass=SECRET,
    )
    (request,) = _run(client, "transactions")
    assert request.headers["X-Edge-Bypass"] == SECRET
    assert request.headers["Authorization"] == "Bearer tok"


def test_other_hosts_never_get_it():  # AC-02
    client = CellClient(
        {"transactions": "https://transactions.example"}, edge_bypass=SECRET
    )
    (request,) = _run(client, "transactions")
    assert "X-Edge-Bypass" not in request.headers


def test_unset_sends_nothing():  # AC-03
    client = CellClient(
        {"transactions": "https://gateway.bookchaowalit.com/v1/transactions"}
    )
    (request,) = _run(client, "transactions")
    assert "X-Edge-Bypass" not in request.headers


@pytest.mark.parametrize("bad", ["short", "x" * 40 + " ", "x" * 40 + "\n", "é" * 40])
def test_a_bad_value_stops_the_service(bad):  # AC-04
    with pytest.raises(SystemExit, match="FINANCE_API_EDGE_BYPASS"):
        server.cells({"FINANCE_API_EDGE_BYPASS": bad})


def test_the_server_passes_the_value_through():  # AC-01, AC-03
    assert server.cells({"FINANCE_API_EDGE_BYPASS": SECRET}).edge_bypass == SECRET
    assert server.cells({}).edge_bypass is None
    assert server.cells({"FINANCE_API_EDGE_BYPASS": ""}).edge_bypass is None
