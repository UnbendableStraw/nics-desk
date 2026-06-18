"""Send iMessages through the macOS Messages app.

On macOS this shells out to `osascript` running send_imessage.applescript.
On any other OS (or in dev), it logs instead of sending so the app still runs.
"""
import os
import sys
import shutil
import subprocess

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "send_imessage.applescript")


def imessage_available() -> bool:
    """True only on macOS with osascript present."""
    return sys.platform == "darwin" and shutil.which("osascript") is not None


def send_imessage(phone: str, message: str):
    """Send an iMessage. Returns (ok: bool, detail: str)."""
    phone = (phone or "").strip()
    if not phone:
        return False, "No phone number on file for this customer."

    if not imessage_available():
        # Dev mode: don't fail the whole app, just report clearly.
        print(f"[iMessage DEV MODE] would send to {phone}: {message}")
        return False, ("iMessage only sends on a Mac with Messages signed in. "
                       "Running off-Mac, so this was logged but not sent.")

    try:
        result = subprocess.run(
            ["osascript", SCRIPT, phone, message],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return False, "Messages did not respond in time."
    except Exception as e:  # pragma: no cover
        return False, f"Could not run osascript: {e}"

    if result.returncode == 0:
        return True, "Sent"

    err = (result.stderr or "").strip()
    if "Not authorized" in err or "-1743" in err:
        err = ("macOS blocked automation. Grant your terminal/Python access under "
               "System Settings > Privacy & Security > Automation, then try again.")
    return False, err or "Messages reported an error."
