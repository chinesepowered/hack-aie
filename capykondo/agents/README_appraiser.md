# Capy Appraiser 🔎 - point a camera at your clutter, get priced listings

![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)
![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1)

Send a link to a photo or a short video of things you want to sell. Capy Appraiser:

1. finds every sellable item and boxes it (Meta **Muse Spark 1.3**),
2. segments each one with pixel-accurate masks in a single request (Meta **SAM 3.1**; video items are tracked across frames),
3. inspects condition and lists visible flaws (Google **Gemini 4 Argon**),
4. prices it from live web listings with citations (Muse Spark + web search),
5. builds a studio photo where the item stays **real pixels** and only the backdrop is painted (**Nano Banana 2.1**).

Listings are handed to **Capy Kondo**, which sells them on ASI:One.

Example: "Appraise this: https://example.com/my-desk.jpg"
