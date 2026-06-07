"""Authentication helper for Streamlit pages.

Built on streamlit-authenticator. Configures a single coordinator user
(credentials taken from .env / environment) and exposes a `gate()` helper
that pages can call at the very top to enforce login.

Usage in a page:

    from services.auth import gate
    auth = gate(required_role="coordinator")
    # if we got here, the user is authenticated; auth["name"] is the display name
"""
from __future__ import annotations

import os
from typing import Optional

import bcrypt
import streamlit as st
import streamlit_authenticator as stauth
from dotenv import load_dotenv

load_dotenv()


COORD_USER = os.getenv("AUTH_COORDINATOR_USER", "coordinator")
COORD_PASS = os.getenv("AUTH_COORDINATOR_PASSWORD", "spandan@2026")
COOKIE_SECRET = os.getenv("AUTH_COOKIE_SECRET", "spandan-default-cookie")


def _hash_password(plain: str) -> str:
    """Hash a password with bcrypt. Works across all streamlit-authenticator
    versions (0.3.x uses Hasher([..]).generate(), 0.4.x added Hasher.hash —
    we sidestep that whole API by using bcrypt directly, which is what the
    library uses under the hood anyway).
    """
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _build_authenticator() -> stauth.Authenticate:
    hashed = _hash_password(COORD_PASS)
    config = {
        "credentials": {
            "usernames": {
                COORD_USER: {
                    "name": "Coordinator",
                    "password": hashed,
                    "email": "coordinator@spandan.local",
                    "roles": ["coordinator"],
                }
            }
        },
        "cookie": {
            "name": "spandan_auth",
            "key": COOKIE_SECRET,
            "expiry_days": 1,
        },
        "preauthorized": {"emails": []},
    }
    return stauth.Authenticate(
        config["credentials"],
        config["cookie"]["name"],
        config["cookie"]["key"],
        config["cookie"]["expiry_days"],
    )


def render_login_page() -> bool:
    """Render the split-screen login page (left: brand, right: form).

    Returns True if the user is authenticated after this call (i.e. the
    caller should re-render the dashboard); False if still showing login.
    """
    if "authenticator" not in st.session_state:
        st.session_state.authenticator = _build_authenticator()
    authenticator: stauth.Authenticate = st.session_state.authenticator

    # Hide sidebar + Streamlit chrome on the login screen
    st.markdown(
        """
        <style>
        [data-testid='stSidebar'], [data-testid='collapsedControl'] { display:none !important; }
        [data-testid='stHeader'] { background: transparent; }
        .block-container { padding: 0 !important; max-width: 100% !important; }
        .login-wrap { min-height: 100vh; display:flex; }
        .login-left {
            flex: 1.1; background: linear-gradient(135deg, #c0273f 0%, #6a121c 100%);
            color: #fff; padding: 64px 72px; display: flex; flex-direction: column;
            justify-content: space-between;
        }
        .login-right {
            flex: 1; background: #ffffff; display:flex; align-items:center;
            justify-content: center; padding: 40px;
        }
        .login-card { width: 100%; max-width: 380px; }
        .brand-tag { font-size: 12px; letter-spacing: 4px; opacity: 0.85; }
        .brand-h1 { font-size: 56px; font-weight: 700; line-height: 1.05; margin: 12px 0 18px 0; letter-spacing: -1.5px; }
        .brand-sub { font-size: 17px; line-height: 1.55; opacity: 0.92; max-width: 460px; }
        .brand-pillar { display:flex; gap:14px; margin-top: 14px; align-items:flex-start; }
        .brand-pillar .ico {
            min-width: 36px; height: 36px; border-radius: 10px; background: rgba(255,255,255,0.15);
            display:flex; align-items:center; justify-content:center; font-size:16px;
        }
        .brand-pillar .txt { font-size: 14px; line-height: 1.5; opacity: 0.92; }
        .brand-pillar .txt b { font-weight: 600; }
        .brand-foot { font-size: 12px; opacity: 0.7; letter-spacing: 1px; }
        .form-h { font-size: 26px; font-weight: 700; color: #1a1a1a; margin: 0 0 6px 0; letter-spacing: -0.5px; }
        .form-sub { font-size: 14px; color: #7a7f87; margin: 0 0 28px 0; }
        .form-foot { font-size: 12px; color: #9aa0a8; margin-top: 18px; line-height: 1.5; }
        .form-foot code { background: #f4f5f7; padding: 1px 6px; border-radius: 4px; font-size: 12px; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    left, right = st.columns([1.1, 1], gap="large")

    with left:
        # Inline SVG mark: a clean blood drop with a heartbeat pulse line
        # threaded through it. No external asset, scales crisp at any size.
        logo_svg = (
            "<svg width='52' height='52' viewBox='0 0 64 64' xmlns='http://www.w3.org/2000/svg' "
            "style='display:block;'>"
            # outer drop
            "<path d='M32 4 C32 4 12 26 12 40 a20 20 0 0 0 40 0 C52 26 32 4 32 4 Z' "
            "fill='#ffffff' opacity='0.97'/>"
            # heartbeat line through the drop
            "<path d='M14 42 L22 42 L26 32 L31 50 L36 26 L41 42 L50 42' "
            "fill='none' stroke='#c0273f' stroke-width='3' stroke-linecap='round' "
            "stroke-linejoin='round'/>"
            "</svg>"
        )

        st.markdown(
            f"""
            <div style="background: linear-gradient(135deg, #c0273f 0%, #6a121c 100%);
                        color: #fff; padding: 56px 56px 44px 56px; border-radius: 14px;
                        min-height: 88vh; display:flex; flex-direction: column; justify-content: space-between;">
              <div>
                <div style='display:flex;align-items:center;gap:14px;margin-bottom:6px;'>
                  <div style='background:rgba(255,255,255,0.14);border-radius:14px;
                              padding:8px 10px;display:flex;align-items:center;justify-content:center;'>
                    {logo_svg}
                  </div>
                  <div>
                    <div style='font-size:22px;font-weight:700;letter-spacing:-0.3px;line-height:1;'>Spandan</div>
                    <div style='font-size:10.5px;letter-spacing:3.5px;opacity:0.85;margin-top:4px;'>BLOOD WARRIORS</div>
                  </div>
                </div>
                <div style='font-size:52px;font-weight:700;line-height:1.05;letter-spacing:-1.5px;margin-top:24px;margin-bottom:18px;'>
                  Autonomous<br>blood-care AI.
                </div>
                <div style='font-size:16px;line-height:1.55;opacity:0.92;max-width:460px;margin-bottom:32px;'>
                  An always-on agent that finds the right donor, in the right
                  language, the moment a patient needs blood — without a
                  coordinator having to lift a finger.
                </div>
                <div style='display:flex;gap:14px;margin-top:12px;align-items:flex-start;'>
                  <div style='min-width:36px;height:36px;border-radius:10px;background:rgba(255,255,255,0.15);display:flex;align-items:center;justify-content:center;font-size:16px;'>★</div>
                  <div style='font-size:14px;line-height:1.5;opacity:0.92;'><b>Predict, rank, reach out</b><br>Spandan watches the patient pipeline 24×7 and contacts the most likely donor first.</div>
                </div>
                <div style='display:flex;gap:14px;margin-top:14px;align-items:flex-start;'>
                  <div style='min-width:36px;height:36px;border-radius:10px;background:rgba(255,255,255,0.15);display:flex;align-items:center;justify-content:center;font-size:16px;'>✉</div>
                  <div style='font-size:14px;line-height:1.5;opacity:0.92;'><b>Real emails. One-click reply.</b><br>Donors say YES or NO from their inbox; we never overbook a patient.</div>
                </div>
                <div style='display:flex;gap:14px;margin-top:14px;align-items:flex-start;'>
                  <div style='min-width:36px;height:36px;border-radius:10px;background:rgba(255,255,255,0.15);display:flex;align-items:center;justify-content:center;font-size:16px;'>♥</div>
                  <div style='font-size:14px;line-height:1.5;opacity:0.92;'><b>Speaks every donor's language</b><br>Outreach drafted natively in Telugu, Hindi, Tamil, English.</div>
                </div>
              </div>
              <div style='font-size:11px;opacity:0.7;letter-spacing:1.5px;margin-top:36px;'>BLOOD WARRIORS · HYDERABAD · INDIA</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with right:
        st.markdown(
            "<div style='padding-top:18vh;'>"
            "<div style='font-size:26px;font-weight:700;color:#1a1a1a;margin-bottom:6px;letter-spacing:-0.5px;'>Welcome back</div>"
            "<div style='font-size:14px;color:#7a7f87;margin-bottom:22px;'>Sign in to the Spandan coordinator console.</div>"
            "</div>",
            unsafe_allow_html=True,
        )
        authenticator.login(
            location="main",
            fields={
                "Form name": " ",
                "Username": "Username",
                "Password": "Password",
                "Login": "Sign in",
            },
        )
        auth_status = st.session_state.get("authentication_status")
        if auth_status is False:
            st.error("Username or password is incorrect.")
        elif auth_status is None:
            pass
        else:
            return True
    return False


def gate(required_role: str = "coordinator") -> dict:
    """Enforce login on a Streamlit page. Stops execution if not authenticated.

    Returns a dict with name/username/role on success.
    """
    if "authenticator" not in st.session_state:
        st.session_state.authenticator = _build_authenticator()
    authenticator: stauth.Authenticate = st.session_state.authenticator

    auth_status = st.session_state.get("authentication_status")
    if not auth_status:
        # Render the same split-screen login the Home page uses
        if not render_login_page():
            st.stop()

    # Logged in — render compact sidebar with sign-out, hide Bridge/Reply
    # from the auto-generated nav so the only visible page is Coordinator.
    name = st.session_state.get("name") or "User"
    username = st.session_state.get("username") or ""
    st.markdown(
        """
        <style>
        /* Hide Streamlit's auto-generated page nav entirely; we render our
           own compact sidebar below. Coordinator is the only landing page. */
        [data-testid="stSidebarNav"] { display: none !important; }
        </style>
        """,
        unsafe_allow_html=True,
    )
    sidebar_logo_svg = (
        "<svg width='34' height='34' viewBox='0 0 64 64' xmlns='http://www.w3.org/2000/svg' "
        "style='display:block;'>"
        "<path d='M32 4 C32 4 12 26 12 40 a20 20 0 0 0 40 0 C52 26 32 4 32 4 Z' "
        "fill='#c0273f'/>"
        "<path d='M14 42 L22 42 L26 32 L31 50 L36 26 L41 42 L50 42' "
        "fill='none' stroke='#ffffff' stroke-width='3' stroke-linecap='round' "
        "stroke-linejoin='round'/>"
        "</svg>"
    )
    with st.sidebar:
        st.markdown(
            f"<div style='display:flex;align-items:center;gap:10px;padding:4px 0 14px 0;"
            f"border-bottom:1px solid #e7e9ed;margin-bottom:14px;'>"
            f"<div>{sidebar_logo_svg}</div>"
            f"<div>"
            f"<div style='font-weight:700;font-size:18px;color:#c0273f;line-height:1;'>Spandan</div>"
            f"<div style='font-size:10.5px;color:#7a7f87;letter-spacing:1.5px;margin-top:3px;'>BLOOD WARRIORS · AI</div>"
            f"</div>"
            f"</div>",
            unsafe_allow_html=True,
        )
        st.write(f"**Signed in:** {name}")
        st.caption(f"@{username}")
        st.markdown("---")
        st.markdown("##### Quick links")
        import os as _os
        mailpit = _os.getenv("MAILPIT_UI_URL", "http://localhost:8025")
        st.markdown(
            f"<a href='{mailpit}' target='_blank' style='display:block;text-decoration:none;"
            f"background:#eef3fc;color:#2a4ea1;padding:8px 12px;border-radius:8px;font-size:13px;"
            f"font-weight:500;margin-bottom:8px;'>📧 Open MailPit inbox</a>",
            unsafe_allow_html=True,
        )
        st.markdown("---")
        if st.button("Sign out", use_container_width=True):
            authenticator.logout(location="unrendered")
            st.session_state["authentication_status"] = None
            st.rerun()
    return {"name": name, "username": username, "role": required_role}
