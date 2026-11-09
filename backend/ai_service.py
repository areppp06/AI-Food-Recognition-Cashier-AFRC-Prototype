import os
import json
import base64
from typing import List
from openai import OpenAI
from .schemas import AIItem
import httpx

AI_PROVIDER = os.getenv("AI_PROVIDER", "mock")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

SYSTEM_PROMPT = """You are a food recognition AI for a Malaysian nasi campur / self-service stall.
You will receive today's available menu and a photo of a customer's plate.
Identify ONLY dishes from the provided menu. Estimate quantity for each.
Return STRICT JSON in this format:
{
  "items": [
    {"name": "exact menu name", "quantity": 1.0, "confidence": 0.95}
  ]
}
If unsure, lower the confidence. Do not invent dishes or prices. Prices are handled by backend.
"""

def get_available_menu_text(menu: List[dict]) -> str:
    return "\n".join([f"- {d['name']} ({d['unit']})" for d in menu if d['available']])

async def recognize_food(image_bytes: bytes, available_menu: List[dict]) -> List[AIItem]:
    menu_text = get_available_menu_text(available_menu)
    prompt = f"{SYSTEM_PROMPT}\n\nToday's available menu:\n{menu_text}"

    if AI_PROVIDER == "mock" or not OPENAI_API_KEY:
        # Demo mock - returns plausible items based on common nasi campur
        return [
            AIItem(name="Rice", quantity=1, confidence=0.97),
            AIItem(name="Chicken", quantity=1, confidence=0.92),
            AIItem(name="Vegetables", quantity=1, confidence=0.88),
            AIItem(name="Ikan bawal (per piece)", quantity=1, confidence=0.75),
        ]

    if AI_PROVIDER == "openai" and client:
        b64 = base64.b64encode(image_bytes).decode()
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Recognize the food on this plate."},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
                    ]
                }
            ],
            response_format={"type": "json_object"},
            max_tokens=500
        )
        data = json.loads(response.choices[0].message.content)
        return [AIItem(**item) for item in data.get("items", [])]

    # Gemini fallback stub (implement with google-generativeai if needed)
    return [AIItem(name="Rice", quantity=1, confidence=0.9)]
