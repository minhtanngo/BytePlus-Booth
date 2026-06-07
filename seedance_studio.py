"""
============================================================
 SEEDANCE STUDIO — Streamlit
 One-file app: cinematic UI + Seedance 2.0 R2V pipeline.

 Run (demo mode, mocked generation):
   pip install streamlit httpx
   streamlit run seedance_studio.py

 Run (real generation):
   pip install streamlit httpx tos
   export ARK_API_KEY=...
   export TOS_ACCESS_KEY=... TOS_SECRET_KEY=...
   export TOS_BUCKET=seedance-studio TOS_REGION=ap-southeast-1
   # ffmpeg + ffprobe required on PATH
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
import time
import uuid
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
# CONFIG
# ──────────────────────────────────────────────────────────
ARK_API_KEY = os.environ.get("ARK_API_KEY", "")
ARK_ENDPOINT = "https://ark.ap-southeast.bytepluses.com/api/v3/contents/generations/tasks"
MODEL_ID = "dreamina-seedance-2-0-260128"

TOS_BUCKET = os.environ.get("TOS_BUCKET", "")
TOS_REGION = os.environ.get("TOS_REGION", "ap-southeast-1")
TOS_ACCESS_KEY = os.environ.get("TOS_ACCESS_KEY", "")
TOS_SECRET_KEY = os.environ.get("TOS_SECRET_KEY", "")
# Endpoint used by the BytePlus `tos` Python SDK to upload/manage objects.
#   Local / external:  tos-ap-southeast-1.bytepluses.com    (public, default)
#   BFS / ECS in VPC:  tos-ap-southeast-1.ibytepluses.com   (private — faster, no egress cost)
# NOTE: do NOT use the tos-s3-* variants here. Those are S3-compatible and
# expect AWS4-HMAC-SHA256 signing; the BytePlus SDK uses TOS4-HMAC-SHA256.
TOS_ENDPOINT = os.environ.get("TOS_ENDPOINT", f"https://tos-{TOS_REGION}.bytepluses.com")
# Public bucket hostname — ALWAYS public, since Seedance fetches files over the internet.
TOS_PUBLIC_HOST = f"{TOS_BUCKET}.tos-{TOS_REGION}.bytepluses.com"

CLIP_DURATION = 15        # max single-clip duration on Seedance 2.0
NUM_CLIPS = 2             # 2 × 15 = 30 seconds, two-act structure
ASPECT_RATIO = "16:9"
RESOLUTION = "480p"        # 480p for fast/cheap testing; flip to "720p" or "1080p" for final
POLL_INTERVAL = 5
POLL_TIMEOUT = 600

# Quick setup:
#   export ARK_AK="AKLT..."
#   export ARK_SK="..."
#   # Optional — leave blank to auto-create on first run:
ARK_ASSET_GROUP_ID = os.environ.get("ARK_ASSET_GROUP_ID", "")
ARK_AK = os.environ.get("ARK_AK", "")
ARK_SK = os.environ.get("ARK_SK", "")
# ARK_ASSET_GROUP_ID = os.environ.get("ARK_ASSET_GROUP_ID", "")
ARK_ASSET_GROUP_NAME = "seedance_studio_subjects"   # used if we need to create one
ARK_PROJECT_NAME = "default"
ARK_REGION = "ap-southeast-1"
ASSET_POLL_INTERVAL = 5
ASSET_POLL_TIMEOUT = 180   # asset preprocessing usually 20-60s
USE_ASSET_LIBRARY = bool(ARK_AK and ARK_SK)   # auto-enable if AK/SK present

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM_EMAIL = os.environ.get("SMTP_FROM_EMAIL", SMTP_USER)
SMTP_FROM_NAME = os.environ.get("SMTP_FROM_NAME", "Seedance Studio")
EMAIL_ENABLED = bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD)

DEMO_MODE = not all([ARK_API_KEY, TOS_BUCKET, TOS_ACCESS_KEY, TOS_SECRET_KEY])

# ──────────────────────────────────────────────────────────
# THEMES — 4 short drama themes × 2 acts × 15s = 30s
#
# Short drama (短剧) is a distinct genre with strict craft
# rules: a hook in the first 3 seconds, an explicit conflict
# by 8 seconds, a status reversal by the end of the episode.
#
# DESIGN PRINCIPLES (v3):
#   1. GENDER-NEUTRAL. Every theme works for a male OR female
#      face uploaded as [Image 1]. Prompts use "the protagonist
#      from [Image 1]" and avoid gendered pronouns.
#   2. GENRE DIVERSITY. Four distinct short-drama subgenres:
#        01 SCI-FI       — Time-loop / message from your future self
#        02 HISTORICAL   — Imperial palace / lost-heir reveal
#        03 NEO-NOIR     — Modern mafia / wrong-table mistake
#        04 SUPERNATURAL — Inheritance / step-into-the-painting
#   3. FRAME-PERFECT BRIDGE. Act II opens on the SAME bridge prop
#      that froze in Act I's final frame.
#
# REFERENCE IMAGE CONVENTION (per BytePlus Seedance 2.0 R2V):
#   [Image 1] = the customer's portrait (asset:// URI)
#   [Image 2] = the last frame of Act I (asset:// URI)
#
# CONTINUITY STRATEGY (v3 — environment-drift fix):
#   Act II prompts now use a THREE-LAYER ANCHOR SYSTEM:
#
#     ┌─ Layer 1: CHARACTER ANCHOR  → locks face/body/wardrobe to [Image 1]
#     ├─ Layer 2: FRAME-MATCH ANCHOR → locks first 1.5s of Act II to the
#     │                                bridge prop position in [Image 2]
#     └─ Layer 3: SET ANCHOR (NEW)  → locks furniture, walls, lighting,
#                                     props, floor, windows to [Image 2]
#                                     Explicitly names every macro element
#                                     visible in the background and orders
#                                     the model NOT to reinvent the room.
#
#   Act II Scene 1 also follows a strict "FREEZE-THEN-DOLLY" rhythm:
#     • 0.0–1.5s : HOLD on bridge prop, identical framing to [Image 2]
#     • 1.5–4.0s : SLOW continuous dolly-out (NO cut) revealing the
#                  background — every element of which is locked to
#                  [Image 2] via the SET ANCHOR
#     • 4.0s+    : Action begins
#
#   This eliminates the common R2V failure mode where Act II's
#   wide-shot "establishing" of the room produces a similar-looking
#   but DIFFERENT room (different table, different chairs, different
#   wall pattern). The dolly-out is treated as one continuous shot
#   inside the same physical set, not a new establishing shot.
#
#   Act I prompts now end with an ESTABLISHING HOLD beat that
#   captures BOTH the bridge prop AND the surrounding set in a
#   medium-wide framing — so when the pipeline extracts the last
#   frame as [Image 2], the model has macro-environmental context
#   to lock against, not just a tight close-up.
# ──────────────────────────────────────────────────────────

# Universal audio footer — appended to every prompt.
PROMPT_FOOTER = (
    " IMPORTANT AUDIO CONSTRAINT: No BGM copyrighted music. "
    "Strictly no recognizable melodies. No famous film themes, no popular songs, "
    "no copyrighted soundtracks, no humming, no chanting, no lyrics in any "
    "language. Use only original ambient atmospheric audio: diegetic sound "
    "design, sub-bass, environmental texture, footsteps, breathing, fabric "
    "rustle, and original generic orchestral or piano swells with no "
    "recognizable melodic line."
)


def _p(text: str) -> str:
    """Append the universal audio/safety footer to a per-scene prompt."""
    return text.rstrip() + PROMPT_FOOTER


THEMES: dict[str, dict[str, Any]] = {

    # ═══════════════════════════════════════════════════════
    # 01 — THE LAST MESSAGE   (Sci-Fi Time-Loop Drama)
    # Identity anchor: face from [Image 1].
    # Bridge prop: a chipped white ceramic coffee mug, half-full,
    # held in the protagonist's RIGHT hand, steam still rising.
    # Act II opens on the EXACT same hand + mug + steam, same
    # kitchen tile, same morning light — only the eyes have
    # changed, because now they know.
    # Works for: male or female protagonist (lab coat over a
    # plain dark t-shirt, no gendered styling).
    # ═══════════════════════════════════════════════════════
    "last-message": {
        "code": "01",
        "name": "THE LAST MESSAGE",
        "tagline": "The video was sent from twelve hours in the future.",
        "description": "A scientist at a quantum communications lab receives a video file timestamped twelve hours from now. The person speaking on screen is them — bloodied, breathless, whispering one warning: 'Don't drink the coffee.' They look down at the mug already in their hand.",
        "signature": "#4FB3D9",
        "accent": "#0A1A2A",
        "paper": "#070A10",
        "keywords": ["SCI-FI", "TIME LOOP", "QUANTUM", "WARNING"],
        "acts": [
            {
                "num": "I", "title": "The Transmission",
                "desc": "A video arrives from the future. The face on screen is theirs.",
                "prompt": _p(
                    "Cinematic sci-fi short drama, shot on 35mm anamorphic, "
                    "cold cyan server-room light spilling into a warm tungsten "
                    "kitchenette, subterranean quantum research lab at 03:14 AM, "
                    "tense suspended-breath rhythm. "
                    "CHARACTER ANCHOR: The protagonist throughout this video is "
                    "the person from [Image 1] — match their face, hair, "
                    "complexion, and overall identity in every shot, regardless "
                    "of their gender. They wear an unbuttoned white lab coat "
                    "over a plain dark crew-neck t-shirt and dark trousers. A "
                    "lanyard with a black security badge hangs at their chest. "
                    "Their right hand holds a chipped white ceramic coffee mug, "
                    "half-full of black coffee, steam visibly rising. "
                    "[SCENE 1 - THE LAB]: Wide establishing shot of a dim "
                    "underground research lab. Rows of humming server racks "
                    "glow faint blue. The protagonist from [Image 1] sits alone "
                    "at a curved console of three monitors, white lab coat, "
                    "right hand cradling the chipped white mug as described. "
                    "On the central monitor, a console line blinks: "
                    "'INCOMING TRANSMISSION — ORIGIN: UNKNOWN.' "
                    "[SCENE 2 - THE TIMESTAMP]: Tight macro insert on the screen. "
                    "A video file decodes line by line. Metadata appears: "
                    "'SENT: " + "TODAY +12h 00m." + " RECIPIENT: SELF.' The "
                    "protagonist's free hand hovers over the trackpad. Cut to a "
                    "tight close-up of the face from [Image 1] — confusion, then "
                    "a slow narrowing of the eyes. They click PLAY. "
                    "[SCENE 3 - THE FACE ON SCREEN]: Cut to what plays on the "
                    "monitor: the same face from [Image 1], lit by emergency "
                    "red light, blood at the temple, breathing hard, whispering "
                    "directly into the camera. Audio is muffled, urgent: "
                    "'Listen to me. You have less than a day. Whatever you do — "
                    "don't drink the—' The transmission cuts to static. "
                    "[SCENE 4 - THE REALIZATION]: Reverse shot on the live "
                    "protagonist from [Image 1] in the lab. Their lips part "
                    "slowly. Their eyes drop — camera follows their gaze down to "
                    "their own right hand, still holding the chipped white "
                    "ceramic mug, steam still rising from the black coffee. "
                    "[SCENE 5 - THE FREEZE]: Slow macro push-in on the mug in "
                    "the right hand of the protagonist from [Image 1]. The "
                    "steam curls. Their thumb tightens against the ceramic. "
                    "The reflection of the static-filled monitor flickers in "
                    "the dark surface of the coffee. Their breath catches — we "
                    "see it move the steam. ESTABLISHING HOLD: pull the camera "
                    "back just slightly to a medium framing that keeps BOTH "
                    "the mug AND the surrounding lab in view — the three "
                    "curved monitors with their cyan glow, the edge of the "
                    "metal desk with a scattered notepad and a black retractable "
                    "pen, the back of the protagonist's lab-coat shoulder, the "
                    "blurred row of humming server racks behind, the cold "
                    "concrete floor. Hold this exact framing for the final "
                    "beat — right hand, chipped white mug, rising steam, "
                    "monitor static reflected in the coffee, lab environment "
                    "clearly visible behind. Cut to black. "
                    "16:9, photorealistic, cinematic, 35mm anamorphic, cold "
                    "blue / warm tungsten mixed lighting, 15 seconds."
                ),
            },
            {
                "num": "II", "title": "Twelve Hours",
                "desc": "The same hand. The same mug. They haven't moved.",
                "prompt": _p(
                    "Cinematic sci-fi short drama continuation, shot on 35mm "
                    "anamorphic, same cold cyan and warm tungsten mixed "
                    "lighting as [Image 2], same subterranean quantum lab, "
                    "rising tension. "
                    "CHARACTER ANCHOR: The protagonist throughout this video is "
                    "still the person from [Image 1] — match their face, hair, "
                    "complexion, and identity exactly, regardless of gender. "
                    "Same white lab coat, same dark crew-neck t-shirt, same "
                    "black-badge lanyard as the closing frame of [Image 2]. "
                    "FRAME-MATCH ANCHOR: This video opens on the EXACT same "
                    "frame that closed [Image 2] — a tight macro shot of the "
                    "protagonist's right hand holding the chipped white "
                    "ceramic coffee mug, steam still rising, monitor static "
                    "reflected in the dark coffee surface. Camera position, "
                    "lens, and lighting must match [Image 2] for the first 1.5 "
                    "seconds so the cut is invisible. "
                    "SET ANCHOR (CRITICAL — DO NOT REINVENT THE ROOM): The "
                    "physical environment in this video is the SAME ROOM as "
                    "[Image 2], not a similar room. Reproduce identically: "
                    "the same three curved console monitors with cyan glow, "
                    "the same metal desk surface with the scattered notepad "
                    "and black retractable pen visible in [Image 2], the same "
                    "rows of humming server racks blurred behind, the same "
                    "cold concrete floor, the same dim cyan-and-tungsten "
                    "lighting falloff, the same camera angle and lens height. "
                    "Do not invent new furniture, new monitors, new desk "
                    "objects, new walls, new doors. Every macro element of "
                    "the set must originate from [Image 2]. "
                    "[SCENE 1 - THE HAND, FREEZE-THEN-DOLLY]: Open on the exact "
                    "frame from [Image 2]: medium framing of the right hand of "
                    "the protagonist from [Image 1] holding the chipped white "
                    "mug, steam rising, monitor static reflected in the coffee, "
                    "the metal desk and curved monitors and blurred server "
                    "racks visible exactly as in [Image 2]. HOLD this framing "
                    "completely still for 1.5 seconds — no camera move, no "
                    "actor move. Then begin a slow continuous dolly-out (no "
                    "cut) over the next 2.5 seconds, gradually revealing more "
                    "of the SAME lab — same monitors, same desk, same server "
                    "racks, same concrete floor. As the dolly completes, the "
                    "hand tilts and the black coffee pours out onto the SAME "
                    "concrete floor visible in [Image 2]. Steam rises where it "
                    "hits the cold floor. "
                    "[SCENE 2 - THE SAMPLE]: The protagonist from [Image 1] "
                    "sets the empty mug down on the SAME metal console desk "
                    "from [Image 2], opens a desk drawer, removes a sterile "
                    "glass sample vial, and scrapes a few drops of the "
                    "spilled coffee from the SAME concrete floor from "
                    "[Image 2] into the vial with a pipette. The three "
                    "curved monitors with cyan glow remain visible behind "
                    "them, identical to [Image 2]. Their hands are steady "
                    "now. Cut to a tight close-up of the face from [Image 1] "
                    "— fear has been replaced by something colder: focus. "
                    "[SCENE 3 - THE SCAN]: They drop the vial into a benchtop "
                    "mass spectrometer that sits on the SAME metal desk from "
                    "[Image 2]. A waveform climbs the screen. A red "
                    "alert flashes: 'COMPOUND DETECTED — POLONIUM-210.' The "
                    "protagonist exhales slowly. They look up sharply at the "
                    "security camera in the corner of the lab. "
                    "[SCENE 4 - THE LOOP]: They walk briskly back to the SAME "
                    "three curved console monitors from [Image 2] and begin "
                    "typing. Tight insert on the central monitor: a new "
                    "transmission window opens. The destination field auto-"
                    "fills: 'RECIPIENT: SELF.' The timestamp field auto-fills: "
                    "'SEND TO: TODAY −12h 00m.' Their finger hovers over the "
                    "SEND key. "
                    "[SCENE 5 - THE WHISPER]: They pull the microphone close "
                    "and lean in. Tight close-up on the face from [Image 1], "
                    "lit by the cyan glow of the SAME monitor from [Image 2] "
                    "— calm, controlled, alive. They whisper into the mic, "
                    "voice low and clear: 'Listen to me. You have less than "
                    "a day. Whatever you do — don't drink the—' Their "
                    "finger presses SEND. The screen flashes: 'TRANSMISSION "
                    "ACCEPTED.' Cut to black. "
                    "Final beat: text card appears in clean monospaced cyan "
                    "letters: 'THE LOOP IS HOW YOU SURVIVE IT.' "
                    "ENVIRONMENT LOCK (final reinforcement): every shot in "
                    "this video must take place inside the SAME lab as "
                    "[Image 2] — same monitors, same desk, same server racks, "
                    "same concrete floor, same lighting. Do not change rooms. "
                    "16:9, photorealistic, cinematic, 35mm anamorphic, cold "
                    "blue / warm tungsten, 15 seconds."
                ),
            },
        ],
    },

    # ═══════════════════════════════════════════════════════
    # 02 — THE EMPEROR'S MARK   (Historical Imperial Drama)
    # Identity anchor: face from [Image 1].
    # Bridge prop: a heavy iron shackle on the LEFT wrist, with
    # a circular crimson burn-mark / birthmark visible just above
    # it. Act II opens on the same wrist — the chain is being
    # struck off, and the camera then dollies down to the same
    # crimson mark as a jade-and-gold cuff slides over it.
    # Works for: male or female protagonist (loose grey hemp
    # tunic in Act I, embroidered black-and-gold imperial robe
    # in Act II — both are historically unisex court garments).
    # ═══════════════════════════════════════════════════════
    "emperors-mark": {
        "code": "02",
        "name": "THE EMPEROR'S MARK",
        "tagline": "They dragged a prisoner to the throne. He recognized the mark.",
        "description": "A prisoner is dragged in chains across the polished black stone of an imperial throne room. The old emperor descends from the dais, lifts the prisoner's wrist, and turns it to the light — revealing a crimson mark he has not seen in twenty-five years. The court falls silent. He recognizes his own blood.",
        "signature": "#C8923A",
        "accent": "#5C0F1F",
        "paper": "#0B0807",
        "keywords": ["HISTORICAL", "IMPERIAL COURT", "LOST HEIR", "古装"],
        "acts": [
            {
                "num": "I", "title": "The Throne Room",
                "desc": "Chains. Silence. A mark on the wrist the emperor remembers.",
                "prompt": _p(
                    "Cinematic historical short drama set in an East-Asian "
                    "imperial palace, shot on 35mm anamorphic, vast polished "
                    "black-stone throne hall, towering red lacquered columns, "
                    "shafts of dust-laden golden light falling from high "
                    "latticework windows, slow reverent rhythm. "
                    "CHARACTER ANCHOR: The protagonist throughout this video is "
                    "the person from [Image 1] — match their face, hair, "
                    "complexion, and overall identity in every shot, regardless "
                    "of their gender. Hair is pulled back simply, dust on the "
                    "cheekbones. They wear a coarse loose grey hemp prisoner's "
                    "tunic tied with a rope at the waist, bare feet, heavy "
                    "rust-iron shackles around both wrists. On the LEFT wrist, "
                    "just above the shackle, a distinct circular crimson "
                    "birthmark the size of a coin is clearly visible. "
                    "[SCENE 1 - THE DRAG]: Wide low-angle establishing shot. "
                    "Two imperial guards in black lacquered armor drag the "
                    "protagonist from [Image 1] across the polished black "
                    "throne hall by the iron shackles. The shackles scrape "
                    "loudly against stone. Court ministers in dark embroidered "
                    "robes line both sides of the hall, watching in silence. "
                    "[SCENE 2 - THE KNEEL]: The guards force the protagonist "
                    "to their knees at the foot of a long dais of nine black "
                    "stone steps. At the top of the dais sits an elderly "
                    "emperor — silver beard, dragon-embroidered crimson robe, "
                    "a tall jade-and-gold crown, watching with stone-still "
                    "eyes. Wide shot: protagonist kneeling small at the foot "
                    "of the dais, emperor towering above. "
                    "[SCENE 3 - THE DESCENT]: Slow push-in on the emperor as "
                    "he leans forward in his throne. He has noticed something. "
                    "He rises — the entire court bows lower. He begins to walk "
                    "down the nine black stone steps, one at a time, his robe "
                    "trailing. The court holds its breath. "
                    "[SCENE 4 - THE WRIST]: Medium shot. The emperor stops "
                    "before the kneeling protagonist from [Image 1]. He kneels "
                    "down to their level — an unthinkable gesture — and "
                    "slowly, gently, lifts their LEFT wrist toward the shaft "
                    "of golden light from above. Macro insert close-up: the "
                    "iron shackle, and just above it, the circular crimson "
                    "birthmark glowing in the light. "
                    "[SCENE 5 - THE RECOGNITION]: Tight close-up on the "
                    "emperor's face — his stone-still composure cracks. His "
                    "eyes fill. He whispers a single name to the protagonist, "
                    "his voice barely audible: a name no one in this hall has "
                    "spoken in twenty-five years. Reverse close-up on the face "
                    "from [Image 1]: confusion, then dawning realization. "
                    "Behind them, a minister gasps. ESTABLISHING HOLD: pull "
                    "the camera back just slightly to a medium framing that "
                    "keeps BOTH the protagonist's lifted LEFT wrist (iron "
                    "shackle, crimson birthmark, emperor's old hand cradling) "
                    "AND the surrounding hall in view — the polished black "
                    "stone floor at their knees, the foot of the long dais "
                    "of nine black stone steps behind the emperor, two tall "
                    "red lacquered columns flanking left and right, a row of "
                    "kneeling ministers in dark embroidered robes blurred in "
                    "the deep background, dust suspended in the shaft of "
                    "golden light. Hold this exact framing as the final "
                    "beat. Cut to black. "
                    "16:9, photorealistic, cinematic, 35mm anamorphic, deep "
                    "lacquer-red and obsidian palette, golden god-rays, "
                    "15 seconds."
                ),
            },
            {
                "num": "II", "title": "The Crown",
                "desc": "The chain falls. A jade cuff closes over the mark.",
                "prompt": _p(
                    "Cinematic historical short drama continuation, shot on "
                    "35mm anamorphic, same vast imperial throne hall, same "
                    "shafts of golden light, same lacquer-red and obsidian "
                    "palette as [Image 2], slow ceremonial rhythm. "
                    "CHARACTER ANCHOR: The protagonist throughout this video is "
                    "still the person from [Image 1] — match their face, hair, "
                    "complexion, and identity exactly, regardless of gender. "
                    "They still bear the smudges of dust on the cheekbones "
                    "from [Image 2] for the first scene, before being cleaned. "
                    "FRAME-MATCH ANCHOR: This video opens on the EXACT same "
                    "frame that closed [Image 2] — a medium framing of the "
                    "protagonist's LEFT wrist, the iron shackle and the "
                    "circular crimson birthmark, the emperor's old hand still "
                    "cradling it in the same shaft of golden light, with the "
                    "polished black stone floor and the foot of the dais "
                    "visible behind. Camera position, lens, and lighting "
                    "must match [Image 2] for the first 1.5 seconds so the "
                    "cut is invisible. "
                    "SET ANCHOR (CRITICAL — DO NOT REINVENT THE HALL): The "
                    "physical environment in this video is the SAME imperial "
                    "throne hall as [Image 2], not a similar hall. Reproduce "
                    "identically: the same polished black stone floor with "
                    "its mirror-like reflection, the same long dais of nine "
                    "black stone steps leading up to the throne, the same "
                    "tall red lacquered columns flanking the hall, the same "
                    "high latticework windows with shafts of dust-laden "
                    "golden light, the same row of kneeling ministers in dark "
                    "embroidered robes, the same elderly emperor in the same "
                    "dragon-embroidered crimson robe and same tall jade-and-"
                    "gold crown. Do not invent a new hall, new columns, new "
                    "throne, new floor pattern. Every macro element of the "
                    "set must originate from [Image 2]. "
                    "[SCENE 1 - THE STRIKE, FREEZE-THEN-DOLLY]: Open on the "
                    "exact frame from [Image 2]: medium framing of the LEFT "
                    "wrist of the protagonist from [Image 1] — iron shackle, "
                    "crimson birthmark, emperor's hand cradling, the polished "
                    "black floor and foot of the dais visible behind, exactly "
                    "as in [Image 2]. HOLD this framing completely still for "
                    "1.5 seconds — no camera move, no actor move. Then begin "
                    "a slow continuous dolly-out (no cut) over the next 2.5 "
                    "seconds, gradually revealing more of the SAME hall — "
                    "same red lacquered columns, same kneeling ministers, "
                    "same shafts of golden light, same nine black stone steps "
                    "behind the emperor. As the dolly completes, the emperor "
                    "lifts his other hand and gestures sharply. A guard steps "
                    "forward with a small iron hammer and strikes the shackle "
                    "open in one ringing blow. The iron falls away from the "
                    "wrist and clatters loudly against the SAME polished "
                    "black stone floor visible in [Image 2]. "
                    "[SCENE 2 - THE RISE]: Wide shot of the SAME throne hall "
                    "from [Image 2] — same columns, same dais, same ministers, "
                    "same lighting. The old emperor takes the freed left hand "
                    "of the protagonist from [Image 1] and slowly helps them "
                    "rise to their feet. The entire court — every minister in "
                    "every embroidered robe — drops to their knees in a wave, "
                    "foreheads to the SAME polished black floor from "
                    "[Image 2]. The only two still standing are the old "
                    "emperor and the protagonist from [Image 1]. "
                    "[SCENE 3 - THE ROBE]: Slow ceremonial sequence inside "
                    "the SAME hall: court attendants in white silk approach "
                    "with a folded embroidered black-and-gold imperial robe "
                    "stitched with a single coiled dragon. They lift the "
                    "coarse grey prisoner's tunic away from the protagonist's "
                    "shoulders and drape the imperial robe over them. The "
                    "protagonist from [Image 1] stands perfectly still — same "
                    "face, no longer kneeling. The same red lacquered columns "
                    "and same shafts of golden light from [Image 2] frame the "
                    "shot behind them. "
                    "[SCENE 4 - THE CUFF]: Macro insert close-up on the SAME "
                    "LEFT wrist from [Image 2] — the iron is gone, but the "
                    "crimson birthmark is still there. An attendant slides a "
                    "wide jade-and-gold cuff slowly down the forearm. The "
                    "cuff closes over the wrist with a soft click, covering "
                    "the crimson mark completely. The mark is now hidden — "
                    "but everyone in this room has seen it. "
                    "[SCENE 5 - THE TURN]: The old emperor steps back, faces "
                    "the kneeling court inside the SAME hall from [Image 2], "
                    "and raises his voice so it echoes through the hall: "
                    "'Behold the blood of this house, lost and returned.' "
                    "Slow push-in on the face from [Image 1] as the "
                    "protagonist turns slowly to face the court for the "
                    "first time — calm, unreadable, sovereign. The dragon on "
                    "the robe catches the light. Final beat: HOLD on the "
                    "face from [Image 1] with the same shafts of golden "
                    "light and same red lacquered columns from [Image 2] "
                    "framing the shot behind. Cut to black. "
                    "Final beat: text card appears in elegant vermilion seal-"
                    "script lettering: 'THE THRONE REMEMBERS ITS OWN.' "
                    "ENVIRONMENT LOCK (final reinforcement): every shot in "
                    "this video must take place inside the SAME imperial "
                    "throne hall as [Image 2] — same floor, same columns, "
                    "same dais, same windows, same lighting. Do not change "
                    "rooms. "
                    "16:9, photorealistic, cinematic, 35mm anamorphic, deep "
                    "lacquer-red and obsidian palette, golden god-rays, "
                    "15 seconds."
                ),
            },
        ],
    },

    # ═══════════════════════════════════════════════════════
    # 03 — THE WRONG TABLE   (Neo-Noir Modern Mafia Drama)
    # Identity anchor: face from [Image 1].
    # Bridge prop: a deep-red bordeaux wine being poured into a
    # crystal glass — the pour is FROZEN mid-air at the end of
    # Act I and RESUMES mid-air at the start of Act II.
    # Wardrobe is a sharp black overcoat over a black turtleneck —
    # gender-neutral. The protagonist is the customer.
    # The "stranger" is a generic dangerous figure (dark suit,
    # silver ring, never face-on at start).
    # ═══════════════════════════════════════════════════════
    "wrong-table": {
        "code": "03",
        "name": "THE WRONG TABLE",
        "tagline": "They sat at the wrong table. The right one came back.",
        "description": "A quiet evening at a high-end restaurant. They take an empty corner table by the window. The maître d' approaches — pale, urgent, whispering: 'That table is reserved.' Before they can move, the reservation arrives. He sits down opposite them. He does not look surprised. He pours them a glass of wine.",
        "signature": "#8B0000",
        "accent": "#1A1A1A",
        "paper": "#080706",
        "keywords": ["NEO-NOIR", "MAFIA", "MISTAKE", "POWER"],
        "acts": [
            {
                "num": "I", "title": "The Reservation",
                "desc": "An empty table. A stranger sits down. He's been expected.",
                "prompt": _p(
                    "Cinematic neo-noir short drama, shot on 35mm anamorphic, "
                    "dim moody amber-and-shadow restaurant lighting, "
                    "high-end private dining room of a Michelin-starred "
                    "restaurant, rain streaking the floor-to-ceiling windows "
                    "overlooking a city at night, slow simmering rhythm. "
                    "CHARACTER ANCHOR: The protagonist throughout this video is "
                    "the person from [Image 1] — match their face, hair, "
                    "complexion, and overall identity in every shot, regardless "
                    "of their gender. They wear a sharp tailored black wool "
                    "overcoat over a fine black cashmere turtleneck, simple "
                    "dark trousers, no visible jewelry. Hair neatly groomed, "
                    "expression tired but composed. "
                    "[SCENE 1 - THE ARRIVAL]: Wide establishing shot of an "
                    "almost-empty Michelin restaurant at night, rain on the "
                    "tall windows, distant murmur of two other tables. The "
                    "protagonist from [Image 1] is led by a young waiter to a "
                    "small corner table set for two by the window. They sit, "
                    "remove their black overcoat, drape it over the back of "
                    "the chair, and pick up the menu. Calm, tired, alone. "
                    "[SCENE 2 - THE WHISPER]: The maître d' — a precise man "
                    "in a charcoal suit — approaches with a strange urgency. "
                    "He leans in close, voice barely above a whisper: 'Sir/"
                    "Madam, my apologies — this table is reserved.' Tight "
                    "close-up on the face from [Image 1]: a small polite smile, "
                    "ready to rise. Reverse close-up on the maître d' — his "
                    "eyes flick toward the entrance and his face has gone "
                    "completely pale. "
                    "[SCENE 3 - THE ENTRANCE]: Slow tracking shot toward the "
                    "restaurant entrance. The double doors open. A tall figure "
                    "in a perfectly tailored midnight-black three-piece suit "
                    "steps in — we do not yet see his face, only the silver "
                    "ring on his right hand and the way the entire room goes "
                    "instantly, completely silent. Both other tables stop "
                    "eating. The pianist stops playing mid-note. "
                    "[SCENE 4 - THE SIT]: He walks slowly across the room — "
                    "directly to the protagonist's table. The maître d' "
                    "freezes. The figure sits down opposite the protagonist "
                    "from [Image 1] without a word, without a flicker of "
                    "surprise. Now we see his face for the first time: late "
                    "fifties, scarred jaw, eyes like cold water, a small "
                    "smile. He nods once at the maître d'. A waiter rushes "
                    "forward with a decanter of deep-red bordeaux wine. "
                    "[SCENE 5 - THE POUR]: The waiter offers the decanter. "
                    "The man in the black suit takes it himself. He fills "
                    "his own crystal glass first — slowly. Then he reaches "
                    "across the white linen and tilts the decanter over the "
                    "empty crystal glass in front of the protagonist from "
                    "[Image 1]. Macro close-up: the deep-red wine begins to "
                    "pour in a clean ribbon from the decanter spout toward "
                    "the empty glass. The man's voice, low and warm: 'I have "
                    "been wanting to meet you for a long time.' "
                    "ESTABLISHING HOLD: pull the camera back just slightly "
                    "to a medium framing across the corner table that keeps "
                    "BOTH the frozen wine ribbon AND the surrounding set in "
                    "view — the small round corner table covered in pristine "
                    "white linen, a single low brass candle holder with a "
                    "lit candle between the two place settings, the empty "
                    "crystal glass on the protagonist's side, a folded white "
                    "napkin, two small white porcelain plates with gold rims, "
                    "the back of the protagonist's chair with the black wool "
                    "overcoat draped over it, the rain-streaked floor-to-"
                    "ceiling window directly behind showing the city night, "
                    "and the dim amber dining room with two other tables and "
                    "the dark grand piano blurred in the deep background. "
                    "Hold this exact framing as the final beat — decanter "
                    "tilted, ribbon of deep-red wine frozen mid-air halfway "
                    "between the spout and the empty glass, candle flame "
                    "reflected in the crystal, the entire restaurant set "
                    "clearly visible behind. Cut to black. "
                    "16:9, photorealistic, cinematic, 35mm anamorphic, deep "
                    "amber-and-shadow palette, candle flames, 15 seconds."
                ),
            },
            {
                "num": "II", "title": "The Toast",
                "desc": "The wine fills the glass. The restaurant is empty now.",
                "prompt": _p(
                    "Cinematic neo-noir short drama continuation, shot on "
                    "35mm anamorphic, same dim amber-and-shadow restaurant "
                    "lighting as [Image 2], same rain-streaked windows, same "
                    "white-linen corner table, but the dining room behind "
                    "them is now completely empty — every other guest has "
                    "been quietly removed. Slow controlled rhythm. "
                    "CHARACTER ANCHOR: The protagonist throughout this video "
                    "is still the person from [Image 1] — match their face, "
                    "hair, complexion, and identity exactly, regardless of "
                    "gender. Same tailored black wool overcoat draped over "
                    "the chair, same black cashmere turtleneck as [Image 2]. "
                    "FRAME-MATCH ANCHOR: This video opens on the EXACT same "
                    "frame that closed [Image 2] — a medium framing across "
                    "the corner table showing the decanter tilted in the "
                    "man's hand, the ribbon of deep-red bordeaux wine frozen "
                    "mid-air halfway between the spout and the empty crystal "
                    "glass, the candle flame reflected, AND the surrounding "
                    "table set (white linen, brass candle holder, plates, "
                    "napkin, draped overcoat, rain-streaked window behind). "
                    "Camera position, lens, and lighting must match [Image 2] "
                    "for the first 1.5 seconds so the cut is invisible. "
                    "SET ANCHOR (CRITICAL — DO NOT REINVENT THE TABLE OR "
                    "ROOM): The physical environment in this video is the "
                    "SAME corner table and SAME restaurant as [Image 2], not "
                    "a similar one. Reproduce identically: "
                    "(a) THE TABLE — the same small round corner table, the "
                    "same pristine white linen tablecloth with its specific "
                    "drape and folds, the same low brass candle holder with "
                    "the same single lit candle in the center, the same "
                    "empty crystal wine glass on the protagonist's side, the "
                    "same crystal glass on the man's side, the same two "
                    "small white porcelain plates with gold rims, the same "
                    "folded white napkin, the same silverware position, the "
                    "same decanter shape; "
                    "(b) THE CHAIRS — the same chair the protagonist sits "
                    "in with the same tailored black wool overcoat draped "
                    "exactly the same way over the back, the same chair "
                    "across from them; "
                    "(c) THE ROOM — the same rain-streaked floor-to-ceiling "
                    "window directly behind showing the same city night view, "
                    "the same dim amber-and-shadow lighting, the same wood "
                    "panelling on the visible walls, the same dark grand "
                    "piano position, the same overall geometry of the dining "
                    "room. Do not invent a new table, new chairs, new candle, "
                    "new glassware, new window, new wall, new room layout. "
                    "Every macro element of the set must originate from "
                    "[Image 2]. The ONLY change permitted from [Image 2] is "
                    "that the OTHER tables in the deep background, and any "
                    "background staff, are now empty/gone. "
                    "[SCENE 1 - THE POUR RESUMES, FREEZE-THEN-DOLLY]: Open "
                    "on the exact frame from [Image 2]: medium framing across "
                    "the same corner table, ribbon of deep-red wine "
                    "suspended mid-air, decanter tilted, candle flame, "
                    "white linen, plates, napkin, draped overcoat, rain-"
                    "streaked window — all visible exactly as in [Image 2]. "
                    "HOLD this framing completely still for 1.5 seconds — no "
                    "camera move, no actor move, the wine ribbon does not "
                    "yet move. Then the wine ribbon resumes its motion and "
                    "pours smoothly into the SAME crystal glass from "
                    "[Image 2], filling it halfway. The decanter rises. "
                    "Begin a slow continuous dolly-back (no cut) over the "
                    "next 2.5 seconds, opening to a wide two-shot across "
                    "the SAME table: the protagonist from [Image 1] on one "
                    "side and the man in the midnight-black suit on the "
                    "other — the same table, same chairs, same candle, same "
                    "linen, same window behind, all matching [Image 2]. The "
                    "deep background of the dining room is now empty: the "
                    "other tables that were visible in [Image 2] have been "
                    "cleared, the pianist is gone, two of the man's "
                    "associates in dark coats stand silently by the locked "
                    "entrance. "
                    "[SCENE 2 - THE QUESTION]: Tight close-up on the face "
                    "from [Image 1] — calm on the surface, throat tight. "
                    "Their eyes flick, just once, past the SAME brass candle "
                    "holder from [Image 2] toward the now-empty room. They "
                    "look back at the man. Reverse close-up on the man — "
                    "small warm smile, eyes still cold. He says quietly: "
                    "'I think there has been a mistake about which one of us "
                    "is the dangerous one at this table.' "
                    "[SCENE 3 - THE FILE]: He slides a slim cream manila "
                    "folder across the SAME pristine white linen tablecloth "
                    "from [Image 2] toward the protagonist. Macro insert: "
                    "the folder bears a small wax seal in the shape of a "
                    "coiled serpent. The protagonist from [Image 1] opens it "
                    "slowly. Reverse close-up on their face as they read — "
                    "their composure breaks for a single frame: a flicker "
                    "of recognition, then it locks back into place. "
                    "[SCENE 4 - THE GLASS]: The protagonist from [Image 1] "
                    "closes the folder calmly, sets it down on the SAME "
                    "white linen, and finally reaches for the SAME crystal "
                    "wine glass from [Image 2] now filled with the deep-red "
                    "bordeaux. Macro insert on the fingers from [Image 1] "
                    "as they close around the stem of the crystal — steady, "
                    "deliberate, no tremor. They lift the glass. "
                    "[SCENE 5 - THE TOAST]: Medium two-shot across the SAME "
                    "white linen corner table from [Image 2] — same candle, "
                    "same plates, same napkin, same rain-streaked window "
                    "behind. The protagonist from [Image 1] raises the "
                    "crystal glass of deep-red wine — the same wine the man "
                    "just poured — toward him. He raises his own crystal "
                    "glass in answer. Their glasses meet in the air directly "
                    "above the SAME brass candle holder from [Image 2] with "
                    "a soft chime. The man's small smile widens. The "
                    "protagonist from [Image 1] does not smile — but their "
                    "eyes hold his without flinching. Tight close-up on the "
                    "face from [Image 1] over the rim of the glass: calm, "
                    "unreadable, decided. Cut to black on the chime of the "
                    "glasses. "
                    "Final beat: text card appears in elegant blood-red "
                    "serif lettering: 'NOBODY SITS AT THE WRONG TABLE BY "
                    "ACCIDENT.' "
                    "ENVIRONMENT LOCK (final reinforcement): every shot in "
                    "this video must take place at the SAME corner table "
                    "inside the SAME restaurant as [Image 2] — same "
                    "tablecloth, same candle holder, same crystal, same "
                    "plates, same chairs, same window, same lighting. Do "
                    "not change the table. Do not change the room. "
                    "16:9, photorealistic, cinematic, 35mm anamorphic, deep "
                    "amber-and-shadow palette, candle flames, 15 seconds."
                ),
            },
        ],
    },

    # ═══════════════════════════════════════════════════════
    # 04 — THE INHERITANCE   (Supernatural Gothic Drama)
    # Identity anchor: face from [Image 1].
    # Bridge prop: a single fingertip pressed against the carved
    # gilded wooden frame of an enormous oil portrait. The portrait
    # depicts the SAME face from [Image 1] in 1920s dress. Act I
    # final frame: fingertip touches frame. Act II opening frame:
    # SAME fingertip, SAME frame, SAME angle — but the wallpaper
    # behind has shifted to 1924, and the protagonist is now
    # INSIDE the painting.
    # Wardrobe: charcoal trench coat (Act I) → 1920s charcoal
    # three-piece OR charcoal silk drop-waist dress (Act II,
    # described as "1920s charcoal evening attire" — Seedance
    # will adapt to the gender of [Image 1]).
    # ═══════════════════════════════════════════════════════
    "the-inheritance": {
        "code": "04",
        "name": "THE INHERITANCE",
        "tagline": "The portrait on the wall has their face. It was painted in 1924.",
        "description": "They inherit a remote ancestral mansion they have never visited. The lawyer leaves them at the door with a single brass key. At the end of the long hallway, an enormous oil portrait hangs above the fireplace — painted in 1924, signed by an artist they have never heard of. The face in the portrait is their own.",
        "signature": "#7A6A3F",
        "accent": "#1C1814",
        "paper": "#0A0907",
        "keywords": ["SUPERNATURAL", "GOTHIC", "INHERITANCE", "PORTRAIT"],
        "acts": [
            {
                "num": "I", "title": "The Portrait",
                "desc": "Their face. On a canvas. Signed in 1924.",
                "prompt": _p(
                    "Cinematic supernatural gothic short drama, shot on 35mm "
                    "anamorphic, cold blue moonlight through tall arched "
                    "windows mixed with warm flickering firelight from a vast "
                    "stone fireplace, interior of a remote ancestral mansion, "
                    "dust suspended in the air, slow uneasy rhythm. "
                    "CHARACTER ANCHOR: The protagonist throughout this video is "
                    "the person from [Image 1] — match their face, hair, "
                    "complexion, and overall identity in every shot, regardless "
                    "of their gender. They wear a charcoal grey wool trench "
                    "coat over a fine black turtleneck, dark jeans, plain "
                    "leather boots, no jewelry. They carry nothing but a "
                    "single heavy brass key. "
                    "[SCENE 1 - THE DOOR]: Wide low-angle establishing shot of "
                    "an enormous Victorian mansion on a windswept hill at "
                    "dusk, no other houses for miles. The protagonist from "
                    "[Image 1] stands alone before the tall double front doors "
                    "in the charcoal trench coat described above. They turn "
                    "the heavy brass key in the lock. The door creaks open. "
                    "[SCENE 2 - THE HALLWAY]: Slow tracking shot following "
                    "behind the protagonist from [Image 1] as they walk down "
                    "a long dim wood-paneled hallway. Dust hangs in shafts of "
                    "blue moonlight. White cloths cover the furniture like "
                    "ghosts. At the far end of the hallway, a vast stone "
                    "fireplace is already lit — flames flickering, though no "
                    "one has been here in decades. "
                    "[SCENE 3 - THE PORTRAIT]: Reverse tracking shot. As the "
                    "protagonist from [Image 1] approaches the fireplace, an "
                    "enormous oil portrait above the mantle slowly resolves "
                    "into focus. The portrait shows a single figure in "
                    "elegant 1920s charcoal evening attire, seated in a "
                    "high-backed leather chair, one hand resting on the "
                    "armrest. The face in the portrait is the EXACT same "
                    "face as the protagonist from [Image 1] — same eyes, "
                    "same jawline, same expression. The protagonist stops "
                    "walking. "
                    "[SCENE 4 - THE SIGNATURE]: Slow push-in on the bottom "
                    "right corner of the portrait. Macro insert close-up: a "
                    "small elegant brushstroke signature reads 'H. ASHFORD — "
                    "1924.' Cut to a tight close-up of the face from "
                    "[Image 1] — the live protagonist, lit by firelight, "
                    "lips parting in silent disbelief. Their eyes lift "
                    "slowly to the portrait's eyes. "
                    "[SCENE 5 - THE TOUCH]: They raise their right hand "
                    "slowly toward the gilded carved wooden frame of the "
                    "portrait. Macro insert close-up: the fingertip of their "
                    "right index finger approaches the ornate gilded carving "
                    "of the frame and touches it gently. The instant the "
                    "fingertip makes contact, the painted eyes in the "
                    "portrait shift — a single subtle blink. The flames in "
                    "the fireplace gutter low and blue. "
                    "[SCENE 6 - ESTABLISHING HOLD]: Camera pulls back from "
                    "macro to a wider medium shot that holds for the final "
                    "1.5 seconds — the fingertip is still pressed against "
                    "the carved gilded frame, but now we ALSO see, "
                    "surrounding it: the lower portion of the enormous oil "
                    "portrait above the mantle, the carved stone fireplace "
                    "with flames flickering low and blue, the dark wood-"
                    "panelled wall behind the portrait, suspended dust in "
                    "shafts of cold blue moonlight from the tall arched "
                    "windows on the left, warm amber firelight glow on the "
                    "protagonist's hand and trench coat, and a corner of "
                    "the high-backed leather chair beneath the portrait. "
                    "These environment elements — fingertip + gilded frame "
                    "+ portrait + fireplace + wood panelling + dust + "
                    "moonlight + firelight + leather chair — must ALL be "
                    "clearly visible in the final frame so they carry "
                    "forward into Act II. Cut to black. "
                    "16:9, photorealistic, cinematic, 35mm anamorphic, cold "
                    "moonlight blue and warm firelight amber, 15 seconds."
                ),
            },
            {
                "num": "II", "title": "Inside the Frame",
                "desc": "Same fingertip. Same frame. The wallpaper is from 1924.",
                "prompt": _p(
                    "Cinematic supernatural gothic short drama continuation, "
                    "shot on 35mm anamorphic, same cold moonlight blue and "
                    "warm firelight amber palette as [Image 2], same vast "
                    "stone fireplace, same wood-panelled mansion interior. "
                    "Slow dreamlike rhythm. "
                    "CHARACTER ANCHOR: The protagonist throughout this video "
                    "is still the person from [Image 1] — match their face, "
                    "hair, complexion, and identity exactly, regardless of "
                    "gender. "
                    "SET ANCHOR — THE ROOM: Every shot in this video MUST "
                    "take place inside the SAME mansion drawing room shown "
                    "in [Image 2]. Do NOT invent a new room. Reuse, "
                    "shot-for-shot, the macro environmental elements visible "
                    "in [Image 2]: (a) the SAME enormous oil portrait "
                    "hanging above the SAME carved stone fireplace, framed "
                    "by the SAME ornate gilded carved wooden frame the "
                    "fingertip is touching; (b) the SAME high-backed leather "
                    "chair positioned directly beneath the portrait, "
                    "matching the chair the figure sits in inside the "
                    "painting; (c) the SAME dark wood-panelled walls; "
                    "(d) the SAME tall arched windows on the left wall "
                    "casting cold blue moonlight; (e) the SAME parquet "
                    "wooden floor; (f) the SAME suspended dust motes drifting "
                    "in the moonlight shafts; (g) the SAME firelight amber "
                    "glow from the fireplace. The ONLY environmental shift "
                    "permitted is a slow temporal bleed: the faded peeling "
                    "Victorian wallpaper visible at the edges of [Image 2] "
                    "gradually transitions, over the first 4 seconds, into "
                    "fresh rich 1924 emerald-and-gold damask wallpaper, and "
                    "an upright wooden phonograph in the corner (previously "
                    "hidden under a white dust cloth in [Image 2]) is now "
                    "uncovered and softly hissing a wordless instrumental "
                    "swell. The white dust cloths covering furniture in "
                    "[Image 2] slowly dissolve away. The room is the SAME "
                    "room — only the era changes. "
                    "FRAME-MATCH ANCHOR: This video opens on the EXACT same "
                    "frame that closed [Image 2] — a medium shot of the "
                    "protagonist's right index fingertip pressed against the "
                    "carved gilded wooden frame of the enormous portrait, "
                    "with the fireplace, wood panelling, leather chair, "
                    "moonlight, and dust all visible in the same positions. "
                    "Camera position, lens, focal length, framing, and "
                    "lighting MUST match [Image 2] precisely for the first "
                    "1.5 seconds so the cut is invisible. "
                    "[SCENE 1 - THE SHIFT, FREEZE-THEN-DOLLY]: For the first "
                    "1.5 seconds, hold a perfect static replica of the final "
                    "frame of [Image 2] — fingertip on the gilded frame, "
                    "portrait above, fireplace, leather chair, wood panelling, "
                    "dust, moonlight, firelight all in identical positions. "
                    "No cut. From 1.5s to 4.0s, slowly and continuously dolly "
                    "the camera back from medium to wide, no edit, revealing "
                    "the SAME drawing room from [Image 2] — but the wallpaper "
                    "around the fireplace is now blooming from peeling "
                    "Victorian into rich emerald-and-gold 1924 damask, the "
                    "white dust cloths are dissolving from the furniture, "
                    "and the upright phonograph in the corner has become "
                    "uncovered and is softly playing. The protagonist from "
                    "[Image 1] is still standing in the SAME posture as "
                    "[Image 2], fingertip still pressed against the SAME "
                    "gilded frame on the SAME portrait above the SAME "
                    "fireplace. "
                    "[SCENE 2 - THE COAT]: They look down at themselves. "
                    "Cut to a tight insert of their torso (face from [Image "
                    "1] still visible at top of frame) — the modern charcoal "
                    "trench coat from [Image 2] has dissolved. In its place, "
                    "they are now wearing elegant 1920s charcoal evening "
                    "attire — the SAME outfit as the figure in the portrait. "
                    "Their hands are bare except for a single heavy gold "
                    "signet ring on the right little finger that was not "
                    "there before. Background remains the SAME wood panelling "
                    "and SAME firelight amber glow from [Image 2]. "
                    "[SCENE 3 - THE MIRROR]: They walk slowly across the "
                    "SAME parquet floor from [Image 2] to a tall gilded "
                    "full-length mirror on the opposite wall of the SAME "
                    "drawing room. Slow push-in on the reflection of the "
                    "face from [Image 1] — unchanged — but the room "
                    "reflected behind them is the SAME room from [Image 2] "
                    "now restored to 1924: the SAME fireplace, the SAME "
                    "portrait above it, the SAME high-backed leather chair, "
                    "the SAME tall arched windows — but through those SAME "
                    "windows, gas-lamps now flicker along a cobblestone "
                    "street where, in [Image 2], the courtyard was overgrown "
                    "and dark. "
                    "[SCENE 4 - THE LETTER]: They turn from the mirror and "
                    "walk back toward the SAME fireplace from [Image 2]. On "
                    "the SAME high-backed leather chair beneath the SAME "
                    "portrait — the chair that matches the painting — a "
                    "single sealed cream envelope rests on the seat, wax-"
                    "sealed with the letter A. They approach slowly and "
                    "pick it up. Macro insert close-up of the front of the "
                    "envelope, written in elegant fountain-pen cursive: "
                    "their own full name, addressed to themselves, dated "
                    "'October 1924.' Behind the envelope, the SAME gilded "
                    "carved frame of the SAME portrait is visible, the SAME "
                    "fireplace amber glow lights the paper. "
                    "[SCENE 5 - THE FOOTSTEPS]: They begin to break the wax "
                    "seal. From the hallway behind them — the SAME wood-"
                    "panelled hallway leading off the SAME drawing room — "
                    "slow measured footsteps approach across the SAME "
                    "parquet floor from [Image 2]. Tight close-up on the "
                    "face from [Image 1]: they freeze. The SAME firelight "
                    "from the SAME fireplace flickers across their cheek. "
                    "The footsteps stop directly behind them. A voice — "
                    "warm, familiar, impossible — speaks softly: 'I have "
                    "been waiting for you for a hundred years.' Slow push-in "
                    "on the face from [Image 1] as their eyes lift, "
                    "beginning to turn — cut to black before we see who is "
                    "behind them. "
                    "Final beat: text card appears in elegant antique gold "
                    "serif lettering: 'THE HOUSE NEVER FORGOT YOUR NAME.' "
                    "ENVIRONMENT LOCK: every single shot in this video must "
                    "take place inside the SAME drawing room — the SAME "
                    "stone fireplace, the SAME enormous oil portrait above "
                    "the mantle, the SAME ornate gilded carved wooden frame, "
                    "the SAME high-backed leather chair beneath the portrait, "
                    "the SAME wood-panelled walls, the SAME tall arched "
                    "windows, the SAME parquet floor — as appear in "
                    "[Image 2]. Do NOT invent a new room, new fireplace, "
                    "new portrait, new chair, or new windows. The mansion "
                    "is the same mansion; only the era around the "
                    "protagonist shifts. "
                    "16:9, photorealistic, cinematic, 35mm anamorphic, cold "
                    "moonlight blue and warm firelight amber, 15 seconds."
                ),
            },
        ],
    },

}


# ──────────────────────────────────────────────────────────
# CSS  (the heavy lifting for the aesthetic)
# ──────────────────────────────────────────────────────────
CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;0,400;0,500;0,600;1,400;1,500&family=JetBrains+Mono:wght@300;400;500&display=swap');

/* Hide Streamlit chrome */
header[data-testid="stHeader"] { display: none; }
.stDeployButton { display: none !important; }
footer { display: none !important; }
#MainMenu { display: none; }
section[data-testid="stSidebar"] { display: none; }
[data-testid="stToolbar"] { display: none; }
[data-testid="stDecoration"] { display: none; }

/* Body */
html, body, [data-testid="stAppViewContainer"] {
    background: #0A0907 !important;
    color: #F5F0E8 !important;
}
[data-testid="stAppViewContainer"] {
    background: #0A0907 !important;
}
.block-container {
    max-width: 1280px !important;
    padding-top: 3rem !important;
    padding-bottom: 3rem !important;
}

/* Default text */
.stMarkdown, .stMarkdown * { color: #F5F0E8; }
.stMarkdown p { font-family: 'Cormorant Garamond', serif; font-weight: 500; font-size: 1.18rem; line-height: 1.65; color: #e0dacc; }

/* Headings → serif */
h1, h2, h3, h4 {
    font-family: 'Cormorant Garamond', serif !important;
    color: #F5F0E8 !important;
    font-weight: 400 !important;
    letter-spacing: -0.01em !important;
}

/* Buttons — cinematic mono labels */
.stButton > button, .stDownloadButton > button {
    background: #F5F0E8 !important;
    color: #0A0A0A !important;
    border: none !important;
    border-radius: 0 !important;
    font-family: 'JetBrains Mono', monospace !important;
    font-size: 13px !important;
    font-weight: 500 !important;
    letter-spacing: 0.22em !important;
    text-transform: uppercase !important;
    padding: 1.05rem 2rem !important;
    transition: all 0.25s ease !important;
    box-shadow: none !important;
    min-height: 52px !important;
}
.stButton > button:hover, .stDownloadButton > button:hover {
    background: #FFFFFF !important;
    transform: translateY(-1px);
    color: #0A0A0A !important;
}
.stButton > button:disabled {
    background: rgba(245,240,232,0.1) !important;
    color: rgba(245,240,232,0.3) !important;
    cursor: not-allowed !important;
}

/* Secondary button variant via [kind="secondary"] */
.stButton > button[kind="secondary"] {
    background: transparent !important;
    color: #FFFFFF !important;
    border: 1px solid rgba(245,240,232,0.4) !important;
    font-size: 14px !important;
    letter-spacing: 0.2em !important;
    padding: 1.15rem 2rem !important;
    min-height: 58px !important;
}
.stButton > button[kind="secondary"]:hover {
    border-color: #F5F0E8 !important;
    background: rgba(245,240,232,0.06) !important;
    color: #FFFFFF !important;
}

/* File uploader */
[data-testid="stFileUploader"] section {
    background: transparent !important;
    border: 1px dashed rgba(245,240,232,0.25) !important;
    border-radius: 0 !important;
    padding: 4rem 2rem !important;
}
[data-testid="stFileUploader"] section:hover {
    border-color: rgba(245,240,232,0.5) !important;
}
[data-testid="stFileUploader"] section * {
    font-family: 'JetBrains Mono', monospace !important;
    font-size: 11px !important;
    letter-spacing: 0.2em !important;
    text-transform: uppercase !important;
    color: #d8d2c6 !important;
}
[data-testid="stFileUploader"] section button {
    background: transparent !important;
    color: #F5F0E8 !important;
    border: 1px solid rgba(245,240,232,0.3) !important;
    border-radius: 0 !important;
}
[data-testid="stFileUploaderDropzoneInstructions"] svg { display: none; }

/* Progress bars */
[data-testid="stProgress"] > div > div {
    background: rgba(245,240,232,0.08) !important;
    border-radius: 0 !important;
    height: 2px !important;
}
[data-testid="stProgress"] > div > div > div > div {
    background: #F5F0E8 !important;
    border-radius: 0 !important;
}

/* Custom utility classes used in markdown blocks */
.mono { font-family: 'JetBrains Mono', monospace; font-size: 11px; letter-spacing: 0.22em; text-transform: uppercase; color: #b8b1a3; font-weight: 400; }
.mono-bright { font-family: 'JetBrains Mono', monospace; font-size: 11px; letter-spacing: 0.22em; text-transform: uppercase; color: #FFFFFF; font-weight: 500; }
.serif-display { font-family: 'Cormorant Garamond', serif; font-weight: 500; line-height: 0.95; letter-spacing: -0.01em; }
.serif-italic { font-family: 'Cormorant Garamond', serif; font-style: italic; font-weight: 500; color: #c8c2b6; }
.serif-body { font-family: 'Cormorant Garamond', serif; font-weight: 500; line-height: 1.65; color: #e0dacc; font-size: 1.18rem; }
.amber { color: #f5b800; }

/* Hairline divider */
.hairline { height: 1px; background: rgba(245,240,232,0.1); margin: 1rem 0; }

/* Theme card */
.theme-card {
    position: relative;
    overflow: hidden;
    margin-bottom: 1rem;
}
.theme-card-banner {
    position: relative;
    height: 180px;
    overflow: hidden;
}
.theme-card-body { padding: 1.25rem 0 0.5rem 0; }
.theme-card-acts { display: grid; grid-template-columns: 1fr 1fr; gap: 1.75rem; padding: 1rem 0; border-top: 1px solid rgba(245,240,232,0.1); }

/* Corner registration marks */
.corner { position: absolute; width: 16px; height: 16px; border: 1px solid rgba(245,240,232,0.5); }
.corner.tl { top: 0; left: 0; border-right: none; border-bottom: none; }
.corner.tr { top: 0; right: 0; border-left: none; border-bottom: none; }
.corner.bl { bottom: 0; left: 0; border-right: none; border-top: none; }
.corner.br { bottom: 0; right: 0; border-left: none; border-top: none; }

/* Film grain */
body::before {
    content: '';
    position: fixed;
    inset: 0;
    pointer-events: none;
    z-index: 9999;
    opacity: 0.12;
    mix-blend-mode: overlay;
    background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 200 200'><filter id='n'><feTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='3' stitchTiles='stitch'/><feColorMatrix values='0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0.5 0'/></filter><rect width='100%' height='100%' filter='url(%23n)'/></svg>");
}

/* Hero typography */
.hero-h1 {
    font-family: 'Cormorant Garamond', serif;
    font-size: clamp(3rem, 8vw, 7.5rem);
    line-height: 0.92;
    color: #F5F0E8;
    letter-spacing: -0.01em;
    margin: 0;
}
.hero-h1 .it { font-style: italic; color: #c8c2b6; }

/* Section banner */
.banner-bar {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    margin-bottom: 3rem;
}
</style>
"""

# ──────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────
def b64(data: bytes, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def step_badge(n: int, total: int, label: str) -> str:
    return (
        f'<div style="display:flex;align-items:baseline;gap:12px;" class="mono">'
        f'<span>STEP</span><span class="mono-bright">{n:02d}</span>'
        f'<span style="opacity:0.5">/ {total:02d}</span>'
        f'<span style="display:inline-block;width:48px;height:1px;background:#3a352d;margin:0 8px;"></span>'
        f'<span class="mono-bright">{label}</span></div>'
    )


# ──────────────────────────────────────────────────────────
# SEEDANCE / TOS / FFMPEG
# ──────────────────────────────────────────────────────────
def submit_seedance_task(photo_url: str, prompt: str, secondary_ref: str | None = None) -> str:
    """Submit a Seedance 2.0 R2V task. The image roles are positional:
       reference_image[0] → [Image 1] in the prompt (customer portrait)
       reference_image[1] → [Image 2] in the prompt (last frame of Act I)
       This matches the official BytePlus fruit-tea R2V example."""
    content: list[dict] = [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": photo_url}, "role": "reference_image"},
    ]
    if secondary_ref:
        content.append({"type": "image_url", "image_url": {"url": secondary_ref}, "role": "reference_image"})
    payload = {
        "model": MODEL_ID, "content": content,
        "generate_audio": True, "ratio": ASPECT_RATIO,
        "duration": CLIP_DURATION, "resolution": RESOLUTION, "watermark": False,
    }
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {ARK_API_KEY}"}
    r = httpx.post(ARK_ENDPOINT, json=payload, headers=headers, timeout=60)
    if r.status_code >= 400:
        raise RuntimeError(f"Seedance submit {r.status_code}: {r.text}\n--- payload photo_url: {photo_url}")
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
    """Upload a file to TOS and return a presigned GET URL (24h TTL).

    Seedance fetches the URL anonymously, so we need a signed URL rather than
    a raw bucket path. Matches the pattern used in the ecommerce-ad-workflow.
    """
    import tos as _tos
    client = _tos_client()
    with open(local_path, "rb") as f:
        client.put_object(TOS_BUCKET, key, content=f, content_type=content_type)
    out = client.pre_signed_url(
        _tos.HttpMethodType.Http_Method_Get,
        TOS_BUCKET,
        key,
        expires=86400,   # 24 hours — plenty of room for the pipeline + audit
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


# ──────────────────────────────────────────────────────────
# MODELARK ASSET LIBRARY  (AK/SK signed Open API)
# Seedance 2.0 blocks real human faces in raw reference_image URLs.
# To use a customer's actual face, upload to the asset library and
# pass asset://<id> instead. The library treats uploads as trusted.
#
# Signing: Volcano Engine v4 / HMAC-SHA256 — implemented inline so we
# don't pin to a specific SDK version. No external dependencies beyond
# httpx + stdlib.
# ──────────────────────────────────────────────────────────
ARK_OPENAPI_HOST = "ark.ap-southeast-1.byteplusapi.com"
ARK_SERVICE = "ark"


def _sign_ark_request(method: str, query: dict, body_str: str) -> tuple[str, dict]:
    """Sign a BytePlus Open API request (Volcano v4 / HMAC-SHA256).

    Returns (url, headers) ready to pass to httpx. The query string built
    here MUST match exactly what's sent on the wire, so we URL-encode
    here and assemble the final URL ourselves (no httpx params= magic).
    """
    now = datetime.now(timezone.utc)
    x_date = now.strftime("%Y%m%dT%H%M%SZ")
    short_date = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(body_str.encode("utf-8")).hexdigest()

    # Canonical query — sorted by key, RFC 3986 unreserved chars only
    canonical_query = "&".join(
        f"{quote(k, safe='-._~')}={quote(str(v), safe='-._~')}"
        for k, v in sorted(query.items())
    )

    # Canonical headers — lowercase keys, sorted, trailing newline per line
    headers_to_sign = {
        "content-type": "application/json",
        "host": ARK_OPENAPI_HOST,
        "x-content-sha256": payload_hash,
        "x-date": x_date,
    }
    signed_headers_list = sorted(headers_to_sign.keys())
    signed_headers = ";".join(signed_headers_list)
    canonical_headers = "".join(f"{k}:{headers_to_sign[k]}\n" for k in signed_headers_list)

    # Canonical request → string-to-sign
    canonical_request = "\n".join([
        method,
        "/",                                   # path
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

    # Signing key chain: SK → date → region → service → "request"
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
    """Call a ModelArk Open API action with AK/SK signing. Returns Result dict."""
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
    """Return a usable asset group ID, creating one if not configured/found."""
    global ARK_ASSET_GROUP_ID
    if ARK_ASSET_GROUP_ID:
        return ARK_ASSET_GROUP_ID

    # Try to find an existing group by name first (idempotent reuse)
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
        pass  # fall through to create

    # Create it
    result = _ark_call("CreateAssetGroup", {
        "Name": ARK_ASSET_GROUP_NAME,
        "Description": "Customer portrait subjects uploaded via Seedance Studio",
        "ProjectName": ARK_PROJECT_NAME,
    })
    ARK_ASSET_GROUP_ID = result["Id"]
    return ARK_ASSET_GROUP_ID


def upload_to_asset_library(image_url: str, name: str | None = None,
                            on_step=None) -> str:
    """Upload an image (referenced by URL) to ModelArk's asset library,
    poll until Active, and return its asset URI (asset://<id>) suitable
    for passing to Seedance as a reference_image URL.

    The input image_url must be publicly accessible — ModelArk fetches
    it server-side. Our TOS presigned URLs work fine.

    Args:
        image_url: publicly fetchable URL of the source image
        name:      optional, shows up in the BytePlus console for debugging
        on_step:   optional callback(str) — called at each diagram step
                   so the UI can surface progress
    """
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
        payload["Name"] = name[:64]   # docs cap Name at 64 chars
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
        # Still Processing → wait and retry
        elapsed = int(time.time() - start)
        _step(f"PROCESSING ASSET · {asset_id[-8:]} · {elapsed}s")
        time.sleep(ASSET_POLL_INTERVAL)


def download_url(url: str, dest: str) -> None:
    with httpx.stream("GET", url, timeout=120, follow_redirects=True) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)


def extract_last_frame(video_path: str, image_path: str) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-sseof", "-0.1", "-i", video_path, "-vframes", "1", "-q:v", "2", image_path],
        check=True, capture_output=True,
    )


def concat_clips(clip_paths: list[str], output_path: str) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        for p in clip_paths:
            f.write(f"file '{Path(p).resolve()}'\n")
        list_file = f.name
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file,
             "-c:v", "libx264", "-preset", "medium", "-crf", "20",
             "-c:a", "aac", "-b:a", "192k", "-r", "30", output_path],
            check=True, capture_output=True,
        )
    finally:
        os.unlink(list_file)


# ──────────────────────────────────────────────────────────
# EMAIL DELIVERY
# Sends the customer a styled HTML email with their video link.
# The video itself stays on TOS (linked, not attached) so the
# email is small and renders in any client.
# ──────────────────────────────────────────────────────────
EMAIL_RE = re.compile(r"^[\w\.\+\-]+@[\w\-]+(\.[\w\-]+)+$")


def is_valid_email(addr: str) -> bool:
    return bool(EMAIL_RE.match((addr or "").strip()))


def send_video_email(to_email: str, theme: dict, video_url: str) -> tuple[bool, str]:
    """Send the customer their video link as a styled HTML email.
    Returns (ok, message)."""
    if not EMAIL_ENABLED:
        return False, "Email is not configured on this server."
    if not is_valid_email(to_email):
        return False, "That doesn't look like a valid email address."

    subject = f"Your {theme['name']} short film is ready"
    sig = theme["signature"]
    paper = theme["paper"]
    name = theme["name"]
    tagline = theme["tagline"]

    text_body = (
        f"Your {name} short film is ready.\n\n"
        f"{tagline}\n\n"
        f"Watch and download here: {video_url}\n\n"
        f"30 seconds · {RESOLUTION} · 16:9\n\n"
        f"— Seedance Studio"
    )

    html_body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#0A0907;font-family:Georgia,serif;color:#F5F0E8">
  <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#0A0907;padding:48px 16px">
    <tr><td align="center">
      <table width="560" cellpadding="0" cellspacing="0" border="0" style="max-width:560px;background:{paper};padding:48px 40px">
        <tr><td style="padding-bottom:32px">
          <div style="font-family:'Courier New',monospace;font-size:11px;letter-spacing:0.28em;color:{sig};text-transform:uppercase">A Short Film By Seedance</div>
        </td></tr>
        <tr><td style="padding-bottom:8px">
          <div style="font-family:Georgia,serif;font-size:44px;line-height:0.95;color:#FFFFFF;font-weight:500">{name}</div>
        </td></tr>
        <tr><td style="padding-bottom:36px">
          <div style="font-family:Georgia,serif;font-style:italic;font-size:18px;color:#c8c2b6">{tagline}</div>
        </td></tr>
        <tr><td style="padding-bottom:40px">
          <p style="font-family:Georgia,serif;font-size:17px;line-height:1.6;color:#ece6d8;margin:0">
            Your thirty-second short film is ready. Tap below to watch and download — the link works for the next 24 hours.
          </p>
        </td></tr>
        <tr><td align="center" style="padding-bottom:40px">
          <a href="{video_url}" target="_blank"
             style="display:inline-block;background:{sig};color:#0A0A0A;padding:18px 36px;font-family:'Courier New',monospace;font-size:13px;letter-spacing:0.22em;text-transform:uppercase;text-decoration:none;font-weight:500">
            Watch your film →
          </a>
        </td></tr>
        <tr><td style="border-top:1px solid rgba(245,240,232,0.1);padding-top:24px">
          <div style="font-family:'Courier New',monospace;font-size:10px;letter-spacing:0.28em;color:#a8a294;text-transform:uppercase">
            00:30 &nbsp;·&nbsp; {RESOLUTION.upper()} &nbsp;·&nbsp; 16:9
          </div>
        </td></tr>
      </table>
      <div style="font-family:Georgia,serif;font-style:italic;font-size:13px;color:#6a655c;padding-top:24px">
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
        "theme_id": None,
        "clip_status": [],     # list of dicts {act, status, progress, video_url}
        "final_url": None,
        "error": None,
        "email_sent_to": None,     # str of email address once sent
        "email_error": None,       # last email error message
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def goto(step: str):
    st.session_state.step = step
    st.rerun()


# ──────────────────────────────────────────────────────────
# SCREEN — WELCOME
# ──────────────────────────────────────────────────────────
def render_welcome():
    st.markdown(
        '<div class="banner-bar">'
        '  <div>'
        '    <div class="mono-bright">SEEDANCE STUDIO</div>'
        '    <div class="mono" style="margin-top:4px">v2.0 · AP-SOUTHEAST</div>'
        '  </div>'
        '  <div style="text-align:right">'
        f'    <div class="mono">30s · {RESOLUTION.upper()} · 16:9</div>'
        '    <div class="mono" style="margin-top:4px">EST. 2026</div>'
        '  </div>'
        '</div>',
        unsafe_allow_html=True,
    )

    st.markdown('<div class="mono" style="margin-bottom:32px">A SHORT FILM, FROM ONE PHOTOGRAPH</div>', unsafe_allow_html=True)
    st.markdown(
        '<h1 class="hero-h1">'
        'Become<br>'
        '<span class="it">the protagonist</span><br>'
        'of your own<br>'
        '<span style="color:#f5b800">— cinema.</span>'
        '</h1>',
        unsafe_allow_html=True,
    )
    st.markdown(
        '<p class="serif-body" style="max-width:520px;margin-top:2.5rem;font-size:1.2rem">'
        "Upload a single portrait. Choose a world. We&rsquo;ll generate a thirty-second sci-fi short film of you inside it, in two acts."
        f'<br><span class="mono" style="margin-top:14px;display:inline-block">Output · {RESOLUTION.upper()} · 16:9 · ~3 min wait</span>'
        '</p>',
        unsafe_allow_html=True,
    )

    st.markdown("<div style='height:48px'></div>", unsafe_allow_html=True)

    cols = st.columns([1, 6])
    with cols[0]:
        if st.button("Begin →", key="begin", use_container_width=True):
            goto("capture")

    if DEMO_MODE:
        st.markdown(
            '<div class="mono" style="margin-top:48px;padding:12px 16px;border:1px solid rgba(245,184,0,0.3);display:inline-block">'
            '<span class="amber">● DEMO MODE</span>'
            '<span style="margin-left:16px">Set ARK_API_KEY + TOS_* env vars to enable real generation</span>'
            '</div>',
            unsafe_allow_html=True,
        )
    elif USE_ASSET_LIBRARY:
        st.markdown(
            '<div class="mono" style="margin-top:48px;padding:12px 16px;border:1px solid rgba(0,229,255,0.3);display:inline-block">'
            '<span style="color:#00E5FF">● ASSET LIBRARY ACTIVE</span>'
            '<span style="margin-left:16px;color:#d8d2c6">Real-face photos enabled via ModelArk asset library</span>'
            '</div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            '<div class="mono" style="margin-top:48px;padding:12px 16px;border:1px solid rgba(245,184,0,0.3);display:inline-block">'
            '<span class="amber">● FACE-FREE MODE</span>'
            '<span style="margin-left:16px;color:#d8d2c6">Set ARK_AK + ARK_SK to enable real-face uploads</span>'
            '</div>',
            unsafe_allow_html=True,
        )


# ──────────────────────────────────────────────────────────
# SCREEN — CAPTURE
# ──────────────────────────────────────────────────────────
def render_capture():
    cols = st.columns([6, 1])
    with cols[0]:
        st.markdown(step_badge(1, 3, "THE SUBJECT"), unsafe_allow_html=True)
    with cols[1]:
        if st.button("← Back", key="cap_back", type="secondary"):
            goto("welcome")

    st.markdown("<div style='height:48px'></div>", unsafe_allow_html=True)

    left, right = st.columns([1, 1], gap="large")

    with left:
        st.markdown(
            '<h2 style="font-size:3.5rem;line-height:0.95;margin:0">'
            'One portrait.<br><span class="serif-italic" style="font-size:3.5rem">That&rsquo;s all we need.</span>'
            '</h2>'
            '<p class="serif-body" style="margin-top:2rem;max-width:420px">'
            "Best results from a clear, well-lit photograph of one person, facing the camera. "
            "We&rsquo;ll use this as the reference for every shot of the film."
            '</p>'
            '<div style="margin-top:2.5rem;display:grid;grid-template-columns:1fr 1fr;gap:12px 32px">'
            '<div class="mono">→ ONE PERSON, CENTER</div>'
            '<div class="mono">→ CLEAR LIGHTING</div>'
            '<div class="mono">→ JPG / PNG / WEBP</div>'
            '<div class="mono">→ 1024×1024 OR LARGER</div>'
            '</div>',
            unsafe_allow_html=True,
        )

    with right:
        if st.session_state.photo_bytes is None:
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
        else:
            # Cinematic frame preview
            img_b64 = b64(st.session_state.photo_bytes)
            size_kb = len(st.session_state.photo_bytes) // 1024
            st.markdown(
                f'<div style="position:relative;width:100%;aspect-ratio:3/4;max-width:420px">'
                f'  <div class="mono" style="position:absolute;top:-22px;left:0;right:0;display:flex;justify-content:space-between;color:#d8d2c6">'
                f'    <span>+</span><span>16:9 OUTPUT</span><span>+</span>'
                f'  </div>'
                f'  <img src="{img_b64}" style="width:100%;height:100%;object-fit:cover;display:block;outline:1px solid rgba(245,240,232,0.3)" />'
                f'  <div class="corner tl"></div><div class="corner tr"></div>'
                f'  <div class="corner bl"></div><div class="corner br"></div>'
                f'  <div class="mono" style="display:flex;justify-content:space-between;margin-top:16px">'
                f'    <span class="mono-bright">SUBJECT_01.JPG</span>'
                f'    <span>{size_kb} KB</span>'
                f'  </div>'
                f'</div>',
                unsafe_allow_html=True,
            )
            if st.button("Replace photo", key="replace", type="secondary"):
                st.session_state.photo_bytes = None
                st.session_state.photo_name = None
                st.rerun()

    st.markdown("<div style='height:64px'></div>", unsafe_allow_html=True)

    cols = st.columns([6, 1.4])
    with cols[1]:
        if st.button("Choose a world →", key="cap_next", use_container_width=True, disabled=st.session_state.photo_bytes is None):
            goto("themes")


# ──────────────────────────────────────────────────────────
# SCREEN — THEMES
# ──────────────────────────────────────────────────────────
def theme_card_html(theme: dict, photo_bytes: bytes | None) -> str:
    photo_layer = ""
    if photo_bytes:
        photo_layer = (
            f'<img src="{b64(photo_bytes)}" '
            f'style="position:absolute;inset:0;width:100%;height:100%;object-fit:cover;opacity:0.42;filter:grayscale(0.3) contrast(1.15);" />'
            f'<div style="position:absolute;inset:0;background:linear-gradient(180deg,transparent 35%,{theme["paper"]} 98%);"></div>'
        )

    acts_html = "".join(
        f'<div>'
        f'<div style="font-family:JetBrains Mono,monospace;font-size:12px;letter-spacing:0.22em;text-transform:uppercase;color:{theme["signature"]};font-weight:500">ACT {a["num"]}</div>'
        f'<div style="margin-top:8px;font-family:Cormorant Garamond,serif;font-weight:500;color:#FFFFFF;font-size:1.35rem;line-height:1.1">{a["title"]}</div>'
        f'<div style="margin-top:6px;font-family:Cormorant Garamond,serif;font-style:italic;font-weight:500;color:#c8c2b6;font-size:1rem;line-height:1.35">{a["desc"]}</div>'
        f'</div>'
        for a in theme["acts"]
    )
    keywords_html = "&nbsp;&nbsp;·&nbsp;&nbsp;".join(theme["keywords"])

    return (
        f'<div class="theme-card" style="background:{theme["paper"]};outline:1px solid rgba(245,240,232,0.10)">'
        # Hero banner — larger, more dramatic
        f'  <div class="theme-card-banner" style="height:260px;background:radial-gradient(circle at 30% 30%,{theme["signature"]}66,transparent 55%),radial-gradient(circle at 70% 70%,{theme["accent"]}55,transparent 55%),{theme["paper"]}">'
        f'    {photo_layer}'
        f'    <div style="position:absolute;top:18px;left:20px;font-family:JetBrains Mono,monospace;font-size:12px;letter-spacing:0.28em;text-transform:uppercase;color:{theme["signature"]};font-weight:500">THEME {theme["code"]}</div>'
        f'    <div style="position:absolute;top:18px;right:20px;font-family:JetBrains Mono,monospace;font-size:12px;letter-spacing:0.28em;color:#e8e2d6;font-weight:500">00:30 · 2 ACTS</div>'
        # Big title overlaid on the banner so it reads at a glance
        f'    <div style="position:absolute;bottom:20px;left:20px;right:20px">'
        f'      <div style="font-family:Cormorant Garamond,serif;font-weight:500;font-size:2.6rem;color:#FFFFFF;line-height:0.95;letter-spacing:-0.01em">{theme["name"]}</div>'
        f'      <div style="margin-top:6px;font-family:Cormorant Garamond,serif;font-style:italic;font-weight:500;color:#e8e2d6;font-size:1.2rem">{theme["tagline"]}</div>'
        f'    </div>'
        f'  </div>'
        # Body — bigger, more readable description
        f'  <div class="theme-card-body" style="padding:22px 22px 0">'
        f'    <p style="font-family:Cormorant Garamond,serif;font-weight:500;font-size:1.18rem;line-height:1.55;color:#ece6d8;margin:0">{theme["description"]}</p>'
        f'    <div style="margin-top:18px;font-family:JetBrains Mono,monospace;font-size:11px;letter-spacing:0.22em;text-transform:uppercase;color:#a8a294;font-weight:500">{keywords_html}</div>'
        f'  </div>'
        # Acts strip with bigger labels
        f'  <div class="theme-card-acts" style="padding:20px 22px 22px;margin-top:18px">'
        f'    {acts_html}'
        f'  </div>'
        f'</div>'
    )


def render_themes():
    cols = st.columns([6, 1])
    with cols[0]:
        st.markdown(step_badge(2, 3, "THE WORLD"), unsafe_allow_html=True)
    with cols[1]:
        if st.button("← Back", key="th_back", type="secondary"):
            goto("capture")

    st.markdown("<div style='height:32px'></div>", unsafe_allow_html=True)

    left, right = st.columns([1.4, 1])
    with left:
        st.markdown(
            '<h2 style="font-size:3.5rem;line-height:0.95;margin:0">'
            'Which world<br><span class="serif-italic" style="font-size:3.5rem">do you walk into?</span>'
            '</h2>',
            unsafe_allow_html=True,
        )
    with right:
        st.markdown(
            '<p class="serif-body" style="max-width:340px">'
            "Each theme is a thirty-second short, structured in three acts. "
            "The aesthetic, score, and pace differ wildly."
            '</p>',
            unsafe_allow_html=True,
        )

    st.markdown("<div style='height:32px'></div>", unsafe_allow_html=True)

    # 2x2 grid of theme cards
    theme_list = list(THEMES.items())
    for row_start in (0, 2):
        c1, c2 = st.columns(2, gap="medium")
        for col, (tid, t) in zip((c1, c2), theme_list[row_start:row_start + 2]):
            with col:
                is_selected = st.session_state.theme_id == tid
                # If selected, highlight with thicker accent outline
                outline = f"outline:2px solid {t['signature']};outline-offset:6px" if is_selected else ""
                if outline:
                    st.markdown(f'<div style="{outline};margin-bottom:1.5rem">{theme_card_html(t, st.session_state.photo_bytes)}</div>', unsafe_allow_html=True)
                else:
                    st.markdown(theme_card_html(t, st.session_state.photo_bytes), unsafe_allow_html=True)
                # Wrap the select button in a div that bumps its size
                label = f"✓  {t['name']}  ·  CHOSEN" if is_selected else f"Choose  {t['name']}"
                st.markdown(f'<div class="theme-select-btn" data-selected="{is_selected}" data-color="{t["signature"]}">', unsafe_allow_html=True)
                if st.button(label, key=f"sel_{tid}", use_container_width=True, type="secondary"):
                    st.session_state.theme_id = tid
                    st.rerun()
                st.markdown('</div>', unsafe_allow_html=True)
                st.markdown("<div style='height:24px'></div>", unsafe_allow_html=True)

    st.markdown("<div style='height:48px'></div>", unsafe_allow_html=True)
    cols = st.columns([6, 1.6])
    with cols[1]:
        selected = st.session_state.theme_id
        label = f"Generate {THEMES[selected]['name']} →" if selected else "Select a world"
        if st.button(label, key="th_next", use_container_width=True, disabled=selected is None):
            goto("generating")


# ──────────────────────────────────────────────────────────
# SCREEN — GENERATING
# Clean single-state view. The 3-clip stitch happens internally
# to reach 30s (Seedance caps at 15s per call), but it's surfaced
# as one unified progress bar — no acts, no pipeline noise.
# ──────────────────────────────────────────────────────────
def render_generating():
    theme = THEMES[st.session_state.theme_id]

    st.markdown(step_badge(3, 3, "GENERATION"), unsafe_allow_html=True)
    st.markdown("<div style='height:48px'></div>", unsafe_allow_html=True)

    left, right = st.columns([1, 2.5], gap="large")

    # LEFT — photo + theme name only. No pipeline details.
    with left:
        if st.session_state.photo_bytes:
            st.markdown(
                f'<img src="{b64(st.session_state.photo_bytes)}" '
                f'style="width:100%;aspect-ratio:3/4;object-fit:cover;outline:1px solid rgba(245,240,232,0.15);filter:grayscale(0.3) contrast(1.05)" />',
                unsafe_allow_html=True,
            )
        st.markdown(
            f'<div class="mono" style="margin-top:28px">CURRENT THEME</div>'
            f'<div style="font-family:Cormorant Garamond,serif;font-weight:500;font-size:1.9rem;color:#F5F0E8;line-height:1;margin-top:10px">{theme["name"]}</div>'
            f'<div class="serif-italic" style="margin-top:6px">{theme["tagline"]}</div>',
            unsafe_allow_html=True,
        )

    # RIGHT — single placeholder that atomically redraws each tick.
    with right:
        slot = st.empty()
        start_time = time.time()
        # Mutable status line — updated by pipeline as it walks the diagram steps
        current_status = {"text": "PREPARING"}

        def set_status(msg: str):
            """Update the status line shown beneath the master timeline."""
            current_status["text"] = msg

        def render(pct: float):
            pct = max(0.0, min(1.0, pct))
            secs = int(pct * 30)
            elapsed = int(time.time() - start_time)
            elapsed_mm = elapsed // 60
            elapsed_ss = elapsed % 60
            dots = "." * ((int(time.time() * 2) % 3) + 1)
            with slot.container():
                st.markdown(
                    f'<h2 style="font-family:Cormorant Garamond,serif;font-weight:500;font-size:4.5rem;line-height:0.95;margin:0">'
                    f'Generating<span style="opacity:0.5">{dots}</span></h2>',
                    unsafe_allow_html=True,
                )
                # Elapsed time — big, prominent, the focal stat for the wait.
                st.markdown(
                    f'<div style="margin-top:24px;display:flex;align-items:baseline;gap:18px">'
                    f'  <div class="mono">ELAPSED</div>'
                    f'  <div style="font-family:JetBrains Mono,monospace;font-size:2.4rem;font-weight:500;color:{theme["signature"]};letter-spacing:0.04em">'
                    f'{elapsed_mm:02d}:{elapsed_ss:02d}</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
                st.markdown("<div style='height:36px'></div>", unsafe_allow_html=True)
                st.markdown(
                    f'<div style="display:flex;justify-content:space-between;margin-bottom:10px">'
                    f'  <div class="mono">MASTER TIMELINE</div>'
                    f'  <div class="mono" style="color:{theme["signature"]}">{int(pct*100)}% · 00:{secs:02d} / 00:30</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
                st.progress(pct)
                # Step-level status line — surfaces what the pipeline is doing
                # right now (matching the BytePlus asset-library diagram steps).
                st.markdown(
                    f'<div class="mono" style="margin-top:18px;color:#a8a294;text-align:center">'
                    f'  → {current_status["text"]}'
                    f'</div>',
                    unsafe_allow_html=True,
                )

        try:
            render(0.0)
            if DEMO_MODE:
                run_demo_pipeline(render, set_status)
            else:
                run_real_pipeline(render, set_status)
            render(1.0)
            time.sleep(0.4)
            goto("result")
        except Exception as e:
            st.session_state.error = str(e)
            st.error(f"Pipeline failed: {e}")
            if st.button("← Back", type="secondary"):
                goto("themes")


def run_demo_pipeline(render, set_status):
    """Mocked single-bar pipeline for demo mode."""
    set_status("DEMO MODE — simulating pipeline")
    for tick in range(40):
        render((tick + 1) / 40)
        time.sleep(0.15)
    st.session_state.final_url = None


def run_real_pipeline(render, set_status):
    """2 sequential R2V calls + ffmpeg stitch — reported as one unified bar.

    Walks the exact flow from the BytePlus diagram:
      1. Upload to TOS (staging)
      2. CreateAssetGroup / ListAssetGroups (idempotent reuse)
      3. CreateAsset → poll GetAsset until Active
      4. Submit Seedance generation with asset://<id>
      5. Stitch + upload master
    """
    theme = THEMES[st.session_state.theme_id]
    workdir = Path(tempfile.mkdtemp(prefix="seedance_"))

    # Step 1+2+3 — upload customer photo to TOS, then register in asset library
    if not st.session_state.photo_remote_url:
        set_status("STAGING PHOTO TO TOS")
        key = f"seedance/subjects/{uuid.uuid4().hex}.jpg"
        tos_url = upload_bytes_to_tos(
            st.session_state.photo_bytes, key, content_type="image/jpeg"
        )

        if USE_ASSET_LIBRARY:
            # Register in ModelArk asset library — the only path that lets
            # real human faces through Seedance 2.0 content moderation.
            try:
                asset_name = f"subject_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
                st.session_state.photo_remote_url = upload_to_asset_library(
                    tos_url, name=asset_name, on_step=set_status,
                )
            except Exception as e:
                raise RuntimeError(
                    f"Asset library upload failed: {e}\n\n"
                    f"Common causes:\n"
                    f"  • ARK_AK / ARK_SK not set or invalid\n"
                    f"  • 'Advanced Creation Rights' not purchased on this account\n"
                    f"  • Authorization letter not signed in the BytePlus console\n"
                    f"  • Photo rejected by asset moderation\n"
                ) from e
        else:
            set_status("USING RAW TOS URL (FACE-FREE MODE)")
            st.session_state.photo_remote_url = tos_url

    clip_paths: list[str] = []
    secondary_ref: str | None = None
    CLIPS_BUDGET = 0.90   # 0-90% spent on the clips, 90-100% on stitch+upload

    # Step 4 — generate each act using the asset:// URI as reference_image.
    # Positional convention (matches BytePlus R2V example):
    #   [Image 1] = photo_url (customer portrait, asset:// URI)
    #   [Image 2] = secondary_ref (last frame of previous act, asset:// URI)
    for i, act in enumerate(theme["acts"]):
        base = (i / NUM_CLIPS) * CLIPS_BUDGET
        slot_size = CLIPS_BUDGET / NUM_CLIPS

        set_status(f"SUBMITTING ACT {['I','II','III'][i]} TO SEEDANCE")
        task_id = submit_seedance_task(
            photo_url=st.session_state.photo_remote_url,
            prompt=act["prompt"],
            secondary_ref=secondary_ref,
        )
        set_status(f"GENERATING ACT {['I','II','III'][i]} · TASK {task_id[-8:]}")

        def on_progress(status, elapsed, _base=base, _slot=slot_size, _i=i):
            # Asymptotic clip progress 0→0.95 over ~75s, mapped to its slot.
            clip_pct = min(0.95, 1 - (0.5 ** (elapsed / 30)))
            render(_base + clip_pct * _slot)

        video_url = poll_seedance_task(task_id, on_progress=on_progress)
        set_status(f"DOWNLOADING ACT {['I','II','III'][i]}")
        clip_path = str(workdir / f"clip_{i+1}.mp4")
        download_url(video_url, clip_path)
        clip_paths.append(clip_path)
        render(base + slot_size)

        # Last-frame continuity reference for the next clip
        if i < NUM_CLIPS - 1:
            set_status("EXTRACTING CONTINUITY FRAME")
            frame_path = str(workdir / f"frame_{i+1}.jpg")
            extract_last_frame(clip_path, frame_path)
            key = f"seedance/frames/{uuid.uuid4().hex}.jpg"
            frame_tos_url = upload_to_tos(frame_path, key, content_type="image/jpeg")
            # Register frame as asset too — without this, Seedance will reject
            # it on the next clip if it contains the (now-transformed) face.
            if USE_ASSET_LIBRARY:
                frame_name = f"frame_{i+1}_{datetime.now().strftime('%H%M%S')}"
                secondary_ref = upload_to_asset_library(
                    frame_tos_url, name=frame_name, on_step=set_status,
                )
            else:
                secondary_ref = frame_tos_url

    # Step 5 — stitch + upload final master
    set_status("STITCHING WITH FFMPEG")
    render(CLIPS_BUDGET + 0.04)
    master_path = str(workdir / "master.mp4")
    concat_clips(clip_paths, master_path)

    set_status("UPLOADING FINAL MASTER")
    render(CLIPS_BUDGET + 0.07)
    key = f"seedance/final/{uuid.uuid4().hex}.mp4"
    st.session_state.final_url = upload_to_tos(master_path, key, content_type="video/mp4")
    set_status("COMPLETE")


# ──────────────────────────────────────────────────────────
# SCREEN — RESULT
# ──────────────────────────────────────────────────────────
def render_result():
    theme = THEMES[st.session_state.theme_id]

    cols = st.columns([3, 1])
    with cols[0]:
        st.markdown(
            f'<div class="mono">A SHORT FILM BY SEEDANCE</div>'
            f'<h2 style="font-size:5rem;line-height:1;margin:12px 0 0 0">{theme["name"]}</h2>'
            f'<div class="serif-italic" style="margin-top:8px;font-size:1.2rem">{theme["tagline"]}</div>',
            unsafe_allow_html=True,
        )
    with cols[1]:
        st.markdown(
            f'<div class="mono" style="text-align:right;color:{theme["signature"]}">● READY</div>'
            f'<div class="mono" style="text-align:right;margin-top:4px">00:30 · {RESOLUTION.upper()} · 16:9</div>',
            unsafe_allow_html=True,
        )

    st.markdown("<div style='height:32px'></div>", unsafe_allow_html=True)

    # Video stage
    if st.session_state.final_url:
        st.video(st.session_state.final_url)
    else:
        # Demo-mode poster
        photo_layer = ""
        if st.session_state.photo_bytes:
            photo_layer = (
                f'<img src="{b64(st.session_state.photo_bytes)}" '
                f'style="position:absolute;inset:0;width:100%;height:100%;object-fit:cover;opacity:0.55;filter:grayscale(0.2) contrast(1.05) saturate(1.1)" />'
                f'<div style="position:absolute;inset:0;background:linear-gradient(180deg,transparent 40%,{theme["paper"]}cc)"></div>'
            )
        st.markdown(
            f'<div style="position:relative;width:100%;aspect-ratio:16/9;background:{theme["paper"]};overflow:hidden">'
            f'  <div style="position:absolute;inset:0;background:radial-gradient(circle at 30% 40%,{theme["signature"]}55,transparent 55%),radial-gradient(circle at 70% 60%,{theme["accent"]}45,transparent 55%)"></div>'
            f'  {photo_layer}'
            f'  <div style="position:absolute;bottom:0;left:0;right:0;padding:32px">'
            f'    <div class="mono" style="color:{theme["signature"]}">A FILM ABOUT YOU</div>'
            f'    <div style="font-family:Cormorant Garamond,serif;font-size:3.5rem;color:#F5F0E8;line-height:0.95;margin-top:8px">{theme["name"]}</div>'
            f'  </div>'
            f'  <div class="corner tl"></div><div class="corner tr"></div>'
            f'  <div class="corner bl"></div><div class="corner br"></div>'
            f'</div>'
            f'<div class="mono" style="margin-top:16px;color:{theme["signature"] if not DEMO_MODE else "#f5b800"}">'
            f'  {"● DEMO MODE — poster shown, configure env vars for real video" if DEMO_MODE else ""}'
            f'</div>',
            unsafe_allow_html=True,
        )

    # 2-act strip
    cols = st.columns(2, gap="small")
    for col, act in zip(cols, theme["acts"]):
        with col:
            st.markdown(
                f'<div style="background:#0F0E0C;padding:18px 20px;border-top:2px solid {theme["signature"]};margin-top:20px">'
                f'  <div class="mono" style="color:{theme["signature"]}">ACT {act["num"]} · {act["title"].upper()}</div>'
                f'  <div class="serif-italic" style="margin-top:10px;font-size:1.05rem">{act["desc"]}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

    # ─────────────────────────────────────────────────────
    # EMAIL THIS TO ME — only renders if EMAIL_ENABLED + we have a real video
    # ─────────────────────────────────────────────────────
    if EMAIL_ENABLED and st.session_state.final_url:
        st.markdown("<div style='height:40px'></div>", unsafe_allow_html=True)
        st.markdown(
            f'<div style="display:flex;align-items:baseline;gap:14px;margin-bottom:12px">'
            f'  <div class="mono" style="color:{theme["signature"]}">SEND TO MY EMAIL</div>'
            f'  <div class="serif-italic" style="font-size:0.95rem;color:#a8a294">Get the link in your inbox — useful if you want to watch on your phone or share later.</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

        if st.session_state.email_sent_to:
            # Sent state
            st.markdown(
                f'<div style="padding:18px 22px;background:#0F0E0C;border-left:3px solid {theme["signature"]}">'
                f'  <div class="mono" style="color:{theme["signature"]}">✓ SENT</div>'
                f'  <div class="serif-body" style="margin-top:6px;font-size:1.05rem">Check <strong style="color:#FFFFFF">{st.session_state.email_sent_to}</strong> — should arrive in a moment.</div>'
                f'</div>',
                unsafe_allow_html=True,
            )
        else:
            ec1, ec2 = st.columns([3, 1])
            with ec1:
                email = st.text_input(
                    "your email",
                    placeholder="you@example.com",
                    key="email_input",
                    label_visibility="collapsed",
                )
            with ec2:
                if st.button("Send →", key="send_email", use_container_width=True):
                    if not is_valid_email(email):
                        st.session_state.email_error = "That doesn't look like a valid email address."
                    else:
                        ok, msg = send_video_email(email, theme, st.session_state.final_url)
                        if ok:
                            st.session_state.email_sent_to = email
                            st.session_state.email_error = None
                        else:
                            st.session_state.email_error = msg
                        st.rerun()

            if st.session_state.email_error:
                st.markdown(
                    f'<div class="mono" style="margin-top:10px;color:#FF6B6B">✗ {st.session_state.email_error}</div>',
                    unsafe_allow_html=True,
                )

    st.markdown("<div style='height:40px'></div>", unsafe_allow_html=True)

    cols = st.columns([1.2, 1, 3, 1.6])
    with cols[0]:
        if st.session_state.final_url:
            st.markdown(
                f'<a href="{st.session_state.final_url}" target="_blank" style="text-decoration:none">'
                f'<button style="width:100%;background:{theme["signature"]};color:#0A0A0A;border:none;font-family:JetBrains Mono,monospace;font-size:13px;font-weight:500;letter-spacing:0.22em;text-transform:uppercase;padding:1.05rem 2rem;cursor:pointer;min-height:52px">Download .mp4</button>'
                f'</a>',
                unsafe_allow_html=True,
            )
        else:
            st.button("Download .mp4", disabled=True, use_container_width=True)
    with cols[1]:
        if st.button("Share", type="secondary", use_container_width=True):
            st.toast("Link copied")
    with cols[3]:
        if st.button("Make another →", key="restart", use_container_width=True):
            for k in ("step", "photo_bytes", "photo_name", "photo_remote_url",
                      "theme_id", "clip_status", "final_url", "error",
                      "email_sent_to", "email_error"):
                st.session_state.pop(k, None)
            init_state()
            st.rerun()


# ──────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────
def main():
    st.set_page_config(
        page_title="Seedance Studio",
        page_icon="🎬",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    st.markdown(CSS, unsafe_allow_html=True)
    init_state()

    {
        "welcome": render_welcome,
        "capture": render_capture,
        "themes": render_themes,
        "generating": render_generating,
        "result": render_result,
    }[st.session_state.step]()


if __name__ == "__main__":
    main()