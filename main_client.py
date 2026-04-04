#!/usr/bin/env python3
"""
CogniLens Pi Main Client — Unified WebSocket connection.
Handles audio, camera, gyro, and button in a single persistent session.

Dependencies on Pi:
    pip3 install websockets requests RPi.GPIO smbus2 picamera2
    sudo apt install alsa-utils mpg123 libcamera-apps

Usage:
    python3 main_client.py --server ws://192.168.1.100:8000
"""
import asyncio
import argparse
import base64
import json
import subprocess
import tempfile
import os
import time
import threading
import sys

import websockets
import RPi.GPIO as GPIO

# ── GPIO Pins ────────────────────────────────────────────────────────────────
AUDIO_BUTTON_PIN  = 17   # Press → record voice query
CAMERA_BUTTON_PIN = 27   # Press → capture image for scene analysis
LED_PIN           = 22   # Status LED
RECORD_SECONDS    = 5
SAMPLE_RATE       = 16000
MIC_DEVICE        = "hw:0,0"
SPEAKER_DEVICE    = "hw:0,0"

# ── State ─────────────────────────────────────────────────────────────────────
is_recording = False
is_playing   = False


def setup_gpio():
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(AUDIO_BUTTON_PIN,  GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(CAMERA_BUTTON_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(LED_PIN, GPIO.OUT, initial=GPIO.LOW)


def led(state: bool):
    try:
        GPIO.output(LED_PIN, GPIO.HIGH if state else GPIO.LOW)
    except Exception:
        pass


def record_audio_wav() -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        path = f.name
    cmd = ["arecord", "-D", MIC_DEVICE, "-f", "S16_LE",
           "-r", str(SAMPLE_RATE), "-c", "1",
           "-d", str(RECORD_SECONDS), "-t", "wav", path]
    led(True)
    subprocess.run(cmd, check=True, capture_output=True)
    led(False)
    with open(path, "rb") as f:
        data = f.read()
    os.unlink(path)
    return data


def capture_image_jpeg() -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        path = f.name
    cmd = ["libcamera-jpeg", "-o", path, "--width", "640",
           "--height", "480", "--nopreview", "-t", "200"]
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
    subprocess.run(["mpg123", "-q", "-a", SPEAKER_DEVICE, path],
                   check=False, capture_output=True)
    os.unlink(path)
    is_playing = False


async def run_client(server_url: str):
    """Main WebSocket client loop."""
    setup_gpio()
    print(f"🔗 Connecting to {server_url} ...")

    async with websockets.connect(f"{server_url}/ws/glasses?device_id=pi-glasses-01",
                                  ping_interval=20, ping_timeout=10) as ws:
        print("✅ Connected to CogniLens backend")

        # ── Listener task ─────────────────────────────────────────────────────
        async def listen():
            async for message in ws:
                try:
                    msg = json.loads(message)
                    mtype = msg.get("type", "")

                    if mtype == "audio_response":
                        transcript    = msg.get("transcript", "")
                        response_text = msg.get("response_text", "")
                        audio_b64     = msg.get("audio_b64", "")

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
                            await ws.send(json.dumps({
                                "type": "image",
                                "data": base64.b64encode(img).decode(),
                            }))
                except Exception as e:
                    print(f"⚠️  Message error: {e}")

        # ── Button polling task ───────────────────────────────────────────────
        async def poll_buttons():
            audio_pressed  = False
            camera_pressed = False

            while True:
                # Audio button
                if GPIO.input(AUDIO_BUTTON_PIN) == GPIO.LOW:
                    if not audio_pressed and not is_recording and not is_playing:
                        audio_pressed = True
                        print("🎙  Recording voice ...")
                        try:
                            wav = await asyncio.get_event_loop().run_in_executor(
                                None, record_audio_wav)
                            await ws.send(json.dumps({
                                "type": "audio_chunk",
                                "data": base64.b64encode(wav).decode(),
                            }))
                        except Exception as e:
                            print(f"❌ Audio error: {e}")
                else:
                    audio_pressed = False

                # Camera button
                if GPIO.input(CAMERA_BUTTON_PIN) == GPIO.LOW:
                    if not camera_pressed:
                        camera_pressed = True
                        print("📸 Capturing scene ...")
                        try:
                            img = await asyncio.get_event_loop().run_in_executor(
                                None, capture_image_jpeg)
                            await ws.send(json.dumps({
                                "type": "image",
                                "data": base64.b64encode(img).decode(),
                            }))
                        except Exception as e:
                            print(f"❌ Camera error: {e}")
                else:
                    camera_pressed = False

                await asyncio.sleep(0.05)

        # Run both tasks concurrently
        await asyncio.gather(listen(), poll_buttons())


def main():
    parser = argparse.ArgumentParser(description="CogniLens Pi Main Client")
    parser.add_argument("--server", default="ws://localhost:8000",
                        help="Backend WebSocket URL (ws://host:port)")
    args = parser.parse_args()

    while True:   # Auto-reconnect loop
        try:
            asyncio.run(run_client(args.server))
        except (ConnectionRefusedError, websockets.exceptions.ConnectionClosed,
                OSError) as e:
            print(f"🔌 Disconnected ({e}). Reconnecting in 5s ...")
            time.sleep(5)
        except KeyboardInterrupt:
            print("\n👋 Stopping")
            GPIO.cleanup()
            break


if __name__ == "__main__":
    main()
