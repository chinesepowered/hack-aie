"""Payments: Stripe Checkout in test mode when keys are configured, otherwise a clearly-labelled demo mode."""
from __future__ import annotations

import logging
import time

from . import config

log = logging.getLogger("capy.payments")

try:
    import stripe
except ImportError:  # pragma: no cover
    stripe = None


def enabled() -> bool:
    return bool(stripe and config.STRIPE_SECRET_KEY.startswith("sk_test_"))


def mode() -> str:
    return "stripe-test" if enabled() else "demo"


def _client():
    stripe.api_key = config.STRIPE_SECRET_KEY
    return stripe


def create_checkout(order_id: str, title: str, amount: float, buyer: str, image_url: str | None = None) -> dict:
    """Create a Stripe Checkout Session usable both embedded (ASI:One/Agentverse UI) and as a hosted link."""
    if not enabled():
        return {"mode": "demo", "order_id": order_id}
    s = _client()
    cents = int(round(amount * 100))
    base = config.PUBLIC_BASE_URL or f"http://localhost:{config.PORT}"
    product = {"name": title[:120]}
    if image_url:
        product["images"] = [image_url]
    common = dict(
        mode="payment", payment_method_types=["card"],
        line_items=[{"price_data": {"currency": "usd", "unit_amount": cents, "product_data": product},
                     "quantity": 1}],
        metadata={"order_id": order_id, "buyer": buyer[:200], "service": "capy-kondo"},
        expires_at=int(time.time()) + 1800,
    )
    hosted = s.checkout.Session.create(success_url=f"{base}/pay/done?order={order_id}",
                                       cancel_url=f"{base}/pay/cancel?order={order_id}", **common)
    embedded = None
    try:
        embedded = s.checkout.Session.create(ui_mode="embedded", redirect_on_completion="if_required",
                                             return_url=f"{base}/pay/done?order={order_id}", **common)
    except Exception as e:  # noqa: BLE001
        log.warning("embedded checkout unavailable: %s", e)
    out = {"mode": "stripe-test", "order_id": order_id, "url": hosted.url, "hosted_session_id": hosted.id,
           "currency": "usd", "amount_cents": cents}
    if embedded is not None:
        out["embedded"] = {"ui_mode": "embedded", "publishable_key": config.STRIPE_PUBLISHABLE_KEY,
                           "client_secret": embedded.client_secret, "checkout_session_id": embedded.id,
                           "currency": "usd", "amount_cents": cents}
    return out


def is_paid(session_id: str) -> bool:
    if not enabled() or not session_id:
        return False
    try:
        sess = _client().checkout.Session.retrieve(session_id)
        return getattr(sess, "payment_status", None) == "paid"
    except Exception as e:  # noqa: BLE001
        log.warning("stripe verify failed: %s", e)
        return False
