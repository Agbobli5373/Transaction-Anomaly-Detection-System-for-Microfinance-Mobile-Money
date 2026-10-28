"""What the pages show, fetched through the API and kept for half a minute."""

from typing import Any

import streamlit as st
from client import Api, ApiError

_api: Api | None = None


def use(api: Api) -> None:
    """Fetch through `api` instead of the one the environment configures (tests)."""
    global _api
    _api = api
    st.cache_data.clear()


def _client() -> Api:
    global _api
    if _api is None:
        _api = Api.from_environment()
    return _api


@st.cache_data(ttl=30, show_spinner=False)
def models() -> dict[str, Any]:
    return _client().models()


@st.cache_data(ttl=30, show_spinner=False)
def card(version: str) -> dict[str, Any]:
    return _client().card(version)


@st.cache_data(ttl=30, show_spinner=False)
def evaluation(version: str) -> dict[str, Any] | None:
    return _client().evaluation(version)


def listed_versions() -> dict[str, Any]:
    """The models the API lists, or the reason it cannot, shown and the page stopped."""
    try:
        listed = models()
    except ApiError as e:
        st.error(str(e))
        st.stop()
    if not listed["models"]:
        st.info("No model versions yet. Train one with `make train`.")
        st.stop()
    return listed


def describe(entry: dict[str, Any]) -> str:
    """A version's name with its roles, as it is offered in a picker."""
    roles = ", ".join(entry["roles"])
    return f"{entry['version']} ({roles})" if roles else entry["version"]
