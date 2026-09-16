#!/usr/bin/env python3

"""
Receive Procare's push notifications by registering as one of its Android
devices.

Procare pushes through Firebase Cloud Messaging. Nothing here talks to
Procare itself: registration produces an FCM token, and whoever holds it
tells Procare about it - daycare_babybuddy_sync.py does, at login.

The values below were read out of the Procare APK (6.6.1). They identify
the app to Google, not us, so they are the same for everybody.

Procare's Firebase API key is not among them. It is no more secret than
the rest - it ships inside the app - but it is Procare's to publish, not
ours, so it is read from $PROCARE_FCM_API_KEY. The README says how to
recover it from the APK.
"""

import base64
import json
import os
import secrets
import time
from pathlib import Path

import requests
from firebase_messaging import FcmPushClient, FcmRegisterConfig

from daycare_babybuddy_sync import SCRIPT_DIR, log, warn

PROJECT_ID = "project-3013984628452988490"
APP_ID = "1:275509452379:android:ee2a45ab3c625743"
API_KEY = os.environ.get("PROCARE_FCM_API_KEY", "")
SENDER_ID = "275509452379"
PACKAGE = "com.kinderlime.dev"
APP_VERSION_CODE = "765"
APP_VERSION_NAME = "6.6.1"

# SHA-1 of the APK's signing certificate, taken from its v2 signing block.
CERT_SHA1 = "b362afdfb59da9ffc957d6a9da085cac5041e3db"

# A Play services build recent enough that the check-in looks ordinary.
GMS_VERSION = "243731016"
ANDROID_SDK = 34

CHECKIN_URL = "https://android.clients.google.com/checkin"
REGISTER_URL = "https://android.clients.google.com/c2dm/register3"
INSTALLATION_URL = (
    f"https://firebaseinstallations.googleapis.com/v1/projects/{PROJECT_ID}/installations"
)

# A registration is a device identity, so it is kept and reused: registering afresh every run would leave a trail of dead devices on Procare's side.
FCM_CACHE = Path(os.environ.get("PROCARE_FCM_CACHE") or SCRIPT_DIR / ".procare-fcm.json")

# Google needs a moment to finish provisioning a new android id; registering straight away comes back PHONE_REGISTRATION_ERROR.
PROVISION_DELAY = 5


# ---------------------------------------------------------------------------
# Protobuf, by hand
# ---------------------------------------------------------------------------

# The check-in has to describe an Android device. The protobuf schema that
# ships with the library we use describes a Chrome browser instead and has
# no build message in it, and the wire format is simple enough to write out.

def _varint(value):
    out = bytearray()

    while True:
        seven = value & 0x7F
        value >>= 7
        out.append(seven | (0x80 if value else 0))

        if not value:
            return bytes(out)


def _field(number, wire):
    return _varint((number << 3) | wire)


def _text(number, value):
    raw = value.encode()

    return _field(number, 2) + _varint(len(raw)) + raw


def _number(number, value):
    return _field(number, 0) + _varint(value)


def _nested(number, raw):
    return _field(number, 2) + _varint(len(raw)) + raw


def _checkin_request():
    build = (
        _text(1, "google/panther/panther:14/UP1A.231005.007/10754064:user/release-keys")
        + _text(2, "panther")
        + _text(3, "Google")
        + _text(4, "g5300q")
        + _text(5, "unknown")
        + _text(6, "android-google")
        + _number(7, int(time.time()))
        + _number(8, int(GMS_VERSION))
        + _text(9, "panther")
        + _number(10, ANDROID_SDK)
        + _text(11, "Pixel 7")
        + _text(12, "Google")
        + _text(13, "panther")
        + _number(14, 0)
    )

    checkin = _nested(1, build) + _number(2, 0) + _number(12, 1)  # type 1: Android

    return (
        _number(2, 0)  # no android id yet
        + _nested(4, checkin)
        + _text(6, "en_US")
        + _number(7, secrets.randbits(63))
        + _text(12, "America/New_York")
        + _number(14, 3)
        + _number(20, 0)
        + _number(22, 0)
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def _checkin():
    from firebase_messaging.proto.checkin_pb2 import AndroidCheckinResponse

    response = requests.post(
        CHECKIN_URL,
        headers={"Content-Type": "application/x-protobuf"},
        data=_checkin_request(),
        timeout=30,
    )
    response.raise_for_status()

    parsed = AndroidCheckinResponse()
    parsed.ParseFromString(response.content)

    return str(parsed.android_id), str(parsed.security_token)


def _install():
    """
    A Firebase installation, which is what authorises the registration.
    """

    if not API_KEY:
        raise RuntimeError(
            "Set $PROCARE_FCM_API_KEY to the google_api_key string in the "
            "Procare APK; see the README. Registering with Firebase needs it."
        )

    fid = bytearray(secrets.token_bytes(17))
    fid[0] = 0b01110000 + (fid[0] % 0b00010000)  # the 4-bit FID header

    response = requests.post(
        INSTALLATION_URL,
        headers={"x-goog-api-key": API_KEY, "Content-Type": "application/json"},
        json={
            "appId": APP_ID,
            "authVersion": "FIS_v2",
            "fid": base64.b64encode(fid).decode(),
            "sdkVersion": "a:17.2.0",
        },
        timeout=30,
    )
    response.raise_for_status()
    body = response.json()

    return body["fid"], body["authToken"]["token"]


def _register(android_id, security_token, fid, installation_token):
    response = requests.post(
        REGISTER_URL,
        headers={
            "Authorization": f"AidLogin {android_id}:{security_token}",
            "app": PACKAGE,
            "gcm_ver": GMS_VERSION,
            "User-Agent": "Android-GCM/1.5",
        },
        data={
            "device": android_id,
            "app": PACKAGE,
            "cert": CERT_SHA1,
            "app_ver": APP_VERSION_CODE,
            "X-app_ver": APP_VERSION_CODE,
            "X-app_ver_name": APP_VERSION_NAME,
            "X-osv": str(ANDROID_SDK),
            "X-cliv": "fiid-21.1.1",
            "X-gmsv": GMS_VERSION,
            "X-appid": fid,
            "X-scope": "*",
            "X-Goog-Firebase-Installations-Auth": installation_token,
            "X-gmp_app_id": APP_ID,
            "X-subtype": SENDER_ID,
            "target_ver": str(ANDROID_SDK),
            "sender": SENDER_ID,
        },
        timeout=30,
    )
    response.raise_for_status()

    if "token=" not in response.text:
        raise RuntimeError(f"Google refused the registration: {response.text.strip()}")

    return response.text.split("token=", 1)[1].strip()


def register():
    """
    Register a new device with Google and return its credentials.
    """

    log("Registering a device with Firebase...")

    android_id, security_token = _checkin()

    time.sleep(PROVISION_DELAY)

    fid, installation_token = _install()
    token = _register(android_id, security_token, fid, installation_token)

    log(f"Registered as android id {android_id}.")

    return {
        "android_id": android_id,
        "security_token": security_token,
        "fid": fid,
        "token": token,
    }


def credentials(refresh=False):
    """
    The cached device credentials, registering on the first run.
    """

    if not refresh and FCM_CACHE.exists():
        with open(FCM_CACHE) as handle:
            return json.load(handle)

    device = register()

    FCM_CACHE.write_text(json.dumps(device, indent=2))

    try:
        FCM_CACHE.chmod(0o600)
    except OSError:
        pass  # Windows and some bind mounts: the cache works without it.

    return device


# ---------------------------------------------------------------------------
# Listening
# ---------------------------------------------------------------------------

# Two listeners sharing one registration take turns evicting each other
# from the Firebase connection. It looks like nothing worse than a busy log
# until you notice every notification arriving twice, so say it plainly.
STORM_LOGINS = 5
STORM_SECONDS = 120


class _AndroidPushClient(FcmPushClient):
    """
    The library registers browsers, whose payloads arrive encrypted. An
    Android app's data comes in the clear, so unpack it instead.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._logins = []
        self._warned_about_storm = False

    async def _login(self):
        await super()._login()

        now = time.monotonic()
        self._logins = [at for at in self._logins if now - at < STORM_SECONDS] + [now]

        if len(self._logins) >= STORM_LOGINS and not self._warned_about_storm:
            self._warned_about_storm = True
            warn(
                f"reconnected {len(self._logins)} times in under "
                f"{STORM_SECONDS}s - is something else listening with this "
                f"same registration ({FCM_CACHE})?"
            )

    def _handle_data_message(self, message):
        data = {item.key: item.value for item in message.app_data}

        if data.get("message_type") == "deleted_messages":
            return

        self.callback(data, message.persistent_id, self.callback_context)


def client(device, on_message):
    """
    A push client ready to start(), delivering messages to on_message.
    """

    config = FcmRegisterConfig(
        project_id=PROJECT_ID,
        app_id=APP_ID,
        api_key=API_KEY,
        messaging_sender_id=SENDER_ID,
        bundle_id=PACKAGE,
    )

    return _AndroidPushClient(
        on_message,
        config,
        {
            "gcm": {
                "android_id": device["android_id"],
                "security_token": device["security_token"],
                "app_id": PACKAGE,
            },
            "fcm": {"registration": {"token": device["token"]}},
        },
    )


if __name__ == "__main__":
    import sys

    device = credentials(refresh="--refresh" in sys.argv)

    log(f"android id: {device['android_id']}")
    log(f"FCM token:  {device['token'][:24]}... ({len(device['token'])} chars)")
    log(f"cached in:  {FCM_CACHE}")
