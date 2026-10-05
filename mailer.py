"""
Email for the tenant websites: tell the tenant about a new enquiry or booking
request, and send the customer an acknowledgement. Uses the MAIL_* settings
(config.py, set in Coolify); with none set, nothing is sent and everything
still lands in the Website Inbox. Sending runs in a background thread so a
slow mail server never holds up the visitor's page.
"""
import smtplib
import threading
from email.message import EmailMessage


def configured():
    from config import Config
    return bool(Config.MAIL_SERVER and Config.MAIL_FROM)


def _send(msgs):
    from config import Config
    try:
        cls = smtplib.SMTP_SSL if Config.MAIL_USE_SSL else smtplib.SMTP
        with cls(Config.MAIL_SERVER, Config.MAIL_PORT, timeout=20) as s:
            if not Config.MAIL_USE_SSL:
                try:
                    s.starttls()
                except smtplib.SMTPException:
                    pass
            if Config.MAIL_USERNAME:
                s.login(Config.MAIL_USERNAME, Config.MAIL_PASSWORD or "")
            for m in msgs:
                s.send_message(m)
    except Exception as e:  # never break the page; the inbox has it anyway
        print(f"[mailer] not sent: {e.__class__.__name__}: {e}")


def send(messages):
    """messages: [(to, subject, text, reply_to)]."""
    if not configured():
        return False
    from config import Config
    msgs = []
    for to, subject, text, reply_to in messages:
        if not to:
            continue
        m = EmailMessage()
        m["From"], m["To"], m["Subject"] = Config.MAIL_FROM, to, subject
        if reply_to:
            m["Reply-To"] = reply_to
        m.set_content(text)
        msgs.append(m)
    if msgs:
        threading.Thread(target=_send, args=(msgs,), daemon=True).start()
    return True
