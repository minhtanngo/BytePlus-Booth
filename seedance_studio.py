"""
============================================================
 SEEDANCE STUDIO — Streamlit (BytePlus)
 One-file app: cinematic BytePlus-branded UI + Seedance 2.0 R2V pipeline.

 FLOW:
   1. Upload OR take a photo (one portrait — any gender works).
   2. Choose ONE of four cinematic worlds.
   3. Enter name + phone + email (all required).
   4. A 15-second short film is generated from that portrait in the
      chosen world, via a shared background queue (handles many users
      at once, capped by MAX_CONCURRENT_GENERATIONS).
   5. When it finishes, the customer is automatically emailed their film.

 Run (demo mode, mocked generation):
   pip install streamlit httpx
   streamlit run seedance_studio.py

 Run (real generation):
   pip install streamlit httpx tos
   export ARK_API_KEY=...
   export TOS_ACCESS_KEY=... TOS_SECRET_KEY=...
   export TOS_BUCKET=seedance-studio TOS_REGION=ap-southeast-1
   streamlit run seedance_studio.py
============================================================
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import smtplib
import subprocess
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import streamlit as st

# ──────────────────────────────────────────────────────────
# CONFIG  (AK/SK + Seedance usage UNCHANGED)
# ──────────────────────────────────────────────────────────
ARK_API_KEY = os.environ.get("ARK_API_KEY", "")
ARK_ENDPOINT = "https://ark.ap-southeast.bytepluses.com/api/v3/contents/generations/tasks"
MODEL_ID = "dreamina-seedance-2-0-260128"

TOS_BUCKET = os.environ.get("TOS_BUCKET", "")
TOS_REGION = os.environ.get("TOS_REGION", "ap-southeast-1")
TOS_ACCESS_KEY = os.environ.get("TOS_ACCESS_KEY", "")
TOS_SECRET_KEY = os.environ.get("TOS_SECRET_KEY", "")
TOS_ENDPOINT = os.environ.get("TOS_ENDPOINT", f"https://tos-{TOS_REGION}.bytepluses.com")
TOS_PUBLIC_HOST = f"{TOS_BUCKET}.tos-{TOS_REGION}.bytepluses.com"

CLIP_DURATION = 15        # single-clip duration on Seedance 2.0
ASPECT_RATIO = "9:16"
RESOLUTION = "720p"
POLL_INTERVAL = 5
POLL_TIMEOUT = 1000

# BytePlus logo overlay burned into each finished film (bottom-right) via ffmpeg.
WATERMARK_PATH = str(Path(__file__).parent / "byteplus_watermark.png")
WATERMARK_ENABLED = (
    os.environ.get("WATERMARK", "1").lower() not in ("0", "false", "no")
    and os.path.exists(WATERMARK_PATH)
)
WATERMARK_WIDTH = int(os.environ.get("WATERMARK_WIDTH", "220"))  # px, scaled by aspect
WATERMARK_OPACITY = float(os.environ.get("WATERMARK_OPACITY", "0.85"))
WATERMARK_MARGIN = int(os.environ.get("WATERMARK_MARGIN", "28"))  # px from edges

ARK_ASSET_GROUP_ID = os.environ.get("ARK_ASSET_GROUP_ID", "")
ARK_AK = os.environ.get("ARK_AK", "")
ARK_SK = os.environ.get("ARK_SK", "")
ARK_ASSET_GROUP_NAME = "seedance_studio_subjects"
ARK_PROJECT_NAME = "default"
ARK_REGION = "ap-southeast-1"
ASSET_POLL_INTERVAL = 5
ASSET_POLL_TIMEOUT = 300
USE_ASSET_LIBRARY = bool(ARK_AK and ARK_SK)

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM_EMAIL = os.environ.get("SMTP_FROM_EMAIL", SMTP_USER)
SMTP_FROM_NAME = os.environ.get("SMTP_FROM_NAME", "Seedance Studio")
EMAIL_ENABLED = bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD)

DEMO_MODE = not all([ARK_API_KEY, TOS_BUCKET, TOS_ACCESS_KEY, TOS_SECRET_KEY])

# BytePlus brand
BP_BLUE = "#2E72FF"
BP_BLUE_SOFT = "#5B93FF"

# ──────────────────────────────────────────────────────────
# THEMES — four vibrant 15-second worlds; the customer chooses ONE
# ──────────────────────────────────────────────────────────
PROMPT_FOOTER = (
    " Keep the main character the exact same person as in [Image 1] in every "
    "shot — same face and features, clearly lit and recognisable; do not "
    "restyle or beautify the face. Show only ONE person on screen at all times "
    "— never duplicate, clone or mirror the character. No on-screen text, "
    "watermarks, logos or subtitles. Original score and sound design only — no "
    "recognisable melodies, songs, lyrics or famous film music."
)

def _p(text: str) -> str:
    """Append the universal character-lock + audio footer to a scene prompt."""
    return text.rstrip() + PROMPT_FOOTER


THEMES: dict[str, dict[str, Any]] = {

    "neon-velocity": {
        "code": "01",
        "name": "NEON VELOCITY",
        "genre": "CYBERPUNK · STREET RACE",
        "tagline": "Green light. The whole city blurs into light.",
        "scene": "You grip the wheel, hit the NOS, and tear through a neon city.",
        "description": "A high-octane cyberpunk night race. You are the driver behind the wheel of a glowing supercar, rain-slick neon streets streaking past, the boost igniting in a surge of electric speed through a tunnel of light.",
        "signature": "#00E5FF",
        "accent": "#FF2E97",
        "paper": "#05060B",
        "keywords": ["CYBERPUNK", "NEON", "SPEED", "RACE"],
        "prompt": _p(
            "Cinematic cyberpunk night street race, vertical 9:16, "
            "photorealistic, neon-soaked, strong sense of speed, 15 seconds. The "
            "single driver is [Image 1], wearing a black racing jacket with cyan "
            "trim. Motion-blur the environment but keep the face sharp and "
            "clearly lit. "
            "Shot 1 (0-5s): clean close-up of [Image 1] behind the wheel of a "
            "glowing supercar, calm focused eyes, soft dashboard light, neon city "
            "blurred beyond the windshield. "
            "Shot 2 (5-10s): exterior shot of the car racing through a tunnel of "
            "neon signs and tail-lights, light trails streaking past (no face). "
            "Shot 3 (10-15s): cut back to a clear close-up of [Image 1] with a "
            "confident half-smile, city lights as soft bokeh behind, hold on the "
            "face, then cut to black. "
            "Look: glossy cyberpunk, deep blacks, cyan and magenta, "
            "photorealistic. Audio: engine roar, turbo, NOS hiss, tyre screech, "
            "driving synth-percussion."
        ),
    },

    "orbital-summit": {
        "code": "02",
        "name": "ORBITAL SUMMIT",
        "genre": "SCI-FI · EXECUTIVE KEYNOTE",
        "tagline": "Earth turns below. Your keynote lights up the void.",
        "scene": "You take the stage on an orbital station and summon a sky of holographic data.",
        "description": "A sci-fi executive keynote aboard an orbital space station. You are the speaker on a glass conference deck with Earth turning beyond the window, conjuring holographic charts and a glowing product reveal before an audience among the stars.",
        "signature": "#4DA3FF",
        "accent": "#7FE9D0",
        "paper": "#04060E",
        "keywords": ["ORBITAL", "HOLOGRAM", "SUMMIT", "SCI-FI"],
        "prompt": _p(
            "Cinematic sci-fi executive keynote aboard an orbital space station, "
            "vertical 9:16, 8K, photorealistic, sleek glass-and-chrome design "
            "lit by holographic blue light, 15 seconds. The single speaker is "
            "[Image 1], in a sharp modern business suit. "
            "Shot 1 (0-5s): [Image 1] walks confidently across a curved glass "
            "deck; through the window behind, Earth turns slowly in space. "
            "Shot 2 (5-10s): [Image 1] raises a hand and glowing holographic "
            "charts and a rotating globe bloom in the air; clear close-up of "
            "[Image 1]'s face lit blue by the holograms. "
            "Shot 3 (10-15s): low-angle hero shot of [Image 1] standing tall "
            "with holograms and Earth behind, a calm assured smile; hold on the "
            "face, then a clean cut to white. "
            "Look: premium sci-fi, glass, chrome, holographic cyan-blue, "
            "photorealistic. Audio: soft station ambience, holographic chimes "
            "and whooshes, swelling cinematic synth-orchestral score."
        ),
    },

    "stormbreaker": {
        "code": "03",
        "name": "STORMBREAKER",
        "genre": "LIVE-ACTION ANIME · FX",
        "tagline": "One breath. A water dragon and a storm of gold.",
        "scene": "You draw a blade and summon a roaring blue water dragon.",
        "description": "A Hollywood live-action anime-style battle. You are a lone elemental swordmaster in a moonlit forest, summoning a towering blue water dragon and a tempest of golden lightning in a single devastating draw of the blade.",
        "signature": "#36B5FF",
        "accent": "#E0A93A",
        "paper": "#060810",
        "keywords": ["ANIME", "ELEMENTAL", "VFX", "EPIC"],
        "prompt": _p(
            "Hollywood live-action anime style, dark samurai mood, vertical "
            "9:16, 4K, dramatic particle effects, no gore, 15 seconds. The single "
            "warrior is [Image 1], in a green-and-black checkered haori, holding "
            "a katana in a misty moonlit forest. "
            "Shot 1 (0-5s): [Image 1] lowers into a stance, both hands on the "
            "hilt; as the blade draws, a giant blue water dragon spirals around "
            "the warrior, roaring with rushing water. "
            "Shot 2 (5-10s): [Image 1] dashes forward in a streak of golden "
            "lightning between the trees; quick clear close-up of [Image 1]'s "
            "focused face. "
            "Shot 3 (10-15s): [Image 1] swings the blade down — water dragon and "
            "golden lightning collide in a burst of light; the warrior lands in a "
            "low stance, lit blue and gold; hold on the calm face, then cut to "
            "black. "
            "Look: cinematic anime realism, electric blue and gold, volumetric "
            "mist, photorealistic VFX. Audio: rushing water, crackling "
            "lightning, blade ring, deep impact, epic percussive score."
        ),
    },

    "festival-of-light": {
        "code": "04",
        "name": "FESTIVAL OF LIGHT",
        "genre": "FESTIVAL · JOYFUL MV",
        "tagline": "A thousand lanterns rise. So does your smile.",
        "scene": "You release a glowing lantern as fireworks bloom overhead.",
        "description": "A warm, vibrant lantern festival by an old riverside town at night — glowing paper lanterns drifting on the water and into the sky, fireworks blooming above, and you at the joyful heart of it, releasing a lantern of your own.",
        "signature": "#FF8A3D",
        "accent": "#C81E3A",
        "paper": "#0A0705",
        "keywords": ["FESTIVAL", "LANTERNS", "FIREWORKS", "JOY"],
        "prompt": _p(
            "Warm, joyful lantern-festival video, vertical 9:16, ultra-realistic, "
            "golden-amber and crimson tones, 15 seconds. The single lead is "
            "[Image 1], in elegant festive attire at an old riverside town at "
            "night strung with glowing lanterns. "
            "Shot 1 (0-5s): warm shot gliding toward [Image 1] on a stone bridge "
            "over a lantern-lit river, floating lanterns and soft bokeh all "
            "around, a bright genuine smile. "
            "Shot 2 (5-10s): clear close-up of [Image 1] cupping a glowing paper "
            "lantern, then releasing it; slow motion as it rises, golden light "
            "warming the face. "
            "Shot 3 (10-15s): the camera tilts up to a sky full of lanterns as "
            "fireworks bloom in gold and crimson; cut back to [Image 1] laughing "
            "with joy, fireworks reflected behind; hold on the radiant face, then "
            "a soft cut to warm light. "
            "Look: lush festival realism, glowing lanterns, gold and crimson "
            "fireworks, creamy bokeh, photorealistic. Audio: distant festival "
            "crowd, firework crackle and whoosh, water lapping, uplifting "
            "celebratory score."
        ),
    },

}

# ──────────────────────────────────────────────────────────
# CSS  (BytePlus-branded cinematic theme)
# ──────────────────────────────────────────────────────────
CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;0,400;0,500;0,600;1,400;1,500&family=JetBrains+Mono:wght@300;400;500&family=Inter:wght@400;500;600;700&display=swap');

/* Hide Streamlit chrome */
header[data-testid="stHeader"] { display: none; }
.stDeployButton { display: none !important; }
footer { display: none !important; }
#MainMenu { display: none; }
section[data-testid="stSidebar"] { display: none; }
[data-testid="stToolbar"] { display: none; }
[data-testid="stDecoration"] { display: none; }

html, body, [data-testid="stAppViewContainer"] {
    background: #07080A !important;
    color: #EDEBE4 !important;
}
[data-testid="stAppViewContainer"] { background:
    radial-gradient(1200px 600px at 80% -10%, rgba(46,114,255,0.10), transparent 60%),
    #07080A !important;
}
.block-container {
    max-width: 1200px !important;
    padding-top: 2.4rem !important;
    padding-bottom: 3rem !important;
}

.stMarkdown, .stMarkdown * { color: #EDEBE4; }
.stMarkdown p { font-family: 'Cormorant Garamond', serif; font-weight: 500; font-size: 1.18rem; line-height: 1.65; color: #d9d6cc; }

h1, h2, h3, h4 {
    font-family: 'Cormorant Garamond', serif !important;
    color: #F5F3EC !important;
    font-weight: 400 !important;
    letter-spacing: -0.01em !important;
}

/* ──────────────────────────────────────────────────────────
   BUTTONS
   In Streamlit the DEFAULT button kind is "secondary", so we key
   styling on the kind attribute and mark real CTAs as type="primary".
     • kind="primary"   → solid BytePlus blue (the CTA look)
     • kind="secondary" → transparent outline (back / ghost actions)
   ────────────────────────────────────────────────────────── */
.stButton > button[kind="primary"],
.stDownloadButton > button {
    background: #2E72FF !important;
    color: #FFFFFF !important;
    border: 1px solid #2E72FF !important;
    border-radius: 6px !important;
    font-family: 'JetBrains Mono', monospace !important;
    font-size: 13px !important;
    font-weight: 500 !important;
    letter-spacing: 0.16em !important;
    text-transform: uppercase !important;
    padding: 1.0rem 2rem !important;
    transition: all 0.18s ease !important;
    box-shadow: 0 4px 24px rgba(46,114,255,0.28) !important;
    min-height: 52px !important;
}
.stButton > button[kind="primary"]:hover,
.stDownloadButton > button:hover {
    background: #4d86ff !important;
    border-color: #4d86ff !important;
    transform: translateY(-1px);
    color: #FFFFFF !important;
    box-shadow: 0 6px 30px rgba(46,114,255,0.45) !important;
}
.stButton > button[kind="primary"]:disabled {
    background: rgba(46,114,255,0.18) !important;
    border-color: transparent !important;
    color: rgba(237,235,228,0.45) !important;
    cursor: not-allowed !important;
    box-shadow: none !important;
    transform: none !important;
}

.stButton > button[kind="secondary"] {
    background: transparent !important;
    color: #EDEBE4 !important;
    border: 1px solid rgba(237,235,228,0.22) !important;
    border-radius: 6px !important;
    font-family: 'JetBrains Mono', monospace !important;
    font-size: 12px !important;
    font-weight: 500 !important;
    letter-spacing: 0.16em !important;
    text-transform: uppercase !important;
    padding: 0.9rem 1.6rem !important;
    min-height: 48px !important;
    box-shadow: none !important;
    transition: all 0.18s ease !important;
}
.stButton > button[kind="secondary"]:hover {
    border-color: #2E72FF !important;
    background: rgba(46,114,255,0.08) !important;
    color: #FFFFFF !important;
    box-shadow: none !important;
}
.stButton > button[kind="secondary"]:disabled {
    color: rgba(237,235,228,0.3) !important;
    border-color: rgba(237,235,228,0.1) !important;
    cursor: not-allowed !important;
}

/* ──────────────────────────────────────────────────────────
   TEXT INPUTS
   Let the BaseWeb wrapper own the border (avoids the double-border /
   pink accent that BaseWeb paints on focus). The inner <input> is
   transparent + borderless; the wrapper draws the blue focus ring.
   ────────────────────────────────────────────────────────── */
[data-testid="stTextInput"] div[data-baseweb="input"],
[data-testid="stTextInput"] div[data-baseweb="base-input"] {
    background: #12141A !important;
    border: 1px solid rgba(237,235,228,0.18) !important;
    border-radius: 6px !important;
    transition: border-color 0.15s ease, box-shadow 0.15s ease !important;
}
[data-testid="stTextInput"] div[data-baseweb="input"]:hover {
    border-color: rgba(237,235,228,0.34) !important;
}
[data-testid="stTextInput"] div[data-baseweb="input"]:focus-within {
    border-color: #2E72FF !important;
    box-shadow: 0 0 0 3px rgba(46,114,255,0.22) !important;
}
[data-testid="stTextInput"] input {
    background: transparent !important;
    color: #F5F3EC !important;
    -webkit-text-fill-color: #F5F3EC !important;
    caret-color: #2E72FF !important;
    border: none !important;
    outline: none !important;
    box-shadow: none !important;
    font-family: 'JetBrains Mono', monospace !important;
    font-size: 14px !important;
    letter-spacing: 0.04em !important;
    padding: 0.95rem 1rem !important;
}
[data-testid="stTextInput"] input::placeholder {
    color: #6f6a5d !important;
    -webkit-text-fill-color: #6f6a5d !important;
    opacity: 1 !important;
}
/* Kill Chrome autofill's yellow box / colour bleed */
[data-testid="stTextInput"] input:-webkit-autofill,
[data-testid="stTextInput"] input:-webkit-autofill:hover,
[data-testid="stTextInput"] input:-webkit-autofill:focus {
    -webkit-text-fill-color: #F5F3EC !important;
    -webkit-box-shadow: 0 0 0 1000px #12141A inset !important;
    caret-color: #2E72FF !important;
    transition: background-color 9999s ease-in-out 0s !important;
}

/* Tabs */
.stTabs [data-baseweb="tab-list"] { gap: 4px; border-bottom: 1px solid rgba(237,235,228,0.1); }
.stTabs [data-baseweb="tab"] {
    font-family:'JetBrains Mono',monospace; font-size:11px; letter-spacing:0.2em;
    text-transform:uppercase; color:#9a9488; background:transparent;
}
.stTabs [aria-selected="true"] { color:#2E72FF !important; }
.stTabs [data-baseweb="tab-highlight"] { background:#2E72FF !important; }

/* File uploader */
[data-testid="stFileUploader"] section {
    background: transparent !important;
    border: 1px dashed rgba(237,235,228,0.22) !important;
    border-radius: 6px !important;
    padding: 3.4rem 2rem !important;
}
[data-testid="stFileUploader"] section:hover { border-color: #2E72FF !important; }
[data-testid="stFileUploader"] section * {
    font-family: 'JetBrains Mono', monospace !important;
    font-size: 11px !important; letter-spacing: 0.2em !important;
    text-transform: uppercase !important; color: #cfc9bd !important;
}
[data-testid="stFileUploader"] section button {
    background: transparent !important; color: #EDEBE4 !important;
    border: 1px solid rgba(237,235,228,0.3) !important; border-radius: 4px !important;
    box-shadow:none !important;
}
[data-testid="stFileUploaderDropzoneInstructions"] svg { display: none; }

/* Progress bars */
[data-testid="stProgress"] > div > div {
    background: rgba(237,235,228,0.08) !important; border-radius: 99px !important; height: 4px !important;
}
[data-testid="stProgress"] > div > div > div > div {
    background: linear-gradient(90deg,#2E72FF,#5B93FF) !important; border-radius: 99px !important;
}

/* Utility classes */
.mono { font-family: 'JetBrains Mono', monospace; font-size: 11px; letter-spacing: 0.22em; text-transform: uppercase; color: #9a9488; font-weight: 400; }
.mono-bright { font-family: 'JetBrains Mono', monospace; font-size: 11px; letter-spacing: 0.22em; text-transform: uppercase; color: #FFFFFF; font-weight: 500; }
.serif-italic { font-family: 'Cormorant Garamond', serif; font-style: italic; font-weight: 500; color: #c4bfb3; }
.serif-body { font-family: 'Cormorant Garamond', serif; font-weight: 500; line-height: 1.65; color: #d9d6cc; font-size: 1.18rem; }
.bp-blue { color: #2E72FF; }
.req { color: #2E72FF; font-family:'JetBrains Mono',monospace; }

.corner { position: absolute; width: 16px; height: 16px; border: 1px solid rgba(46,114,255,0.6); }
.corner.tl { top: 0; left: 0; border-right: none; border-bottom: none; }
.corner.tr { top: 0; right: 0; border-left: none; border-bottom: none; }
.corner.bl { bottom: 0; left: 0; border-right: none; border-top: none; }
.corner.br { bottom: 0; right: 0; border-left: none; border-top: none; }

/* Film grain */
body::before {
    content: ''; position: fixed; inset: 0; pointer-events: none; z-index: 9999;
    opacity: 0.10; mix-blend-mode: overlay;
    background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 200 200'><filter id='n'><feTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='3' stitchTiles='stitch'/><feColorMatrix values='0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0.5 0'/></filter><rect width='100%' height='100%' filter='url(%23n)'/></svg>");
}

.hero-h1 {
    font-family: 'Cormorant Garamond', serif;
    font-size: clamp(3rem, 8vw, 7rem);
    line-height: 0.94; color: #F5F3EC; letter-spacing: -0.01em; margin: 0;
}
.hero-h1 .it { font-style: italic; color: #c4bfb3; }

.banner-bar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 2.6rem; }

/* Card surface used on the details / form screen */
.form-card {
    background: linear-gradient(180deg, rgba(18,20,26,0.9), rgba(12,14,18,0.9));
    border: 1px solid rgba(237,235,228,0.10);
    border-radius: 12px;
    padding: 30px 32px;
}
</style>
"""

# ──────────────────────────────────────────────────────────
# BRANDING
# ──────────────────────────────────────────────────────────
BYTEPLUS_LOGO_URL = (
    "https://assets.byteplus.com/obj/byteplus-assets/navigation/byteplus_logo_footer.svg"
)


def byteplus_logo(height: int = 30) -> str:
    fs = int(height * 0.7)
    return (
        f'<a href="?home=1" target="_self" title="Start over"'
        f'   style="text-decoration:none;cursor:pointer;display:inline-block">'
        f'<div style="display:flex;align-items:center;gap:12px">'
        f'  <img src="{BYTEPLUS_LOGO_URL}" alt="BytePlus" '
        f'       style="height:{height}px;width:auto;display:block" />'
        f'  <span style="font-family:Inter,sans-serif;font-weight:700;'
        f'font-size:{fs}px;letter-spacing:-0.01em;color:#FFFFFF">BytePlus</span>'
        f'</div>'
        f'</a>'
    )


def render_header(step: int | None = None):
    steps = ["THE SUBJECT", "THE WORLD", "YOUR DETAILS", "GENERATION"]
    if step is not None:
        right = (
            f'<div style="text-align:right">'
            f'  <div class="mono">STEP <span class="mono-bright">{step:02d}</span>'
            f' <span style="opacity:0.5">/ 04</span></div>'
            f'  <div class="mono-bright" style="margin-top:4px">{steps[step-1]}</div>'
            f'</div>'
        )
    else:
        right = (
            f'<div style="text-align:right">'
            f'  <div class="mono">15s FILM · {RESOLUTION.upper()} · 9:16</div>'
            f'  <div class="mono" style="margin-top:4px">AP-SOUTHEAST · EST. 2026</div>'
            f'</div>'
        )
    st.markdown(
        '<div class="banner-bar">'
        '  <div style="display:flex;flex-direction:column;gap:6px">'
        f'    {byteplus_logo(30)}'
        '    <div class="mono" style="margin-left:42px">SEEDANCE STUDIO</div>'
        '  </div>'
        f'  {right}'
        '</div>',
        unsafe_allow_html=True,
    )


# ──────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────
def b64(data: bytes, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def field_label(text: str, required: bool = True) -> str:
    star = ' <span class="req">*</span>' if required else ''
    st.markdown(f'<div class="mono" style="margin-bottom:8px">{text}{star}</div>',
                unsafe_allow_html=True)


def fmt_mmss(seconds: float | int | None) -> str:
    """Seconds → MM:SS."""
    s = max(0, int(seconds or 0))
    return f"{s // 60:02d}:{s % 60:02d}"


def fmt_clock(ts: float | None) -> str:
    """Epoch timestamp → HH:MM:SS (server local time)."""
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def normalize_snap(snap: dict) -> dict:
    """Backfill timing keys so the UI never KeyErrors on a snapshot produced
    by an older code version still cached in st.cache_resource (e.g. a job
    created before a hot redeploy). Safe no-op for fresh jobs."""
    snap.setdefault("elapsed", 0.0)
    snap.setdefault("queue_wait", 0.0)
    snap.setdefault("total_elapsed", snap.get("elapsed", 0.0))
    snap.setdefault("created_at", None)
    snap.setdefault("started_at", None)
    snap.setdefault("finished_at", None)
    return snap


# ──────────────────────────────────────────────────────────
# SEEDANCE / TOS  (usage UNCHANGED)
# ──────────────────────────────────────────────────────────
def submit_seedance_task(prompt: str, refs: list[str]) -> str:
    content: list[dict] = [{"type": "text", "text": prompt}]
    for ref in refs:
        content.append({
            "type": "image_url",
            "image_url": {"url": ref},
            "role": "reference_image",
        })
    payload = {
        "model": MODEL_ID, "content": content,
        "generate_audio": True, "ratio": ASPECT_RATIO,
        "duration": CLIP_DURATION, "resolution": RESOLUTION, "watermark": False,
    }
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {ARK_API_KEY}"}
    r = httpx.post(ARK_ENDPOINT, json=payload, headers=headers, timeout=60)
    if r.status_code >= 400:
        raise RuntimeError(f"Seedance submit {r.status_code}: {r.text}\n--- payload refs: {refs}")
    data = r.json()
    return data.get("id") or data["task_id"]


def poll_seedance_task(task_id: str, on_progress=None) -> str:
    headers = {"Authorization": f"Bearer {ARK_API_KEY}"}
    url = f"{ARK_ENDPOINT}/{task_id}"
    start = time.time()
    while True:
        if time.time() - start > POLL_TIMEOUT:
            raise TimeoutError(f"Task {task_id} timed out")
        r = httpx.get(url, headers=headers, timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"Seedance poll {r.status_code}: {r.text}")
        data = r.json()
        status = data.get("status")
        if on_progress:
            on_progress(status, time.time() - start)
        if status == "succeeded":
            return (data.get("content") or {}).get("video_url") or data["content"]["url"]
        if status in ("failed", "cancelled"):
            raise RuntimeError(f"Task {task_id} {status}: {data.get('error', data)}")
        time.sleep(POLL_INTERVAL)


def _tos_client():
    import tos
    return tos.TosClientV2(TOS_ACCESS_KEY, TOS_SECRET_KEY, TOS_ENDPOINT, TOS_REGION)


def upload_to_tos(local_path: str, key: str, content_type: str = "application/octet-stream") -> str:
    import tos as _tos
    client = _tos_client()
    with open(local_path, "rb") as f:
        client.put_object(TOS_BUCKET, key, content=f, content_type=content_type)
    out = client.pre_signed_url(
        _tos.HttpMethodType.Http_Method_Get,
        TOS_BUCKET,
        key,
        expires=86400,
    )
    return out.signed_url


def upload_bytes_to_tos(data: bytes, key: str, content_type: str) -> str:
    with tempfile.NamedTemporaryFile(delete=False, suffix=Path(key).suffix) as tmp:
        tmp.write(data)
        path = tmp.name
    try:
        return upload_to_tos(path, key, content_type)
    finally:
        os.unlink(path)


def presigned_download_url(key: str, filename: str = "seedance_film.mp4",
                           expires: int = 86400) -> str:
    """Presigned GET URL that forces a file download (attachment) — needed so
    the email button saves the .mp4 on phones instead of just playing it."""
    import tos as _tos
    client = _tos_client()
    return client.pre_signed_url(
        _tos.HttpMethodType.Http_Method_Get, TOS_BUCKET, key, expires=expires,
        query={"response-content-disposition": f'attachment; filename="{filename}"'},
    ).signed_url


def add_watermark(in_path: str, out_path: str) -> str:
    """Burn the BytePlus logo into the bottom-right of the clip with ffmpeg.
    Falls back to the original clip if anything goes wrong (never fails a job)."""
    if not WATERMARK_ENABLED:
        return in_path
    m = WATERMARK_MARGIN
    filt = (
        f"[1:v]format=rgba,colorchannelmixer=aa={WATERMARK_OPACITY},"
        f"scale={WATERMARK_WIDTH}:-1[wm];"
        f"[0:v][wm]overlay=W-w-{m}:H-h-{m}"
    )
    cmd = [
        "ffmpeg", "-y", "-i", in_path, "-i", WATERMARK_PATH,
        "-filter_complex", filt,
        "-c:a", "copy", "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "20", "-movflags", "+faststart", out_path,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=180)
        return out_path
    except Exception:
        return in_path


def list_final_videos(limit: int = 120) -> list[dict]:
    """Every finished film, read straight from TOS so the gallery survives app
    restarts and link expiry. Returns newest-first:
    [{"theme_id": str, "url": fresh-presigned, "modified": datetime|None}]."""
    import tos as _tos
    client = _tos_client()
    prefix = "seedance/final/"

    raw: list[tuple[str, Any]] = []
    token = None
    while True:
        out = client.list_objects_type2(
            TOS_BUCKET, prefix=prefix, max_keys=1000, continuation_token=token
        )
        for o in (out.contents or []):
            if o.key.endswith(".mp4"):
                raw.append((o.key, getattr(o, "last_modified", None)))
        if getattr(out, "is_truncated", False) and getattr(out, "next_continuation_token", ""):
            token = out.next_continuation_token
        else:
            break

    raw.sort(key=lambda x: (x[1] is not None, x[1]), reverse=True)
    films: list[dict] = []
    for key, modified in raw[:limit]:
        fname = key[len(prefix):]
        theme_id = fname.rsplit("_", 1)[0]          # "{theme-id}_{uuid}.mp4"
        signed = client.pre_signed_url(
            _tos.HttpMethodType.Http_Method_Get, TOS_BUCKET, key, expires=86400,
        ).signed_url
        films.append({"theme_id": theme_id, "url": signed, "modified": modified})
    return films


# ──────────────────────────────────────────────────────────
# MODELARK ASSET LIBRARY  (AK/SK signing UNCHANGED)
# ──────────────────────────────────────────────────────────
ARK_OPENAPI_HOST = "ark.ap-southeast-1.byteplusapi.com"
ARK_SERVICE = "ark"


def _sign_ark_request(method: str, query: dict, body_str: str) -> tuple[str, dict]:
    now = datetime.now(timezone.utc)
    x_date = now.strftime("%Y%m%dT%H%M%SZ")
    short_date = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(body_str.encode("utf-8")).hexdigest()

    canonical_query = "&".join(
        f"{quote(k, safe='-._~')}={quote(str(v), safe='-._~')}"
        for k, v in sorted(query.items())
    )

    headers_to_sign = {
        "content-type": "application/json",
        "host": ARK_OPENAPI_HOST,
        "x-content-sha256": payload_hash,
        "x-date": x_date,
    }
    signed_headers_list = sorted(headers_to_sign.keys())
    signed_headers = ";".join(signed_headers_list)
    canonical_headers = "".join(f"{k}:{headers_to_sign[k]}\n" for k in signed_headers_list)

    canonical_request = "\n".join([
        method,
        "/",
        canonical_query,
        canonical_headers,
        signed_headers,
        payload_hash,
    ])
    credential_scope = f"{short_date}/{ARK_REGION}/{ARK_SERVICE}/request"
    string_to_sign = "\n".join([
        "HMAC-SHA256",
        x_date,
        credential_scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])

    k1 = hmac.new(ARK_SK.encode("utf-8"), short_date.encode(), hashlib.sha256).digest()
    k2 = hmac.new(k1, ARK_REGION.encode(), hashlib.sha256).digest()
    k3 = hmac.new(k2, ARK_SERVICE.encode(), hashlib.sha256).digest()
    signing_key = hmac.new(k3, b"request", hashlib.sha256).digest()
    signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()

    authorization = (
        f"HMAC-SHA256 Credential={ARK_AK}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, "
        f"Signature={signature}"
    )

    url = f"https://{ARK_OPENAPI_HOST}/?{canonical_query}"
    headers = {
        "Content-Type": "application/json",
        "Host": ARK_OPENAPI_HOST,
        "X-Date": x_date,
        "X-Content-Sha256": payload_hash,
        "Authorization": authorization,
    }
    return url, headers


def _ark_call(action: str, body: dict) -> dict:
    if not (ARK_AK and ARK_SK):
        raise RuntimeError("ARK_AK / ARK_SK not configured.")
    body_str = json.dumps(body, ensure_ascii=False)
    query = {"Action": action, "Version": "2024-01-01"}
    url, headers = _sign_ark_request("POST", query, body_str)
    r = httpx.post(url, content=body_str.encode("utf-8"), headers=headers, timeout=60)
    if r.status_code >= 400:
        raise RuntimeError(f"ModelArk {action} HTTP {r.status_code}: {r.text}")
    resp = r.json()
    if "Result" not in resp:
        meta = resp.get("ResponseMetadata") or resp
        err = (meta.get("Error") or {})
        raise RuntimeError(
            f"ModelArk {action} failed: "
            f"code={err.get('Code') or '?'} msg={err.get('Message') or meta}"
        )
    return resp["Result"]


def _ensure_asset_group_id() -> str:
    global ARK_ASSET_GROUP_ID
    if ARK_ASSET_GROUP_ID:
        return ARK_ASSET_GROUP_ID

    with _ASSET_GROUP_LOCK:
        if ARK_ASSET_GROUP_ID:
            return ARK_ASSET_GROUP_ID
        try:
            result = _ark_call("ListAssetGroups", {
                "Filter": {"Name": ARK_ASSET_GROUP_NAME, "GroupType": "AIGC"},
                "PageNumber": 1, "PageSize": 10,
                "ProjectName": ARK_PROJECT_NAME,
            })
            items = result.get("Items") or []
            for item in items:
                if item.get("Name") == ARK_ASSET_GROUP_NAME:
                    ARK_ASSET_GROUP_ID = item["Id"]
                    return ARK_ASSET_GROUP_ID
        except Exception:
            pass

        result = _ark_call("CreateAssetGroup", {
            "Name": ARK_ASSET_GROUP_NAME,
            "Description": "Customer portrait subjects uploaded via Seedance Studio",
            "ProjectName": ARK_PROJECT_NAME,
        })
        ARK_ASSET_GROUP_ID = result["Id"]
        return ARK_ASSET_GROUP_ID


def upload_to_asset_library(image_url: str, name: str | None = None,
                            on_step=None) -> str:
    def _step(msg: str):
        if on_step:
            on_step(msg)

    _step("RESOLVING ASSET GROUP")
    group_id = _ensure_asset_group_id()

    _step("UPLOADING TO ASSET LIBRARY")
    payload = {
        "GroupId": group_id,
        "URL": image_url,
        "AssetType": "Image",
        "ProjectName": ARK_PROJECT_NAME,
    }
    if name:
        payload["Name"] = name[:64]
    created = _ark_call("CreateAsset", payload)
    asset_id = created["Id"]

    _step(f"PROCESSING ASSET · {asset_id[-8:]}")
    start = time.time()
    while True:
        if time.time() - start > ASSET_POLL_TIMEOUT:
            raise TimeoutError(
                f"Asset {asset_id} did not become Active in {ASSET_POLL_TIMEOUT}s "
                f"(still Processing). The asset library may be slow today."
            )
        got = _ark_call("GetAsset", {
            "Id": asset_id, "ProjectName": ARK_PROJECT_NAME,
        })
        status = got.get("Status")
        if status == "Active":
            _step(f"ASSET ACTIVE · {asset_id[-8:]}")
            return f"asset://{asset_id}"
        if status == "Failed":
            err = got.get("Error") or {}
            raise RuntimeError(
                f"Asset {asset_id} preprocessing failed: "
                f"{err.get('Code')} — {err.get('Message')}. "
                f"This usually means the photo doesn't meet the asset library "
                f"content guidelines (e.g. resembles a real public figure, or "
                f"contains restricted content)."
            )
        elapsed = int(time.time() - start)
        _step(f"PROCESSING ASSET · {asset_id[-8:]} · {elapsed}s")
        time.sleep(ASSET_POLL_INTERVAL)


def download_url(url: str, dest: str) -> None:
    with httpx.stream("GET", url, timeout=120, follow_redirects=True) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)


# ──────────────────────────────────────────────────────────
# VALIDATION
# ──────────────────────────────────────────────────────────
EMAIL_RE = re.compile(r"^[\w\.\+\-]+@[\w\-]+(\.[\w\-]+)+$")
PHONE_RE = re.compile(r"^\+?\d{8,15}$")


def is_valid_email(addr: str) -> bool:
    return bool(EMAIL_RE.match((addr or "").strip()))


def is_valid_phone(num: str) -> bool:
    """Lenient international check: strip spaces/dashes/brackets, then require
    8–15 digits with an optional leading +. Works for VN (+84) and others."""
    cleaned = re.sub(r"[\s\-().]", "", (num or "").strip())
    return bool(PHONE_RE.match(cleaned))


# ──────────────────────────────────────────────────────────
# EMAIL DELIVERY  (multi-film collection)
# ──────────────────────────────────────────────────────────
def send_films_email(to_email: str, name: str,
                     films: list[tuple[dict, str, str]]) -> tuple[bool, str]:
    if not EMAIL_ENABLED:
        return False, "Email is not configured on this server."
    if not is_valid_email(to_email):
        return False, "That doesn't look like a valid email address."
    if not films:
        return False, "No completed films to send."

    greeting = name.strip() or "there"
    n = len(films)
    subject = f"{greeting}, your {n} Seedance short film{'s' if n != 1 else ''} are ready"

    text_lines = [f"Hi {greeting},", "",
                  f"Your {n} short film{'s' if n != 1 else ''} are ready to watch."]
    for theme, watch_url, dl_url in films:
        text_lines += ["", f"{theme['name']} — {theme['tagline']}",
                       f"Watch: {watch_url}", f"Download: {dl_url}"]
    text_lines += ["", "Each film: 15 seconds · 9:16 · powered by BytePlus Seedance 2.0",
                   "", "— Seedance Studio · BytePlus"]
    text_body = "\n".join(text_lines)

    cards = ""
    for theme, watch_url, dl_url in films:
        sig = theme["signature"]
        cards += f"""
        <tr><td style="padding:0 0 18px 0">
          <table width="100%" cellpadding="0" cellspacing="0" border="0"
                 style="background:#0F1116;border-left:3px solid {sig}">
            <tr><td style="padding:20px 22px">
              <div style="font-family:'Courier New',monospace;font-size:10px;
                          letter-spacing:0.24em;color:{sig};text-transform:uppercase">
                {theme.get('genre','')}
              </div>
              <div style="font-family:Georgia,serif;font-size:24px;color:#FFFFFF;
                          margin-top:6px">{theme['name']}</div>
              <div style="font-family:Georgia,serif;font-style:italic;font-size:15px;
                          color:#b9b3a6;margin-top:4px">{theme['tagline']}</div>
              <div style="margin-top:16px">
                <a href="{dl_url}" target="_blank"
                   style="display:inline-block;background:{BP_BLUE};color:#FFFFFF;
                          padding:12px 24px;font-family:'Courier New',monospace;
                          font-size:12px;letter-spacing:0.2em;text-transform:uppercase;
                          text-decoration:none;border-radius:4px">&darr; Download .mp4</a>
                <a href="{watch_url}" target="_blank"
                   style="display:inline-block;margin-left:10px;background:transparent;
                          color:#cfc9bd;border:1px solid rgba(237,235,228,0.25);
                          padding:11px 22px;font-family:'Courier New',monospace;
                          font-size:12px;letter-spacing:0.2em;text-transform:uppercase;
                          text-decoration:none;border-radius:4px">Watch &rarr;</a>
              </div>
            </td></tr>
          </table>
        </td></tr>"""

    html_body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#07080A;font-family:Georgia,serif;color:#EDEBE4">
  <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#07080A;padding:48px 16px">
    <tr><td align="center">
      <table width="560" cellpadding="0" cellspacing="0" border="0" style="max-width:560px">
        <tr><td style="padding-bottom:28px">
          <span style="font-family:Inter,Arial,sans-serif;font-weight:700;font-size:20px;color:#F5F3EC">
            Byte<span style="color:{BP_BLUE}">Plus</span></span>
          <span style="font-family:'Courier New',monospace;font-size:10px;letter-spacing:0.24em;
                       color:#9a9488;text-transform:uppercase;margin-left:10px">Seedance Studio</span>
        </td></tr>
        <tr><td style="padding-bottom:8px">
          <div style="font-family:Georgia,serif;font-size:38px;line-height:1.05;color:#FFFFFF">
            {greeting}, your films are ready.</div>
        </td></tr>
        <tr><td style="padding-bottom:32px">
          <p style="font-family:Georgia,serif;font-size:17px;line-height:1.6;color:#cfc9bd;margin:0">
            We generated {n} cinematic short film{'s' if n != 1 else ''} from your portrait —
            each a different world. Links work for the next 24 hours.
          </p>
        </td></tr>
        {cards}
        <tr><td style="border-top:1px solid rgba(237,235,228,0.1);padding-top:22px">
          <div style="font-family:'Courier New',monospace;font-size:10px;letter-spacing:0.26em;
                      color:#9a9488;text-transform:uppercase">
            00:15 EACH &nbsp;·&nbsp; {RESOLUTION.upper()} &nbsp;·&nbsp; 9:16
          </div>
        </td></tr>
      </table>
      <div style="font-family:Georgia,serif;font-style:italic;font-size:13px;color:#5f5a51;padding-top:22px">
        Seedance Studio · powered by BytePlus Seedance 2.0
      </div>
    </td></tr>
  </table>
</body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr((SMTP_FROM_NAME, SMTP_FROM_EMAIL))
    msg["To"] = to_email
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.ehlo()
            s.starttls()
            s.ehlo()
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.send_message(msg)
        return True, f"Sent to {to_email}"
    except smtplib.SMTPAuthenticationError:
        return False, "Email auth failed — check SMTP_USER / SMTP_PASSWORD."
    except Exception as e:
        return False, f"Couldn't send email: {e}"


# ──────────────────────────────────────────────────────────
# SESSION STATE
# ──────────────────────────────────────────────────────────
def init_state():
    defaults = {
        "step": "welcome",
        "photo_bytes": None,
        "photo_name": None,
        "photo_remote_url": None,
        "customer_name": "",
        "customer_phone": "",
        "customer_email": "",
        "theme_id": None,
        "job_id": None,
        "error": None,
        "detail_errors": {},     # {"name": "...", "phone": "...", "email": "..."}
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def goto(step: str):
    st.session_state.step = step
    st.rerun()


# ══════════════════════════════════════════════════════════════════════
# CONCURRENCY — background job queue shared across ALL user sessions
# ══════════════════════════════════════════════════════════════════════
MAX_CONCURRENT_GENERATIONS = int(os.environ.get("MAX_CONCURRENT_GENERATIONS", "6"))
MAX_QUEUE_WORKERS = int(os.environ.get("MAX_QUEUE_WORKERS", "128"))

_ASSET_GROUP_LOCK = threading.Lock()


@dataclass
class Job:
    job_id: str
    photo_bytes: bytes
    photo_name: str
    theme_id: str
    customer_name: str
    customer_phone: str
    customer_email: str
    status: str = "QUEUED"          # QUEUED | RUNNING | DONE | FAILED
    step: str = "WAITING IN QUEUE"
    progress: float = 0.0
    final_url: str | None = None
    final_key: str | None = None
    error: str | None = None
    email_status: str = "PENDING"   # PENDING | SENT | SKIPPED | FAILED: <msg>
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def set(self, **kwargs):
        with self.lock:
            for k, v in kwargs.items():
                setattr(self, k, v)

    def snapshot(self) -> dict:
        with self.lock:
            now = time.time()
            # render time = time spent actually generating (started → finished/now)
            render_elapsed = (self.finished_at or now) - self.started_at \
                if self.started_at else 0.0
            # total time = whole journey since submission (incl. queue wait)
            total_elapsed = (self.finished_at or now) - self.created_at
            # how long it waited in the queue before a slot opened
            queue_wait = (self.started_at or now) - self.created_at
            return {
                "job_id": self.job_id,
                "photo_name": self.photo_name,
                "theme_id": self.theme_id,
                "customer_name": self.customer_name,
                "customer_phone": self.customer_phone,
                "customer_email": self.customer_email,
                "status": self.status,
                "step": self.step,
                "progress": self.progress,
                "final_url": self.final_url,
                "error": self.error,
                "email_status": self.email_status,
                "created_at": self.created_at,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "elapsed": render_elapsed,        # render time (started → done)
                "total_elapsed": total_elapsed,   # incl. queue wait
                "queue_wait": queue_wait,
            }


class JobQueue:
    def __init__(self):
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self.executor = ThreadPoolExecutor(
            max_workers=MAX_QUEUE_WORKERS, thread_name_prefix="seedance-job"
        )
        self.gen_semaphore = threading.Semaphore(MAX_CONCURRENT_GENERATIONS)

    def submit(self, photo_bytes: bytes, photo_name: str, theme_id: str,
               customer_name: str, customer_phone: str, customer_email: str) -> str:
        job_id = (
            f"job-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
        )
        job = Job(
            job_id=job_id, photo_bytes=photo_bytes, photo_name=photo_name,
            theme_id=theme_id, customer_name=customer_name,
            customer_phone=customer_phone, customer_email=customer_email,
        )
        with self.lock:
            self.jobs[job_id] = job
        self.executor.submit(self._run, job)
        return job_id

    def get(self, job_id: str | None) -> Job | None:
        if not job_id:
            return None
        return self.jobs.get(job_id)

    def active_count(self) -> int:
        with self.lock:
            return sum(1 for j in self.jobs.values()
                       if j.status in ("QUEUED", "RUNNING"))

    def _run(self, job: Job):
        if DEMO_MODE:
            return self._run_demo(job)

        if not self.gen_semaphore.acquire(blocking=False):
            job.set(status="QUEUED", step="WAITING IN QUEUE FOR A SLOT")
            self.gen_semaphore.acquire()
        try:
            self._generate(job)
        finally:
            self.gen_semaphore.release()

        self._email(job)

    def _generate(self, job: Job):
        try:
            theme = THEMES[job.theme_id]
            job.set(status="RUNNING", started_at=time.time(),
                    step="REGISTERING PROTAGONIST", progress=0.03)
            workdir = Path(tempfile.mkdtemp(prefix=f"seedance_{job.job_id}_"))

            def step(msg: str):
                job.set(step=msg)

            step("STAGING PHOTO TO TOS")
            key = f"seedance/subjects/{uuid.uuid4().hex}.jpg"
            tos_url = upload_bytes_to_tos(job.photo_bytes, key, content_type="image/jpeg")
            if USE_ASSET_LIBRARY:
                asset_name = (
                    f"subject_{datetime.now().strftime('%Y%m%d_%H%M%S')}_"
                    f"{uuid.uuid4().hex[:6]}"
                )
                character_ref = upload_to_asset_library(tos_url, name=asset_name, on_step=step)
            else:
                character_ref = tos_url
            job.set(progress=0.15)

            step("PREPARING REFERENCES")
            refs: list[str] = [character_ref]
            if theme.get("set_plate_url"):
                stp = upload_to_asset_library(theme["set_plate_url"], name="set_plate",
                                              on_step=step) if USE_ASSET_LIBRARY \
                    else theme["set_plate_url"]
                refs.append(stp)
            if theme.get("secondary_character_url"):
                sec = upload_to_asset_library(theme["secondary_character_url"],
                                              name="secondary", on_step=step) \
                    if USE_ASSET_LIBRARY else theme["secondary_character_url"]
                refs.append(sec)
            job.set(progress=0.22)

            step(f"SUBMITTING · {len(refs)} REFS")
            task_id = submit_seedance_task(prompt=theme["prompt"], refs=refs)
            job.set(step=f"GENERATING · {task_id[-8:]}", progress=0.30)

            def on_progress(status, elapsed):
                clip_pct = min(0.95, 1 - (0.5 ** (elapsed / 30)))
                job.set(progress=0.30 + clip_pct * 0.55)

            video_url = poll_seedance_task(task_id, on_progress=on_progress)

            step("DOWNLOADING FILM")
            job.set(progress=0.90)
            clip_path = str(workdir / "film.mp4")
            download_url(video_url, clip_path)
            if WATERMARK_ENABLED:
                step("BRANDING")
                clip_path = add_watermark(clip_path, str(workdir / "film_branded.mp4"))
            step("PUBLISHING")
            job.set(progress=0.95)
            mkey = f"seedance/final/{job.theme_id}_{uuid.uuid4().hex}.mp4"
            final_url = upload_to_tos(clip_path, mkey, content_type="video/mp4")

            job.set(status="DONE", step="COMPLETE", progress=1.0,
                    final_url=final_url, final_key=mkey, finished_at=time.time())
        except Exception as e:
            job.set(status="FAILED", step=f"ERROR: {type(e).__name__}",
                    error=str(e), finished_at=time.time())

    def _email(self, job: Job):
        if job.status != "DONE" or not job.final_url:
            return
        if not EMAIL_ENABLED:
            job.set(email_status="SKIPPED")
            return
        try:
            theme = THEMES[job.theme_id]
            fname = f"{theme['name'].replace(' ', '_')}_BytePlus_Seedance.mp4"
            try:
                download_url = (
                    presigned_download_url(job.final_key, filename=fname)
                    if job.final_key else job.final_url
                )
            except Exception:
                download_url = job.final_url
            ok, msg = send_films_email(
                job.customer_email, job.customer_name,
                [(theme, job.final_url, download_url)],
            )
            job.set(email_status="SENT" if ok else f"FAILED: {msg}")
        except Exception as e:
            job.set(email_status=f"FAILED: {e}")

    def _run_demo(self, job: Job):
        job.set(status="RUNNING", started_at=time.time(), step="DEMO · SIMULATING")
        for tick in range(40):
            job.set(progress=min(0.99, (tick + 1) / 40))
            time.sleep(0.15)
        job.set(status="DONE", step="COMPLETE (DEMO)", progress=1.0,
                finished_at=time.time(), email_status="SKIPPED")


@st.cache_resource
def get_job_queue() -> JobQueue:
    return JobQueue()


# ──────────────────────────────────────────────────────────
# SCREEN — WELCOME
# ──────────────────────────────────────────────────────────
def render_welcome():
    render_header(None)

    st.markdown('<div class="mono" style="margin-bottom:28px">ONE PHOTOGRAPH. ONE CINEMATIC WORLD.</div>', unsafe_allow_html=True)
    st.markdown(
        '<h1 class="hero-h1">'
        'Become<br>'
        '<span class="it">the protagonist</span><br>'
        'of your own<br>'
        f'<span style="color:{BP_BLUE}">— cinema.</span>'
        '</h1>',
        unsafe_allow_html=True,
    )
    st.markdown(
        '<p class="serif-body" style="max-width:560px;margin-top:2.4rem;font-size:1.2rem">'
        "Upload a single portrait — or take one now. Choose one of four cinematic "
        "worlds and we&rsquo;ll cast you as the lead in a 15-second short film, "
        "delivered straight to your inbox."
        f'<br><span class="mono" style="margin-top:14px;display:inline-block">1 FILM · 00:15 · {RESOLUTION.upper()} · 9:16</span>'
        '</p>',
        unsafe_allow_html=True,
    )

    st.markdown("<div style='height:44px'></div>", unsafe_allow_html=True)

    cols = st.columns([1.3, 1.6, 4])
    with cols[0]:
        if st.button("Begin →", key="begin", type="primary", use_container_width=True):
            goto("capture")
    with cols[1]:
        if st.button("View gallery →", key="welcome_gallery", type="secondary",
                     use_container_width=True):
            goto("gallery")

    if DEMO_MODE:
        st.markdown(
            '<div class="mono" style="margin-top:44px;padding:12px 16px;border:1px solid rgba(245,184,0,0.3);border-radius:6px;display:inline-block">'
            '<span style="color:#f5b800">● DEMO MODE</span>'
            '<span style="margin-left:16px;color:#cfc9bd">Set ARK_API_KEY + TOS_* env vars to enable real generation</span>'
            '</div>',
            unsafe_allow_html=True,
        )
    elif USE_ASSET_LIBRARY:
        st.markdown(
            f'<div class="mono" style="margin-top:44px;padding:12px 16px;border:1px solid rgba(46,114,255,0.4);border-radius:6px;display:inline-block">'
            f'<span style="color:{BP_BLUE}">● ASSET LIBRARY ACTIVE</span>'
            '<span style="margin-left:16px;color:#cfc9bd">Real-face photos enabled via BytePlus ModelArk</span>'
            '</div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            '<div class="mono" style="margin-top:44px;padding:12px 16px;border:1px solid rgba(245,184,0,0.3);border-radius:6px;display:inline-block">'
            '<span style="color:#f5b800">● FACE-FREE MODE</span>'
            '<span style="margin-left:16px;color:#cfc9bd">Set ARK_AK + ARK_SK to enable real-face uploads</span>'
            '</div>',
            unsafe_allow_html=True,
        )


# ──────────────────────────────────────────────────────────
# SCREEN — CAPTURE (upload OR take photo)
# ──────────────────────────────────────────────────────────
def render_capture():
    render_header(1)
    cols = st.columns([6, 1])
    with cols[1]:
        if st.button("← Back", key="cap_back", type="secondary"):
            goto("welcome")

    st.markdown("<div style='height:36px'></div>", unsafe_allow_html=True)

    left, right = st.columns([1, 1], gap="large")

    with left:
        st.markdown(
            '<h2 style="font-size:3.4rem;line-height:0.96;margin:0">'
            'One portrait.<br><span class="serif-italic" style="font-size:3.4rem">That&rsquo;s all we need.</span>'
            '</h2>'
            '<p class="serif-body" style="margin-top:1.8rem;max-width:420px">'
            "Best results from a clear, well-lit photo of one person facing the camera. "
            "We&rsquo;ll use this as the reference for every shot of your film."
            '</p>'
            '<div style="margin-top:2.2rem;display:grid;grid-template-columns:1fr 1fr;gap:12px 32px">'
            '<div class="mono">→ ONE PERSON, CENTER</div>'
            '<div class="mono">→ CLEAR LIGHTING</div>'
            '<div class="mono">→ JPG / PNG / WEBP</div>'
            '<div class="mono">→ 1024×1024 OR LARGER</div>'
            '</div>',
            unsafe_allow_html=True,
        )

    with right:
        if st.session_state.photo_bytes is None:
            tab_upload, tab_camera = st.tabs(["UPLOAD", "TAKE A PHOTO"])
            with tab_upload:
                uploaded = st.file_uploader(
                    "Drop a photograph",
                    type=["jpg", "jpeg", "png", "webp"],
                    key="photo_upload",
                    label_visibility="collapsed",
                )
                if uploaded is not None:
                    st.session_state.photo_bytes = uploaded.read()
                    st.session_state.photo_name = uploaded.name
                    st.rerun()
            with tab_camera:
                snap = st.camera_input("Take a photo", key="photo_camera",
                                       label_visibility="collapsed")
                if snap is not None:
                    st.session_state.photo_bytes = snap.getvalue()
                    st.session_state.photo_name = (
                        f"camera_{datetime.now().strftime('%H%M%S')}.jpg"
                    )
                    st.rerun()
        else:
            img_b64 = b64(st.session_state.photo_bytes)
            size_kb = len(st.session_state.photo_bytes) // 1024
            st.markdown(
                f'<div style="position:relative;width:100%;aspect-ratio:3/4;max-width:420px">'
                f'  <div class="mono" style="position:absolute;top:-22px;left:0;right:0;display:flex;justify-content:space-between;color:#cfc9bd">'
                f'    <span>+</span><span>9:16 OUTPUT</span><span>+</span>'
                f'  </div>'
                f'  <img src="{img_b64}" style="width:100%;height:100%;object-fit:cover;display:block;outline:1px solid rgba(46,114,255,0.4)" />'
                f'  <div class="corner tl"></div><div class="corner tr"></div>'
                f'  <div class="corner bl"></div><div class="corner br"></div>'
                f'  <div class="mono" style="display:flex;justify-content:space-between;margin-top:16px">'
                f'    <span class="mono-bright">SUBJECT_01</span>'
                f'    <span>{size_kb} KB</span>'
                f'  </div>'
                f'</div>',
                unsafe_allow_html=True,
            )
            if st.button("Replace photo", key="replace", type="secondary"):
                st.session_state.photo_bytes = None
                st.session_state.photo_name = None
                st.session_state.photo_remote_url = None
                st.rerun()

    st.markdown("<div style='height:56px'></div>", unsafe_allow_html=True)
    cols = st.columns([6, 1.6])
    with cols[1]:
        if st.button("Choose a world →", key="cap_next", type="primary",
                     use_container_width=True,
                     disabled=st.session_state.photo_bytes is None):
            goto("themes")


# ──────────────────────────────────────────────────────────
# SCREEN — THEMES (choose ONE world)
# ──────────────────────────────────────────────────────────
def _theme_select_card(theme: dict, selected: bool) -> str:
    border = (
        f"2px solid {theme['signature']}"
        if selected else "1px solid rgba(237,235,228,0.12)"
    )
    glow = (
        f"box-shadow:0 0 0 4px {theme['signature']}22;"
        if selected else ""
    )
    check = (
        f'<div style="position:absolute;top:16px;right:16px;width:26px;height:26px;'
        f'border-radius:50%;background:{theme["signature"]};display:flex;'
        f'align-items:center;justify-content:center;color:#07080A;'
        f'font-family:JetBrains Mono,monospace;font-weight:700;font-size:14px">✓</div>'
        if selected else ""
    )
    keywords_html = "&nbsp;&nbsp;·&nbsp;&nbsp;".join(theme["keywords"])
    return (
        f'<div style="position:relative;background:{theme["paper"]};border:{border};'
        f'{glow}border-radius:8px;overflow:hidden;margin-bottom:12px">'
        f'  {check}'
        f'  <div style="position:relative;height:150px;background:'
        f'radial-gradient(circle at 30% 30%,{theme["signature"]}55,transparent 55%),'
        f'radial-gradient(circle at 70% 70%,{theme["accent"]}55,transparent 55%),'
        f'{theme["paper"]}">'
        f'    <div style="position:absolute;top:16px;left:18px;font-family:JetBrains Mono,monospace;'
        f'font-size:11px;letter-spacing:0.26em;color:{theme["signature"]};font-weight:500">'
        f'THEME {theme["code"]} · {theme["genre"]}</div>'
        f'    <div style="position:absolute;bottom:16px;left:18px;right:18px">'
        f'      <div style="font-family:Cormorant Garamond,serif;font-weight:500;font-size:2.2rem;'
        f'color:#FFFFFF;line-height:0.95;letter-spacing:-0.01em">{theme["name"]}</div>'
        f'      <div style="margin-top:4px;font-family:Cormorant Garamond,serif;font-style:italic;'
        f'color:#e0dacc;font-size:1.05rem">{theme["tagline"]}</div>'
        f'    </div>'
        f'  </div>'
        f'  <div style="padding:16px 18px 10px">'
        f'    <p style="font-family:Cormorant Garamond,serif;font-weight:500;font-size:1.05rem;'
        f'line-height:1.5;color:#cfc9bd;margin:0">{theme["scene"]}</p>'
        f'    <div style="margin-top:12px;font-family:JetBrains Mono,monospace;font-size:10px;'
        f'letter-spacing:0.22em;text-transform:uppercase;color:#7e7869">{keywords_html}</div>'
        f'  </div>'
        f'</div>'
    )


def render_themes():
    render_header(2)
    cols = st.columns([6, 1])
    with cols[1]:
        if st.button("← Back", key="th_back", type="secondary"):
            goto("capture")

    st.markdown("<div style='height:24px'></div>", unsafe_allow_html=True)
    left, right = st.columns([1.5, 1])
    with left:
        st.markdown(
            '<h2 style="font-size:3.4rem;line-height:0.96;margin:0">'
            'Which world<br><span class="serif-italic" style="font-size:3.4rem">do you walk into?</span>'
            '</h2>',
            unsafe_allow_html=True,
        )
    with right:
        st.markdown(
            '<p class="serif-body" style="max-width:340px">'
            "Pick one. We&rsquo;ll cast you as the lead in a single 15-second short, "
            "with its own aesthetic, pace, and score."
            '</p>',
            unsafe_allow_html=True,
        )

    st.markdown("<div style='height:24px'></div>", unsafe_allow_html=True)

    theme_list = list(THEMES.items())
    selected = st.session_state.theme_id

    for row_start in (0, 2):
        c1, c2 = st.columns(2, gap="medium")
        for col, (tid, t) in zip((c1, c2), theme_list[row_start:row_start + 2]):
            with col:
                is_sel = (tid == selected)
                st.markdown(_theme_select_card(t, is_sel), unsafe_allow_html=True)
                label = "✓  Selected" if is_sel else f"Select {t['name']}"
                if st.button(label, key=f"sel_{tid}",
                             use_container_width=True,
                             type="primary" if is_sel else "secondary"):
                    st.session_state.theme_id = tid
                    st.rerun()
                st.markdown("<div style='height:20px'></div>", unsafe_allow_html=True)

    st.markdown("<div style='height:32px'></div>", unsafe_allow_html=True)
    cols = st.columns([6, 1.6])
    with cols[1]:
        label = "Continue →" if selected else "Pick a world"
        if st.button(label, key="th_next", type="primary", use_container_width=True,
                     disabled=(selected is None)):
            goto("details")


# ──────────────────────────────────────────────────────────
# SCREEN — DETAILS (name + phone + email — all required)
# ──────────────────────────────────────────────────────────
def render_details():
    render_header(3)
    cols = st.columns([6, 1])
    with cols[1]:
        if st.button("← Back", key="det_back", type="secondary"):
            goto("themes")

    st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)

    errs = st.session_state.detail_errors or {}
    left, right = st.columns([1, 1.15], gap="large")

    # ── LEFT: context — heading, chosen world, photo ──────────────
    with left:
        st.markdown(
            '<h2 style="font-size:3.2rem;line-height:0.98;margin:0">'
            'Where do we<br><span class="serif-italic" style="font-size:3.2rem">send your film?</span>'
            '</h2>'
            '<p class="serif-body" style="margin-top:1.6rem;max-width:420px">'
            "When your short film finishes rendering, we&rsquo;ll deliver it to your "
            "inbox automatically — so you don&rsquo;t need to wait on this page."
            '</p>',
            unsafe_allow_html=True,
        )

        chosen = THEMES.get(st.session_state.theme_id, {})
        if st.session_state.photo_bytes:
            st.markdown("<div style='height:18px'></div>", unsafe_allow_html=True)
            st.markdown(
                f'<div style="display:flex;gap:18px;align-items:center">'
                f'  <img src="{b64(st.session_state.photo_bytes)}" '
                f'style="width:104px;aspect-ratio:3/4;object-fit:cover;border-radius:6px;'
                f'outline:1px solid rgba(46,114,255,0.4);filter:grayscale(0.15) contrast(1.05)" />'
                f'  <div>'
                f'    <div class="mono">CHOSEN WORLD</div>'
                f'    <div style="font-family:Cormorant Garamond,serif;font-size:1.7rem;'
                f'color:#F5F3EC;line-height:1.05;margin-top:6px">{chosen.get("name","—")}</div>'
                f'    <div class="serif-italic" style="margin-top:4px;font-size:1rem">'
                f'{chosen.get("tagline","")}</div>'
                f'    <div style="display:flex;align-items:center;gap:8px;margin-top:10px">'
                f'      <div style="width:9px;height:9px;border-radius:50%;'
                f'background:{chosen.get("signature", BP_BLUE)}"></div>'
                f'      <div class="mono" style="font-size:10px">{chosen.get("genre","")}</div>'
                f'    </div>'
                f'  </div>'
                f'</div>',
                unsafe_allow_html=True,
            )

    # ── RIGHT: the form ───────────────────────────────────────────
    with right:
        st.markdown('<div class="mono" style="margin-bottom:18px;color:#cfc9bd">'
                    'YOUR DETAILS <span style="opacity:0.5">— ALL FIELDS REQUIRED</span></div>',
                    unsafe_allow_html=True)

        field_label("Full name")
        name = st.text_input("name", value=st.session_state.customer_name,
                             placeholder="e.g. Tan Nguyen", key="name_input",
                             label_visibility="collapsed")
        if errs.get("name"):
            st.markdown(f'<div class="mono" style="margin-top:6px;color:#FF6B6B">✗ {errs["name"]}</div>',
                        unsafe_allow_html=True)

        st.markdown("<div style='height:18px'></div>", unsafe_allow_html=True)
        field_label("Phone number")
        phone = st.text_input("phone", value=st.session_state.customer_phone,
                              placeholder="e.g. +84 90 123 4567", key="phone_input",
                              label_visibility="collapsed")
        if errs.get("phone"):
            st.markdown(f'<div class="mono" style="margin-top:6px;color:#FF6B6B">✗ {errs["phone"]}</div>',
                        unsafe_allow_html=True)

        st.markdown("<div style='height:18px'></div>", unsafe_allow_html=True)
        field_label("Email address")
        email = st.text_input("email", value=st.session_state.customer_email,
                              placeholder="you@example.com", key="email_input",
                              label_visibility="collapsed")
        if errs.get("email"):
            st.markdown(f'<div class="mono" style="margin-top:6px;color:#FF6B6B">✗ {errs["email"]}</div>',
                        unsafe_allow_html=True)

        if not EMAIL_ENABLED:
            st.markdown(
                '<div class="mono" style="margin-top:18px;color:#9a9488;line-height:1.6">'
                '● Delivery email isn&rsquo;t configured on this server — your film will '
                'still be shown here on screen.</div>',
                unsafe_allow_html=True,
            )

        st.markdown("<div style='height:26px'></div>", unsafe_allow_html=True)
        if st.button("Create my film →", key="det_next", type="primary",
                     use_container_width=True):
            new_errs: dict[str, str] = {}
            if not st.session_state.theme_id:
                new_errs["email"] = "Please go back and choose a world first."
            if not (name or "").strip():
                new_errs["name"] = "Please enter your name."
            if not (phone or "").strip():
                new_errs["phone"] = "Please enter your phone number."
            elif not is_valid_phone(phone):
                new_errs["phone"] = "Enter a valid phone number (8–15 digits, optional +)."
            if not (email or "").strip():
                new_errs["email"] = "Please enter your email address."
            elif not is_valid_email(email):
                new_errs["email"] = "That doesn't look like a valid email address."

            # Persist what they typed so it survives the rerun
            st.session_state.customer_name = (name or "").strip()
            st.session_state.customer_phone = (phone or "").strip()
            st.session_state.customer_email = (email or "").strip()

            if new_errs:
                st.session_state.detail_errors = new_errs
                st.rerun()
            else:
                st.session_state.detail_errors = {}
                q = get_job_queue()
                job_id = q.submit(
                    photo_bytes=st.session_state.photo_bytes,
                    photo_name=st.session_state.photo_name or "photo",
                    theme_id=st.session_state.theme_id,
                    customer_name=st.session_state.customer_name,
                    customer_phone=st.session_state.customer_phone,
                    customer_email=st.session_state.customer_email,
                )
                st.session_state.job_id = job_id
                try:
                    st.query_params["job"] = job_id
                except Exception:
                    pass
                goto("generating")


# ──────────────────────────────────────────────────────────
# SCREEN — GENERATING  (non-blocking; polls the background job)
# ──────────────────────────────────────────────────────────
def render_generating():
    q = get_job_queue()
    job = q.get(st.session_state.get("job_id"))
    render_header(4)

    if job is None:
        st.markdown("<div style='height:24px'></div>", unsafe_allow_html=True)
        st.error("We couldn't find your job — it may have expired after a server "
                 "restart. Please start again.")
        if st.button("← Start over", key="gen_restart", type="secondary"):
            _reset_session()
        return

    snap = normalize_snap(job.snapshot())
    theme = THEMES.get(snap["theme_id"], {})
    sig = theme.get("signature", BP_BLUE)

    if snap["status"] in ("DONE", "FAILED"):
        goto("result")

    st.markdown("<div style='height:24px'></div>", unsafe_allow_html=True)
    left, right = st.columns([1, 2.4], gap="large")

    with left:
        if job.photo_bytes:
            st.markdown(
                f'<img src="{b64(job.photo_bytes)}" '
                f'style="width:100%;aspect-ratio:3/4;object-fit:cover;border-radius:6px;'
                f'outline:1px solid rgba(46,114,255,0.3);'
                f'filter:grayscale(0.25) contrast(1.05)" />',
                unsafe_allow_html=True,
            )
        st.markdown(
            f'<div class="mono" style="margin-top:24px">CASTING</div>'
            f'<div style="font-family:Cormorant Garamond,serif;font-weight:500;'
            f'font-size:1.7rem;color:#F5F3EC;line-height:1;margin-top:8px">'
            f'{snap["customer_name"] or "You"}</div>'
            f'<div class="mono" style="margin-top:10px;color:#9a9488">'
            f'→ {snap["customer_email"]}</div>'
            f'<div style="display:flex;align-items:center;gap:10px;margin-top:16px">'
            f'  <div style="width:10px;height:10px;background:{sig}"></div>'
            f'  <div class="mono" style="font-size:11px">{theme.get("name","—")}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

    with right:
        dots = "." * ((int(time.time() * 2) % 3) + 1)
        queued = snap["status"] == "QUEUED"
        headline = "In&nbsp;the&nbsp;queue" if queued else "Rendering"
        pct = int(round(snap["progress"] * 100))
        # While queued the render clock hasn't started, so show the wait;
        # once running, show the live render time.
        big_clock = fmt_mmss(snap["queue_wait"] if queued else snap["elapsed"])
        big_label = "WAITING" if queued else "ELAPSED"

        st.markdown(
            f'<h2 style="font-family:Cormorant Garamond,serif;font-weight:500;'
            f'font-size:4.2rem;line-height:0.95;margin:0">{headline}'
            f'<span style="opacity:0.5">{dots}</span></h2>',
            unsafe_allow_html=True,
        )
        st.markdown(
            f'<div style="margin-top:20px;display:flex;align-items:baseline;gap:18px">'
            f'  <div class="mono">{big_label}</div>'
            f'  <div style="font-family:JetBrains Mono,monospace;font-size:2.6rem;'
            f'font-weight:500;color:{BP_BLUE};letter-spacing:0.04em">{big_clock}</div>'
            f'  <div class="mono" style="margin-left:auto;color:{sig}">● {snap["status"]}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )
        # Secondary timing line: total since submission + start time.
        st.markdown(
            f'<div style="margin-top:6px;display:flex;gap:26px;flex-wrap:wrap">'
            f'  <div class="mono" style="color:#7e7869">TOTAL · '
            f'<span style="color:#cfc9bd">{fmt_mmss(snap["total_elapsed"])}</span></div>'
            f'  <div class="mono" style="color:#7e7869">SUBMITTED · '
            f'<span style="color:#cfc9bd">{fmt_clock(snap["created_at"])}</span></div>'
            f'  <div class="mono" style="color:#7e7869">STARTED · '
            f'<span style="color:#cfc9bd">{fmt_clock(snap["started_at"])}</span></div>'
            f'</div>',
            unsafe_allow_html=True,
        )

        st.markdown("<div style='height:22px'></div>", unsafe_allow_html=True)
        st.markdown(
            f'<div style="display:flex;justify-content:space-between;margin-bottom:8px">'
            f'  <div class="mono">PROGRESS</div>'
            f'  <div class="mono" style="color:{BP_BLUE}">{pct:02d}%</div>'
            f'</div>',
            unsafe_allow_html=True,
        )
        st.progress(snap["progress"])
        st.markdown(
            f'<div class="mono" style="margin-top:12px;color:#9a9488">'
            f'→ {snap["step"]}</div>',
            unsafe_allow_html=True,
        )
        if queued:
            st.markdown(
                f'<div class="serif-italic" style="margin-top:18px;color:#9a9488">'
                f'Lots of films are rendering right now — yours will start as soon '
                f'as a slot frees up. You can safely leave this page; we&rsquo;ll '
                f'email it to <strong style="color:#cfc9bd">{snap["customer_email"]}</strong> '
                f'when it&rsquo;s ready.</div>',
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                f'<div class="serif-italic" style="margin-top:18px;color:#9a9488">'
                f'You can close this tab — your film will be emailed to '
                f'<strong style="color:#cfc9bd">{snap["customer_email"]}</strong> '
                f'when it&rsquo;s done.</div>',
                unsafe_allow_html=True,
            )

    time.sleep(2.0)
    st.rerun()


# ──────────────────────────────────────────────────────────
# SCREEN — RESULT (single film)
# ──────────────────────────────────────────────────────────
def render_result():
    q = get_job_queue()
    job = q.get(st.session_state.get("job_id"))
    render_header(None)

    if job is None:
        st.markdown("<div style='height:24px'></div>", unsafe_allow_html=True)
        st.error("We couldn't find your film — the job may have expired. "
                 "Please start again.")
        if st.button("← Start over", key="res_restart", type="secondary"):
            _reset_session()
        return

    snap = normalize_snap(job.snapshot())
    theme = THEMES.get(snap["theme_id"], {})
    name = snap["customer_name"] or "Your"
    poss = f"{name}'s" if not name.endswith("s") else f"{name}'"
    is_done = snap["status"] == "DONE" and snap["final_url"]
    is_failed = snap["status"] == "FAILED"

    cols = st.columns([3, 1])
    with cols[0]:
        st.markdown(
            f'<div class="mono">A SHORT FILM BY BYTEPLUS SEEDANCE</div>'
            f'<h2 style="font-size:4.6rem;line-height:1;margin:12px 0 0 0">'
            f'{theme.get("name","Your film")}</h2>'
            f'<div class="serif-italic" style="margin-top:8px;font-size:1.2rem">'
            f'{theme.get("tagline","")}</div>',
            unsafe_allow_html=True,
        )
    with cols[1]:
        sc = "#7BC47F" if is_done else ("#FF6B6B" if is_failed else "#f5b800")
        stt = "READY" if is_done else ("FAILED" if is_failed else "PREVIEW")
        st.markdown(
            f'<div class="mono" style="text-align:right;color:{sc}">● {stt}</div>'
            f'<div class="mono" style="text-align:right;margin-top:4px">'
            f'00:15 · {RESOLUTION.upper()} · 9:16</div>',
            unsafe_allow_html=True,
        )

    # ── Render timing strip ───────────────────────────────────────
    if snap["status"] in ("DONE", "FAILED"):
        stat_color = "#7BC47F" if is_done else "#FF6B6B"
        st.markdown(
            f'<div style="margin-top:22px;display:flex;gap:0;flex-wrap:wrap;'
            f'border:1px solid rgba(237,235,228,0.10);border-radius:8px;overflow:hidden">'
            f'  <div style="flex:1;min-width:150px;padding:16px 20px;'
            f'border-right:1px solid rgba(237,235,228,0.08)">'
            f'    <div class="mono" style="color:#7e7869">RENDER TIME</div>'
            f'    <div style="font-family:JetBrains Mono,monospace;font-size:1.7rem;'
            f'font-weight:500;color:{stat_color};margin-top:6px">{fmt_mmss(snap["elapsed"])}</div>'
            f'  </div>'
            f'  <div style="flex:1;min-width:150px;padding:16px 20px;'
            f'border-right:1px solid rgba(237,235,228,0.08)">'
            f'    <div class="mono" style="color:#7e7869">QUEUE WAIT</div>'
            f'    <div style="font-family:JetBrains Mono,monospace;font-size:1.7rem;'
            f'font-weight:500;color:#cfc9bd;margin-top:6px">{fmt_mmss(snap["queue_wait"])}</div>'
            f'  </div>'
            f'  <div style="flex:1;min-width:150px;padding:16px 20px;'
            f'border-right:1px solid rgba(237,235,228,0.08)">'
            f'    <div class="mono" style="color:#7e7869">TOTAL</div>'
            f'    <div style="font-family:JetBrains Mono,monospace;font-size:1.7rem;'
            f'font-weight:500;color:#cfc9bd;margin-top:6px">{fmt_mmss(snap["total_elapsed"])}</div>'
            f'  </div>'
            f'  <div style="flex:1;min-width:150px;padding:16px 20px">'
            f'    <div class="mono" style="color:#7e7869">FINISHED AT</div>'
            f'    <div style="font-family:JetBrains Mono,monospace;font-size:1.7rem;'
            f'font-weight:500;color:#cfc9bd;margin-top:6px">{fmt_clock(snap["finished_at"])}</div>'
            f'  </div>'
            f'</div>',
            unsafe_allow_html=True,
        )

    es = snap["email_status"]
    if es == "SENT":
        st.markdown(
            f'<div style="margin-top:24px;padding:16px 20px;background:#0F1116;'
            f'border-left:3px solid {BP_BLUE};border-radius:6px">'
            f'  <div class="mono" style="color:{BP_BLUE}">✓ DELIVERED</div>'
            f'  <div class="serif-body" style="margin-top:6px;font-size:1.05rem">'
            f'{poss} film was emailed to '
            f'<strong style="color:#FFFFFF">{snap["customer_email"]}</strong> — '
            f'should arrive in a moment.</div></div>',
            unsafe_allow_html=True,
        )
    elif es and es.startswith("FAILED"):
        st.markdown(
            f'<div style="margin-top:24px;padding:16px 20px;background:#1A0F0F;'
            f'border-left:3px solid #FF6B6B;border-radius:6px">'
            f'  <div class="mono" style="color:#FF6B6B">✗ EMAIL NOT SENT</div>'
            f'  <div class="serif-body" style="margin-top:6px;font-size:1.05rem">'
            f'{es[7:].strip(": ")} — your film is still available below.</div></div>',
            unsafe_allow_html=True,
        )

    st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)

    vcols = st.columns([1, 1.4, 1])
    with vcols[1]:
        _render_film_card(snap["theme_id"], snap)

    st.markdown("<div style='height:24px'></div>", unsafe_allow_html=True)
    bcols = st.columns([2.8, 1.6, 1.6])
    with bcols[1]:
        if st.button("View gallery →", key="res_gallery", type="secondary",
                     use_container_width=True):
            goto("gallery")
    with bcols[2]:
        if st.button("Make another →", key="restart", type="primary",
                     use_container_width=True):
            _reset_session()


def _render_film_card(tid: str, snap: dict | None):
    t = THEMES.get(tid, {})
    sig = t.get("signature", BP_BLUE)
    st.markdown(
        f'<div style="display:flex;align-items:center;gap:10px;margin-bottom:6px">'
        f'  <div class="mono" style="color:{sig}">{t.get("code","")} · {t.get("genre","")}</div>'
        f'</div>'
        f'<div style="font-family:Cormorant Garamond,serif;font-size:2rem;color:#F5F3EC;'
        f'line-height:1;margin-bottom:4px">{t.get("name","")}</div>'
        f'<div class="serif-italic" style="margin-bottom:14px;font-size:1rem;color:#9a9488">'
        f'{t.get("tagline","")}</div>',
        unsafe_allow_html=True,
    )

    status = (snap or {}).get("status")
    final_url = (snap or {}).get("final_url")

    if status == "DONE" and final_url:
        st.video(final_url)
        st.markdown(
            f'<a href="{final_url}" target="_blank" style="text-decoration:none">'
            f'<button style="width:100%;background:{sig};color:#07080A;border:none;'
            f'font-family:JetBrains Mono,monospace;font-size:11px;font-weight:600;'
            f'letter-spacing:0.18em;text-transform:uppercase;padding:0.85rem 1rem;'
            f'cursor:pointer;border-radius:6px;min-height:44px">Download .mp4</button></a>',
            unsafe_allow_html=True,
        )
    elif status == "FAILED":
        st.markdown(
            f'<div style="padding:18px 22px;background:#1A0F0F;border-left:3px solid #FF6B6B;border-radius:6px">'
            f'  <div class="mono" style="color:#FF6B6B">✗ FAILED</div>'
            f'  <div class="serif-body" style="margin-top:6px;font-size:0.95rem">'
            f'{(snap or {}).get("error") or "Unknown error"}</div></div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f'<div style="position:relative;width:100%;aspect-ratio:9/16;background:{t.get("paper","#0A0A0A")};overflow:hidden;border-radius:6px">'
            f'  <div style="position:absolute;inset:0;background:radial-gradient(circle at 30% 40%,{sig}55,transparent 55%),radial-gradient(circle at 70% 60%,{t.get("accent","#222")}45,transparent 55%)"></div>'
            f'  <div style="position:absolute;bottom:0;left:0;right:0;padding:24px">'
            f'    <div class="mono" style="color:{sig}">A FILM ABOUT YOU</div>'
            f'    <div style="font-family:Cormorant Garamond,serif;font-size:2.4rem;color:#F5F3EC;line-height:0.95;margin-top:8px">{t.get("name","")}</div>'
            f'  </div>'
            f'  <div class="corner tl"></div><div class="corner tr"></div>'
            f'  <div class="corner bl"></div><div class="corner br"></div>'
            f'</div>'
            f'<div class="mono" style="margin-top:12px;color:#f5b800">'
            f'{"● DEMO MODE — configure env vars for real video" if DEMO_MODE else "● NO OUTPUT"}</div>',
            unsafe_allow_html=True,
        )

    st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)


# ──────────────────────────────────────────────────────────
# SCREEN — GALLERY (every film ever made, for client viewing)
# ──────────────────────────────────────────────────────────
def render_gallery():
    render_header(None)

    cols = st.columns([6, 1.4])
    with cols[1]:
        if st.button("← Back", key="gal_back", type="secondary",
                     use_container_width=True):
            goto("welcome")

    st.markdown(
        '<div class="mono" style="margin-top:8px">THE GALLERY</div>'
        '<h2 style="font-size:4.2rem;line-height:1;margin:8px 0 0 0">Every film created</h2>'
        '<p class="serif-body" style="max-width:560px;margin-top:1rem">'
        "Every short film generated at this booth, ready to play.</p>",
        unsafe_allow_html=True,
    )

    if DEMO_MODE:
        st.info("The gallery shows real films once generation is enabled "
                "(set ARK_API_KEY + TOS_* env vars).")
        return

    rcol = st.columns([6, 1.4])
    with rcol[1]:
        refresh = st.button("↻ Refresh", key="gal_refresh", type="secondary",
                            use_container_width=True)

    try:
        films = list_final_videos()
    except Exception as e:
        st.error(f"Couldn't load the gallery from storage: {e}")
        return

    if not films:
        st.markdown(
            '<div class="serif-italic" style="margin-top:28px;color:#9a9488;font-size:1.2rem">'
            "No films yet — the first one you create will appear here.</div>",
            unsafe_allow_html=True,
        )
        return

    _ = refresh  # any button press reruns → fresh listing

    def _name_of(tid: str) -> str:
        return THEMES.get(tid, {}).get("name") or tid.replace("-", " ").upper()

    # ── Filter by theme ───────────────────────────────────────────
    present = sorted({f["theme_id"] for f in films}, key=_name_of)
    options = ["All themes"] + present
    fcol = st.columns([2, 4])
    with fcol[0]:
        choice = st.selectbox(
            "Theme", options, key="gallery_theme",
            format_func=lambda t: t if t == "All themes" else _name_of(t),
            label_visibility="collapsed",
        )
    if choice != "All themes":
        films = [f for f in films if f["theme_id"] == choice]

    # ── Paginate (don't dump every player at once) ────────────────
    PAGE_SIZE = 9
    total = len(films)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(st.session_state.get("gallery_page", 0), pages - 1))
    start = page * PAGE_SIZE
    page_films = films[start:start + PAGE_SIZE]

    st.markdown(
        f'<div class="mono" style="margin-top:14px;margin-bottom:8px;color:{BP_BLUE}">'
        f'{total:02d} FILM{"S" if total != 1 else ""} · SHOWING '
        f'{start + 1}–{start + len(page_films)} · PAGE {page + 1}/{pages}</div>',
        unsafe_allow_html=True,
    )

    PER_ROW = 3
    for i in range(0, len(page_films), PER_ROW):
        grid = st.columns(PER_ROW)
        for col, film in zip(grid, page_films[i:i + PER_ROW]):
            with col:
                t = THEMES.get(film["theme_id"], {})
                sig = t.get("signature", BP_BLUE)
                st.markdown(
                    f'<div class="mono" style="color:{sig};margin-bottom:6px">'
                    f'{t.get("code","")} {("· " + _name_of(film["theme_id"])) if t.get("code") else _name_of(film["theme_id"])}</div>',
                    unsafe_allow_html=True,
                )
                st.video(film["url"])

    # ── Page controls ─────────────────────────────────────────────
    if pages > 1:
        st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
        pcol = st.columns([1, 1, 4])
        with pcol[0]:
            if st.button("← Prev", key="gal_prev", type="secondary",
                         use_container_width=True, disabled=page == 0):
                st.session_state.gallery_page = page - 1
                st.rerun()
        with pcol[1]:
            if st.button("Next →", key="gal_next", type="secondary",
                         use_container_width=True, disabled=page >= pages - 1):
                st.session_state.gallery_page = page + 1
                st.rerun()


def _reset_session():
    for k in ("step", "photo_bytes", "photo_name", "photo_remote_url",
              "customer_name", "customer_phone", "customer_email", "theme_id",
              "job_id", "error", "detail_errors"):
        st.session_state.pop(k, None)
    try:
        st.query_params.clear()
    except Exception:
        pass
    init_state()
    st.rerun()


# ──────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────
def main():
    st.set_page_config(
        page_title="Seedance Studio · BytePlus",
        page_icon="🎬",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    st.markdown(CSS, unsafe_allow_html=True)
    init_state()

    # Clicking the BytePlus logo (href="?home=1") starts a fresh film.
    try:
        if st.query_params.get("home"):
            for k in ("step", "photo_bytes", "photo_name", "photo_remote_url",
                      "customer_name", "customer_phone", "customer_email",
                      "theme_id", "job_id", "error", "detail_errors"):
                st.session_state.pop(k, None)
            st.query_params.clear()
            init_state()
    except Exception:
        pass

    if not st.session_state.get("job_id"):
        try:
            url_job = st.query_params.get("job")
        except Exception:
            url_job = None
        if url_job and get_job_queue().get(url_job) is not None:
            st.session_state.job_id = url_job
            snap = get_job_queue().get(url_job).snapshot()
            st.session_state.theme_id = snap["theme_id"]
            st.session_state.step = (
                "result" if snap["status"] in ("DONE", "FAILED") else "generating"
            )

    {
        "welcome": render_welcome,
        "capture": render_capture,
        "themes": render_themes,
        "details": render_details,
        "generating": render_generating,
        "result": render_result,
        "gallery": render_gallery,
    }[st.session_state.step]()


if __name__ == "__main__":
    main()
