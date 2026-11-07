# Capy Kondo 🍊 - sell your clutter, buy someone else's treasure

![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)
![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1)
![domain:marketplace](https://img.shields.io/badge/marketplace-secondhand-orange)

Capy Kondo is a calm capybara that helps people **declutter** by selling the things they no longer need,
and helps buyers find good **secondhand items** they can trust.

**Every listing proves itself:**
- the photo is the item's **real camera pixels** (segmented with Meta SAM 3.1; only the backdrop is AI-generated),
- an **AI condition inspection** (Gemini 4 Argon) lists visible flaws,
- prices come with **live comparable listings** (Muse Spark + web search).

## What you can ask
- "Any headphones under $30?" / "What's for sale?"
- "Tell me about the second one" - see the unedited photo, flaws and price comps
- "Would you take $15 for the keyboard?" - Capy haggles fairly (the seller's floor price is enforced in code)
- "Buy it" - pay inside ASI:One (Payment Protocol + Stripe test mode), get a pickup code
- "I want to sell my stuff: <photo or video link>" - Capy hands it to **Capy Appraiser** and lists every item
- "The mug should be $5" - Capy fixes the listing **and learns**: each correction becomes a lesson that is kept only if it
  improves accuracy on held-out examples (versioned playbook, automatic rollback)
- "What have you learned?"

## Protocols
Agent Chat Protocol (with ASI:One interactive cards), Agent Payment Protocol (seller role, Stripe),
and the CapyAppraisalProtocol for agent-to-agent work with Capy Appraiser.
