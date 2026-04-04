#!/usr/bin/env python3
"""
CogniLens Pi Camera Client.
Captures image from Pi Camera Module 3 and sends to backend for vision analysis.

Dependencies on Pi:
    pip3 install requests picamera2
    sudo apt install libcamera-apps tesseract-ocr
"""
import argparse
import subprocess
import tempfile
import os
import io
import requests
from PIL import Image


BACKEND_URL = "http://192.168.29.190:8000"
DEVICE_ID = "pi-glasses-01"
DEFAULT_ROTATION = 90


def capture_image_libcamera(rotation: int = DEFAULT_ROTATION) -> bytes:
    """Capture JPEG image using libcamera (Pi Camera Module 3)."""
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        tmp_path = f.name

    capture_rotation = rotation if rotation in (0, 180) else 0
    cmd = [
        "rpicam-jpeg",
        "-o",
        tmp_path,
        "--width",
        "640",
        "--height",
        "480",
        "--nopreview",
        "-t",
        "100",  # 100ms warmup
        "--rotation",
        str(capture_rotation),
    ]
    subprocess.run(cmd, check=True, capture_output=True)

    with open(tmp_path, "rb") as f:
        data = f.read()
    os.unlink(tmp_path)

    # 90/270 degree transforms require transpose and may not be supported by libcamera.
    if rotation in (90, 270):
        data = _rotate_jpeg_bytes(data, rotation)

    return data


def _rotate_jpeg_bytes(image_bytes: bytes, rotation: int) -> bytes:
    """Rotate JPEG in software for camera stacks that do not support transpose."""
    with Image.open(io.BytesIO(image_bytes)) as img:
        rotated = img.rotate(-rotation, expand=True)
        out = io.BytesIO()
        rotated.save(out, format="JPEG", quality=95)
        return out.getvalue()


def analyze_scene(
    server_url: str = BACKEND_URL, rotation: int = DEFAULT_ROTATION
) -> dict:
    """Capture image and send to /vision/analyze. Returns spoken response."""
    print("📸 Capturing image ...")
    img_bytes = capture_image_libcamera(rotation=rotation)

    print("📤 Sending to backend for analysis ...")
    files = {"image": ("frame.jpg", img_bytes, "image/jpeg")}
    data = {"device_id": DEVICE_ID, "speak": "true"}

    resp = requests.post(
        f"{server_url}/vision/analyze", files=files, data=data, timeout=30
    )
    resp.raise_for_status()

    description = resp.headers.get("X-Description", "")
    faces = resp.headers.get("X-Faces", "")
    print(f"👁  Scene: {description}")
    print(f"👥 Faces: {faces}")

    # Play audio response
    if resp.content:
        _play_mp3(resp.content)

    return {"description": description, "faces": faces}


def read_text(server_url: str = BACKEND_URL, rotation: int = DEFAULT_ROTATION):
    """Capture image and read any visible text aloud."""
    print("📸 Capturing for OCR ...")
    img_bytes = capture_image_libcamera(rotation=rotation)

    files = {"image": ("frame.jpg", img_bytes, "image/jpeg")}
    resp = requests.post(
        f"{server_url}/vision/ocr", files=files, data={"speak": "true"}, timeout=20
    )
    resp.raise_for_status()

    text = resp.headers.get("X-OCR-Text", "")
    print(f"📝 OCR: {text}")
    if resp.content:
        _play_mp3(resp.content)


def _play_mp3(mp3_bytes: bytes):
    import tempfile, subprocess

    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        f.write(mp3_bytes)
        path = f.name
    subprocess.run(["mpg123", "-q", path], check=False, capture_output=True)
    os.unlink(path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default=BACKEND_URL)
    parser.add_argument("--action", choices=["scene", "ocr"], default="scene")
    parser.add_argument(
        "--rotation", type=int, choices=[0, 90, 180, 270], default=DEFAULT_ROTATION
    )
    args = parser.parse_args()

    if args.action == "scene":
        analyze_scene(args.server, rotation=args.rotation)
    elif args.action == "ocr":
        read_text(args.server, rotation=args.rotation)
