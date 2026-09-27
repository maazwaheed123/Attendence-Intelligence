"""Placeholder UI (Step 0). The real pages arrive in Step 15.

The UI talks only to the API, never to the database.
"""

import os

import httpx
import streamlit as st

API_URL = os.getenv("API_URL", "http://api:8000")

st.set_page_config(page_title="Attendance Intelligence", layout="wide")
st.title("Attendance Intelligence")
st.caption("MVP console. Full pages arrive in Step 15.")

try:
    health = httpx.get(f"{API_URL}/v1/health", timeout=5).json()
    st.success(f"API reachable: {health}")
except Exception as exc:  # noqa: BLE001 - display any connectivity failure
    st.error(f"API not reachable at {API_URL}: {exc}")
