"""Spandan landing + sign-in.

This is the only entry point. The user lands here, sees the split-screen
login (left = brand, right = form), and on successful sign-in is taken
straight to the Coordinator dashboard.

Run locally with:
    streamlit run app/Home.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

st.set_page_config(
    page_title="Spandan — Sign in",
    page_icon="🩸",
    layout="wide",
    initial_sidebar_state="collapsed",
)

from services.auth import render_login_page

# Already signed in? Take them straight to Coordinator.
if st.session_state.get("authentication_status"):
    st.markdown(
        "<style>[data-testid='stSidebar'] {display:none;} [data-testid='collapsedControl'] {display:none;}</style>",
        unsafe_allow_html=True,
    )
    st.markdown(
        "<div style='padding:18vh 0 0 0;text-align:center;'>"
        "<div style='font-size:32px;font-weight:700;color:#c0273f;letter-spacing:-0.5px;'>Welcome, "
        f"{st.session_state.get('name','Coordinator')}</div>"
        "<div style='font-size:14px;color:#7a7f87;margin-top:6px;'>Loading the coordinator console …</div>"
        "</div>",
        unsafe_allow_html=True,
    )
    # Redirect into the Coordinator page
    try:
        st.switch_page("pages/1_Coordinator.py")
    except Exception:
        st.page_link("pages/1_Coordinator.py", label="Open Coordinator", icon="🎯")
    st.stop()

# Not signed in: render the split-screen login. On success, st.rerun()
# will trigger the redirect block above.
if render_login_page():
    st.rerun()
