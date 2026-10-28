"""The txrisk API as the evaluation pages use it: the only place they get data from.

The pages hold no database driver and no credentials but one API key with the `models:read`
scope, which the API checks on every call.
"""

import os
from typing import Any

import httpx

API_URL_ENV = "TXRISK_API_URL"
API_KEY_ENV = "TXRISK_EVALUATION_API_KEY"
API_KEY_HEADER = "X-API-Key"
DEFAULT_API_URL = "http://api:8000"


class ApiError(Exception):
    """The API could not give what a page asked for; the message says why, for the page to show."""


class NotRecorded(ApiError):
    """The API has no such thing (a version without an evaluation report, say)."""


class Api:
    """Reads model versions, their cards and their evaluation reports through `/v1/models`."""

    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    @classmethod
    def from_environment(cls) -> "Api":
        key = os.environ.get(API_KEY_ENV)
        if not key:
            raise ApiError(
                f"{API_KEY_ENV} is not set. Issue a key with the models:read scope "
                "(`txrisk api-key --name evaluation-pages --scope models:read`) and put it in .env."
            )
        return cls(
            httpx.Client(
                base_url=os.environ.get(API_URL_ENV, DEFAULT_API_URL),
                headers={API_KEY_HEADER: key},
                timeout=10.0,
            )
        )

    def _get(self, path: str) -> dict[str, Any]:
        try:
            response = self._http.get(path)
        except httpx.HTTPError as e:
            raise ApiError(f"the API could not be reached: {e}") from e
        if response.status_code == 404:
            raise NotRecorded(str(response.json().get("detail", "not found")))
        if response.status_code in (401, 403):
            raise ApiError(
                f"the API refused the key ({response.status_code}): it needs the models:read scope"
            )
        if response.is_error:
            raise ApiError(f"the API answered {response.status_code}")
        body: dict[str, Any] = response.json()
        return body

    def models(self) -> dict[str, Any]:
        """The Champion and Challenger, every known version, and the Promotion history."""
        return self._get("/v1/models")

    def card(self, version: str) -> dict[str, Any]:
        """The model card of a version, as data."""
        card: dict[str, Any] = self._get(f"/v1/models/{version}/card")["card"]
        return card

    def evaluation(self, version: str) -> dict[str, Any] | None:
        """The offline evaluation report saved with a version, or None if it has none."""
        try:
            report: dict[str, Any] = self._get(f"/v1/models/{version}/evaluation")["evaluation"]
        except NotRecorded:
            return None
        return report
