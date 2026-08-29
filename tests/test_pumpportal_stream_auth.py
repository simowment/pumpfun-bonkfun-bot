"""PumpPortal stream endpoint auth wiring."""

# ruff: noqa: TRY003

from rugbot.ingest.pump.pump_stream import (
    PUMPPORTAL_WS_URL,
    PumpPortalLaunchStream,
)


def test_stream_endpoint_unchanged_without_api_key():
    stream = PumpPortalLaunchStream()
    assert stream._websocket_endpoint == PUMPPORTAL_WS_URL


def test_api_key_appended_as_query_parameter():
    stream = PumpPortalLaunchStream(api_key="abc-123")
    assert stream._websocket_endpoint == f"{PUMPPORTAL_WS_URL}?api-key=abc-123"


def test_api_key_is_url_encoded():
    stream = PumpPortalLaunchStream(api_key="a/b&c=d")
    assert stream._websocket_endpoint.endswith("?api-key=a%2Fb%26c%3Dd")


def test_existing_query_params_are_preserved():
    stream = PumpPortalLaunchStream(
        f"{PUMPPORTAL_WS_URL}?api-key=bundled",
        api_key="real",
    )
    # Explicit endpoint wins; the key only extends the provided URI.
    assert "api-key=bundled" in stream._websocket_endpoint
    assert stream._websocket_endpoint.count("api-key=") == 2


def test_non_wss_endpoint_rejected():
    try:
        PumpPortalLaunchStream("https://pumpportal.example")
    except ValueError as error:
        assert "wss://" in str(error)
    else:
        raise AssertionError("expected ValueError for non-wss endpoint")
