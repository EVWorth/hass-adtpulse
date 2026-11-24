"""Make the browser fingerprint ADT Pulse identifies a device by."""

from __future__ import annotations

import json
import secrets
from base64 import b64encode


def generate_fingerprint() -> str:
    """Return a new browser fingerprint for this installation.

    Pulse's sign-in page sends btoa(JSON.stringify(fingerprint)), and a device
    is trusted by that value. Pulse logs in with a random string but refuses
    to trust it (HTTP 409), so this mimics a desktop Chrome and makes it unique
    with random canvas and WebGL hashes. The format was checked against a real
    account by @jra3 in EVWorth/hass-adtpulse#17.
    """
    fingerprint = {
        "uaBrowser": {"name": "Chrome", "version": "120.0.0.0", "major": "120"},
        "uaString": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "uaDevice": {"model": None, "type": None, "vendor": None},
        "uaEngine": {"name": "Blink", "version": "120.0.0.0"},
        "uaOS": {"name": "Linux", "version": "x86_64"},
        "uaCPU": {"architecture": "amd64"},
        "uaPlatform": "Linux x86_64",
        "language": "en-US",
        "colorDepth": 24,
        "pixelRatio": 1,
        "screenResolution": "1920x1080",
        "availableScreenResolution": "1920x1080",
        "timezone": "UTC",
        "timezoneOffset": 0,
        "localStorage": True,
        "sessionStorage": True,
        "indexedDb": True,
        "addBehavior": False,
        "openDatabase": False,
        "cpuClass": None,
        "platform": "Linux x86_64",
        "doNotTrack": None,
        "plugins": "PDF Viewer::Portable Document Format::application/pdf~pdf",
        "canvas": str(secrets.randbelow(2**31)),
        "webGl": str(secrets.randbelow(2**31)),
        "adBlock": False,
        "userTamperLanguage": False,
        "userTamperScreenResolution": False,
        "userTamperOS": False,
        "userTamperBrowser": False,
        "touchSupport": {
            "maxTouchPoints": 0,
            "touchEvent": False,
            "touchStart": False,
        },
        "cookieSupport": True,
        "fonts": "Arial;Courier New;Times New Roman",
    }
    data = json.dumps({"fingerprint": fingerprint}, separators=(",", ":"))
    return b64encode(data.encode()).decode()
