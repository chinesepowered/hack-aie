"""Agent-to-agent protocol between the Capy Kondo broker and the Capy Appraiser."""
from __future__ import annotations

from uagents import Protocol
from uagents_core.models import Model
from uagents_core.protocol import ProtocolSpecification


class AppraiseRequest(Model):
    session: str          # opaque id the broker uses to route the result back to the right chat
    media_url: str        # public URL of a photo or short video of the clutter
    seller: str           # seller's agent address (who will own the listings)


class AppraiseResult(Model):
    session: str
    ok: bool
    scan_id: str = ""
    count: int = 0
    total: float = 0.0
    seconds: float = 0.0
    error: str = ""


appraisal_spec = ProtocolSpecification(
    name="CapyAppraisalProtocol",
    version="0.1.0",
    interactions={AppraiseRequest: {AppraiseResult}, AppraiseResult: set()},
    roles={"appraiser": {AppraiseRequest}, "requester": {AppraiseResult}},
)


def make_appraisal_protocol(role: str) -> Protocol:
    """Same spec (and digest) for both agents; each side only handles the messages of its role."""
    return Protocol(spec=appraisal_spec, role=role)
