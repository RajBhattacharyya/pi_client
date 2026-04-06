#!/usr/bin/env python3
"""
CogniLens Pi Main Client — Unified WebSocket connection.
Handles wake-word audio, voice-triggered camera actions, gyro, and live responses in a single persistent session.

Dependencies on Pi:
    pip3 install websockets requests RPi.GPIO smbus2 picamera2
    sudo apt install alsa-utils mpg123 libcamera-apps

Usage:
    python3 main_client.py --server ws://192.168.1.100:8000
"""
import asyncio
import argparse
import base64
import re
from difflib import SequenceMatcher
import json
import subprocess
import tempfile
import os
import time
import threading
import sys

import requests
import websockets
import RPi.GPIO as GPIO

# ── GPIO Pins ────────────────────────────────────────────────────────────────
LED_PIN = 22  # Status LED
RECORD_SECONDS = 10
WAKE_LISTEN_SECONDS = 3
WAKE_COOLDOWN_SECONDS = 1.5
WAKE_CAMERA_DELAY_SECONDS = 10
SAMPLE_RATE = 16000
MIC_DEVICE = "plughw:1,0"
SPEAKER_DEVICE = "plughw:1,0"
WAKE_WORD = "cognilens"
WAKE_MIN_SIMILARITY = 0.72
WAKE_VARIANTS = (
    "cognilens",
    "congnilens",
    "cognilance",
    "cognalance",
    "ognilance",
    "ognilens",
    "congilens",
    "cogni lens",
)

# ── State ─────────────────────────────────────────────────────────────────────
is_recording = False
is_playing = False


def setup_gpio():
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(LED_PIN, GPIO.OUT, initial=GPIO.LOW)


def led(state: bool):
    try:
        GPIO.output(LED_PIN, GPIO.HIGH if state else GPIO.LOW)
    except Exception:
        pass


def record_audio_wav(duration: int = RECORD_SECONDS) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        path = f.name
    cmd = [
        "arecord",
        "-D",
        MIC_DEVICE,
        "-f",
        "S16_LE",
        "-r",
        str(SAMPLE_RATE),
        "-c",
        "1",
        "-d",
        str(duration),
        "-t",
        "wav",
        path,
    ]
    led(True)
    subprocess.run(cmd, check=True, capture_output=True)
    led(False)
    with open(path, "rb") as f:
        data = f.read()
    os.unlink(path)
    return data


def transcribe_audio_wav(server_url: str, audio_bytes: bytes) -> str:
    try:
        # Wake-word STT uses HTTP even when the main connection is WebSocket.
        rest_base_url = server_url.rstrip("/")
        if rest_base_url.startswith("ws://"):
            rest_base_url = "http://" + rest_base_url[len("ws://") :]
        elif rest_base_url.startswith("wss://"):
            rest_base_url = "https://" + rest_base_url[len("wss://") :]

        resp = requests.post(
            f"{rest_base_url}/audio/transcribe",
            files={"audio": ("wake.wav", audio_bytes, "audio/wav")},
            data={"language": "en"},
            timeout=20,
        )
        resp.raise_for_status()
        payload = resp.json()
        return str(payload.get("text", "")).strip()
    except Exception as exc:
        print(f"⚠️  Wake-word transcription failed: {exc}")
        return ""


def normalize_text(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9\s]", " ", text.lower())
    return " ".join(cleaned.split())


def _is_wake_token(token: str) -> bool:
    if not token:
        return False
    if token in WAKE_VARIANTS:
        return True
    return SequenceMatcher(None, token, WAKE_WORD).ratio() >= WAKE_MIN_SIMILARITY


def looks_like_wake_word(transcript: str) -> bool:
    normalized = normalize_text(transcript)
    if not normalized:
        return False

    if WAKE_WORD in normalized:
        return True

    if any(variant in normalized for variant in WAKE_VARIANTS):
        return True

    words = normalized.split()
    if any(_is_wake_token(word) for word in words):
        return True

    # Handle split transcription like "cogni lens".
    for i in range(len(words) - 1):
        merged = words[i] + words[i + 1]
        if _is_wake_token(merged):
            return True

    return False


def classify_wake_command(transcript: str) -> str:
    normalized = normalize_text(transcript)
    if not looks_like_wake_word(normalized):
        return ""

    if any(
        phrase in normalized
        for phrase in ("start camera", "camera start", "open camera", "camera on")
    ):
        return "camera"

    return "voice"


def capture_image_jpeg() -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        path = f.name
    cmd = [
        "rpicam-jpeg",
        "-o",
        path,
        "--width",
        "640",
        "--height",
        "480",
        "--nopreview",
        "-t",
        "200",
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    with open(path, "rb") as f:
        data = f.read()
    os.unlink(path)
    return data


def play_mp3_bytes(mp3_bytes: bytes):
    global is_playing
    is_playing = True
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        f.write(mp3_bytes)
        path = f.name
    subprocess.run(
        ["mpg123", "-q", "-a", SPEAKER_DEVICE, path], check=False, capture_output=True
    )
    os.unlink(path)
    is_playing = False


async def send_audio_after_wake(ws, wav: bytes):
    await ws.send(
        json.dumps(
            {
                "type": "audio_chunk",
                "data": base64.b64encode(wav).decode(),
            }
        )
    )


async def send_image_after_delay(ws, delay_seconds: int = WAKE_CAMERA_DELAY_SECONDS):
    global is_recording
    is_recording = True
    try:
        print(f"📷 Camera trigger detected. Capturing in {delay_seconds}s ...")
        await asyncio.sleep(delay_seconds)
        loop = asyncio.get_running_loop()
        img = await loop.run_in_executor(None, capture_image_jpeg)
        await ws.send(
            json.dumps(
                {
                    "type": "image",
                    "data": base64.b64encode(img).decode(),
                }
            )
        )
    finally:
        is_recording = False


async def run_client(server_url: str):
    """Main WebSocket client loop."""
    setup_gpio()
    print(f"🔗 Connecting to {server_url} ...")

    async with websockets.connect(
        f"{server_url}/ws/glasses?device_id=pi-glasses-01",
        ping_interval=20,
        ping_timeout=10,
    ) as ws:
        print("✅ Connected to CogniLens backend")

        # ── Listener task ─────────────────────────────────────────────────────
        async def listen():
            async for message in ws:
                try:
                    msg = json.loads(message)
                    mtype = msg.get("type", "")

                    if mtype == "audio_response":
                        transcript = msg.get("transcript", "")
                        response_text = msg.get("response_text", "")
                        audio_b64 = msg.get("audio_b64", "")

                        print(f"👤 You: {transcript}")
                        print(f"🤖 AI: {response_text}")

                        if audio_b64:
                            mp3 = base64.b64decode(audio_b64)
                            # Play in thread so we don't block async loop
                            threading.Thread(
                                target=play_mp3_bytes, args=(mp3,), daemon=True
                            ).start()

                    elif mtype == "pong":
                        pass  # Heartbeat OK

                    elif mtype == "command":
                        cmd = msg.get("data", "")
                        print(f"📲 Command from app: {cmd}")
                        if cmd == "capture_scene":
                            img = capture_image_jpeg()
                            await ws.send(
                                json.dumps(
                                    {
                                        "type": "image",
                                        "data": base64.b64encode(img).decode(),
                                    }
                                )
                            )
                except Exception as e:
                    print(f"⚠️  Message error: {e}")

        # ── Wake-word task ───────────────────────────────────────────────────
        async def poll_wake_word():
            global is_recording
            cooldown_until = 0.0
            loop = asyncio.get_running_loop()

            while True:
                if is_playing or is_recording:
                    await asyncio.sleep(0.1)
                    continue

                if time.time() < cooldown_until:
                    await asyncio.sleep(0.1)
                    continue

                print("👂 Listening for wake word ...")
                try:
                    is_recording = True
                    wake_audio = await loop.run_in_executor(
                        None, record_audio_wav, WAKE_LISTEN_SECONDS
                    )
                    is_recording = False

                    transcript = await loop.run_in_executor(
                        None, transcribe_audio_wav, server_url, wake_audio
                    )

                    command = classify_wake_command(transcript)
                    if command == "camera":
                        cooldown_until = time.time() + WAKE_COOLDOWN_SECONDS
                        await send_image_after_delay(ws)
                    elif command == "voice":
                        print(
                            f"✅ Wake word detected: {transcript or '[unintelligible]'}"
                        )
                        cooldown_until = time.time() + WAKE_COOLDOWN_SECONDS
                        is_recording = True
                        print("🎙  Recording voice ...")
                        wav = await loop.run_in_executor(
                            None, record_audio_wav, RECORD_SECONDS
                        )
                        is_recording = False
                        await send_audio_after_wake(ws, wav)
                    elif transcript:
                        print(f"… Heard: {transcript}")
                except Exception as e:
                    is_recording = False
                    print(f"❌ Wake-word error: {e}")
                    await asyncio.sleep(1)

        # Run the listener and wake-word detector concurrently
        await asyncio.gather(listen(), poll_wake_word())


def main():
    parser = argparse.ArgumentParser(description="CogniLens Pi Main Client")
    parser.add_argument(
        "--server",
        default="ws://localhost:8000",
        help="Backend WebSocket URL (ws://host:port)",
    )
    args = parser.parse_args()

    while True:  # Auto-reconnect loop
        try:
            asyncio.run(run_client(args.server))
        except (
            ConnectionRefusedError,
            websockets.exceptions.ConnectionClosed,
            OSError,
        ) as e:
            print(f"🔌 Disconnected ({e}). Reconnecting in 5s ...")
            time.sleep(5)
        except KeyboardInterrupt:
            print("\n👋 Stopping")
            GPIO.cleanup()
            break


if __name__ == "__main__":
    main()
