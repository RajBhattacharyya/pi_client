#!/usr/bin/env python3
"""
CogniLens Pi Audio Client.
Records from INMP441 mic via arecord, sends to backend, plays response via aplay.

Usage:
    python3 audio_client.py --server http://192.168.1.100:8000

Dependencies on Pi:
    pip3 install requests RPi.GPIO
    sudo apt install alsa-utils mpg123
"""
import argparse
import subprocess
import tempfile
import os
import sys
import time
import requests
import threading
import RPi.GPIO as GPIO

# ── Config ──────────────────────────────────────────────────────────────────
RECORD_DURATION_SECONDS = 5       # How long to record per interaction
SAMPLE_RATE = 16000
CHANNELS = 1
AUDIO_FORMAT = "S16_LE"           # 16-bit little-endian
CARD_INDEX = 0                    # ALSA card index (check with: arecord -l)
MIC_DEVICE = f"hw:{CARD_INDEX},0"
SPEAKER_DEVICE = f"hw:{CARD_INDEX},0"

# GPIO button pin (wire a physical button between GPIO17 and GND)
BUTTON_PIN = 17
LED_PIN = 27   # Optional status LED

SESSION_ID = ""   # Will be set after first response


def setup_gpio():
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(BUTTON_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(LED_PIN, GPIO.OUT, initial=GPIO.LOW)


def led(state: bool):
    try:
        GPIO.output(LED_PIN, GPIO.HIGH if state else GPIO.LOW)
    except Exception:
        pass


def record_audio(duration: int = RECORD_DURATION_SECONDS) -> bytes:
    """Record WAV audio from INMP441 mic via arecord."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp_path = f.name

    cmd = [
        "arecord",
        "-D", MIC_DEVICE,
        "-f", AUDIO_FORMAT,
        "-r", str(SAMPLE_RATE),
        "-c", str(CHANNELS),
        "-d", str(duration),
        "-t", "wav",
        tmp_path,
    ]
    print(f"🎙  Recording for {duration}s ...")
    led(True)
    subprocess.run(cmd, check=True, capture_output=True)
    led(False)

    with open(tmp_path, "rb") as f:
        data = f.read()
    os.unlink(tmp_path)
    return data


def play_audio_bytes(mp3_bytes: bytes):
    """Play MP3 audio bytes via PCM5102 using mpg123."""
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        f.write(mp3_bytes)
        tmp_path = f.name

    print("🔊 Playing response ...")
    led(True)
    subprocess.run(["mpg123", "-q", "-a", SPEAKER_DEVICE, tmp_path],
                   check=False, capture_output=True)
    led(False)
    os.unlink(tmp_path)


def send_and_play(server_url: str):
    """One full voice interaction cycle."""
    global SESSION_ID
    try:
        audio_data = record_audio()

        print("📤 Sending to backend ...")
        files = {"audio": ("input.wav", audio_data, "audio/wav")}
        data = {"session_id": SESSION_ID, "device_id": "pi-glasses-01"}

        resp = requests.post(
            f"{server_url}/audio/process",
            files=files,
            data=data,
            timeout=30,
        )
        resp.raise_for_status()

        # Extract metadata from headers
        transcript = resp.headers.get("X-Transcript", "")
        response_text = resp.headers.get("X-Response", "")
        SESSION_ID = resp.headers.get("X-Session-Id", SESSION_ID)

        print(f"👤 You said: {transcript}")
        print(f"🤖 AI: {response_text}")

        play_audio_bytes(resp.content)

    except requests.exceptions.ConnectionError:
        print("❌ Cannot reach backend. Check Wi-Fi and server URL.")
    except subprocess.CalledProcessError as e:
        print(f"❌ Audio error: {e}")
    except Exception as e:
        print(f"❌ Error: {e}")


def run_button_mode(server_url: str):
    """Press physical button to trigger voice interaction."""
    setup_gpio()
    print(f"🟢 Button mode: press GPIO{BUTTON_PIN} to speak")
    print(f"🌐 Backend: {server_url}")
    try:
        while True:
            # Wait for button press (active LOW)
            if GPIO.input(BUTTON_PIN) == GPIO.LOW:
                time.sleep(0.05)   # debounce
                if GPIO.input(BUTTON_PIN) == GPIO.LOW:
                    send_and_play(server_url)
                    time.sleep(0.5)   # prevent double-trigger
            time.sleep(0.02)
    except KeyboardInterrupt:
        print("\n👋 Stopping")
    finally:
        GPIO.cleanup()


def run_continuous_mode(server_url: str, interval: int = 2):
    """Continuously record and process every N seconds (no button needed)."""
    print(f"🔄 Continuous mode: recording every {interval}s")
    print(f"🌐 Backend: {server_url}")
    while True:
        send_and_play(server_url)
        time.sleep(interval)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CogniLens Audio Client")
    parser.add_argument("--server", default="http://localhost:8000",
                        help="Backend server URL")
    parser.add_argument("--mode", choices=["button", "continuous"],
                        default="button", help="Trigger mode")
    parser.add_argument("--interval", type=int, default=2,
                        help="Interval (seconds) in continuous mode")
    args = parser.parse_args()

    if args.mode == "button":
        run_button_mode(args.server)
    else:
        run_continuous_mode(args.server, args.interval)
