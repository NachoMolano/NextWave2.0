"""Send one explicit Resend smoke-test email without entering Volta's normal flow.

Run from ``backend/`` after filling ``.env``::

    uv run python -m scripts.send_test_email

Optional flags allow an arbitrary recipient, subject, and HTML body. The API key is read from
``.env`` and is never printed.
"""

import argparse
import asyncio

import httpx

from app.config import Settings


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send one standalone Resend test email.")
    parser.add_argument("--to", dest="to_address", help="Recipient; defaults to MANAGER_EMAIL.")
    parser.add_argument("--subject", default="Hello World")
    parser.add_argument(
        "--html",
        default="<p>Congrats on sending your <strong>first email</strong>!</p>",
    )
    return parser.parse_args()


async def send_test_email(
    *, to_address: str | None = None, subject: str = "Hello World", html: str
) -> str:
    settings = Settings()
    api_key = settings.resend_api_key.strip()
    sender = settings.notify_from_email.strip()
    recipient = (to_address or settings.manager_email).strip()

    if not api_key or api_key == "re_xxxxxxxxx":
        raise ValueError("replace re_xxxxxxxxx in backend/.env with your real Resend API key")
    if not sender:
        raise ValueError("set NOTIFY_FROM_EMAIL in backend/.env")
    if not recipient:
        raise ValueError("set MANAGER_EMAIL or pass --to")

    payload = {
        "from": f"{settings.notify_from_name} <{sender}>",
        "to": [recipient],
        "subject": subject,
        "html": html,
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
        )
    response.raise_for_status()
    message_id = response.json().get("id")
    if not isinstance(message_id, str) or not message_id:
        raise RuntimeError("Resend accepted the request but returned no message id")
    return message_id


async def _main() -> None:
    args = _arguments()
    message_id = await send_test_email(
        to_address=args.to_address,
        subject=args.subject,
        html=args.html,
    )
    print(f"Resend accepted the test email: {message_id}")


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except (ValueError, RuntimeError, httpx.HTTPError) as exc:
        raise SystemExit(f"Email not sent: {exc}") from None
