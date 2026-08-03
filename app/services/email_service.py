"""
Email delivery for OTP codes and password-reset notices.

By default this just logs the email to the console, so the API works out of
the box with no external account needed for local dev / demos. Set the
SMTP_* env vars to switch to real email delivery via SMTP (works with
Gmail, SES SMTP, SendGrid SMTP, Mailgun SMTP, etc.).
"""

import logging
import os
import smtplib
from email.message import EmailMessage

logger = logging.getLogger("email_service")
logging.basicConfig(level=logging.INFO)

SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
SMTP_FROM_EMAIL = os.getenv("SMTP_FROM_EMAIL", "no-reply@example.com")


def send_otp_email(to_email: str, otp: str) -> None:
    subject = "Your verification code"
    body = f"Your verification code is {otp}. It expires in a few minutes."
    _send(to_email, subject, body)


def send_password_reset_email(to_email: str, otp: str) -> None:
    subject = "Reset your password"
    body = (
        f"Your password reset code is {otp}. It expires in a few minutes. "
        "If you didn't request this, you can safely ignore this email."
    )
    _send(to_email, subject, body)


def _send(to_email: str, subject: str, body: str) -> None:
    if SMTP_HOST and SMTP_USERNAME and SMTP_PASSWORD:
        _send_via_smtp(to_email, subject, body)
    else:
        # Console/log backend — good enough for local dev and demos.
        logger.info("[Email to %s] %s: %s", to_email, subject, body)


def _send_via_smtp(to_email: str, subject: str, body: str) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = SMTP_FROM_EMAIL
    message["To"] = to_email
    message.set_content(body)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.send_message(message)
