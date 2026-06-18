"""Send email via SMTP (e.g. a professional Gmail account).

Gmail note: use an App Password (Google Account > Security > 2-Step Verification >
App passwords), not your normal password. Host smtp.gmail.com, port 587, STARTTLS.
"""
import ssl
import smtplib
from email.message import EmailMessage


def smtp_configured(cfg) -> bool:
    return bool(cfg.get("smtp_enabled") == "1"
                and cfg.get("smtp_host")
                and cfg.get("smtp_from"))


def send_email(cfg, to_addr, subject, body):
    """Send a plain-text email. Returns (ok: bool, detail: str)."""
    to_addr = (to_addr or "").strip()
    if not to_addr:
        return False, "No email address on file for this customer."
    if not smtp_configured(cfg):
        return False, "Email isn't set up yet. Add your SMTP details in Settings."

    host = cfg["smtp_host"].strip()
    try:
        port = int(cfg.get("smtp_port") or 587)
    except ValueError:
        port = 587
    security = (cfg.get("smtp_security") or "starttls").lower()
    user = (cfg.get("smtp_user") or "").strip()
    password = cfg.get("smtp_pass") or ""

    msg = EmailMessage()
    msg["From"] = cfg["smtp_from"].strip()
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        context = ssl.create_default_context()
        if security == "ssl":
            server = smtplib.SMTP_SSL(host, port, timeout=20, context=context)
        else:
            server = smtplib.SMTP(host, port, timeout=20)
            if security == "starttls":
                server.starttls(context=context)
        with server:
            if user and password:
                server.login(user, password)
            server.send_message(msg)
        return True, "Sent"
    except smtplib.SMTPAuthenticationError:
        return False, ("Login was rejected. For Gmail, use a 16-character App "
                       "Password, not your normal password.")
    except Exception as e:
        return False, str(e)
