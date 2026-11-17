"""Errors shortened for alerts: what failed, not the request and response."""

from __future__ import annotations

import pytest

from betfair_results_downloader.error_text import brief_error


class APIError(Exception):
    """Stands in for betfairlightweight's, which renders exactly like this."""


class InvalidResponse(Exception):
    pass


def _api_error(method: str, ids: int = 40) -> APIError:
    markets = [f"1.2655{n:05d}" for n in range(ids)]
    body = (
        "{'code': -32099, 'message': 'ANGX-0006', 'data': {'APINGException': "
        "{'requestUUID': 'ie2-ang22a-prd-11110953-0009857119', 'errorCode': "
        "'UNEXPECTED_ERROR', 'errorDetails': ''}, 'exceptionname': 'APINGException'}}"
    )
    return APIError(
        f"SportsAPING/v1.0/{method} \nParams: {{'marketIds': {markets}}} \n"
        f"Exception: None \nError: {body} \n"
        f"Full Response: {{'jsonrpc': '2.0', 'error': {body}, 'id': 1}}"
    )


def test_a_betfair_api_error_is_the_call_and_the_error_code() -> None:
    exc = _api_error("listMarketBook")
    assert len(str(exc)) > 1000
    assert brief_error(exc) == (
        "APIError: SportsAPING/v1.0/listMarketBook returned UNEXPECTED_ERROR (ANGX-0006)"
    )


def test_the_short_form_carries_no_market_ids_or_response_body() -> None:
    short = brief_error(_api_error("listClearedOrders", ids=200))
    assert (
        "1.2655" not in short and "requestUUID" not in short and "Params" not in short
    )
    assert len(short) < 120


def test_an_invalid_response_says_how_big_the_reply_was_not_what_was_in_it() -> None:
    reply = (
        "["
        + ", ".join("{'marketId': '1.265614682', 'venue': 'X'}" for _ in range(73))
        + "]"
    )
    exc = InvalidResponse(f"Invalid response received: {reply}")
    short = brief_error(exc)
    assert short == (
        f"InvalidResponse: Betfair's reply was missing required fields "
        f"({len(str(exc)):,} characters; full text in the log)"
    )
    assert "marketId" not in short


@pytest.mark.parametrize(
    "exc,expected",
    [
        (RuntimeError("catalogue down"), "RuntimeError: catalogue down"),
        (
            ValueError("line one\n  line two\ttabbed"),
            "ValueError: line one line two tabbed",
        ),
        (TimeoutError(), "TimeoutError"),
        (
            OSError(28, "No space left on device"),
            "OSError: [Errno 28] No space left on device",
        ),
    ],
)
def test_an_ordinary_error_keeps_its_own_message_on_one_line(exc, expected) -> None:
    assert brief_error(exc) == expected


def test_a_long_message_is_cut_and_points_at_the_log() -> None:
    short = brief_error(RuntimeError("x" * 5000))
    assert short == "RuntimeError: " + "x" * 200 + "… (truncated; full text in the log)"
