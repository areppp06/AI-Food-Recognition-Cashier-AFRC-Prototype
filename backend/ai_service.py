import os
import json
import base64
from typing import List, Union
from openai import OpenAI
from google import genai
from google.genai import types
from .schemas import AIItem, AIResponse
import httpx

AI_PROVIDER = os.getenv("AI_PROVIDER", "gemini")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

SYSTEM_PROMPT = """You are a food recognition AI for a Malaysian nasi campur / self-service stall.
You will receive today's available menu and a photo of a customer's plate.

Rules:
1. Identify ONLY dishes from the provided menu. Estimate quantity for each.
2. If the photo is blurry, too dark, not a plate of food, random object, or unreadable — return an error.
3. Return STRICT JSON only.

Success format:
{
  "items": [
    {"name": "exact menu name", "quantity": 1.0, "confidence": 0.95}
  ]
}

Error format (use when photo is bad):
{
  "error_code": "UNREADABLE_PHOTO",
  "error_message": "Photo is too blurry or does not clearly show food on a plate. Please retake in better lighting."
}

Possible error_codes: UNREADABLE_PHOTO, NO_FOOD_DETECTED, NOT_A_PLATE
Do not invent dishes or prices. Prices are handled by backend.
"""

def get_available_menu_text(menu: List[dict]) -> str:
    return "\n".join([f"- {d['name']} ({d['unit']})" for d in menu if d['available']])

class RecognitionError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)

async def recognize_food(image_bytes: bytes, available_menu: List[dict]) -> List[AIItem]:
    menu_text = get_available_menu_text(available_menu)
    prompt = f"{SYSTEM_PROMPT}\n\nToday's available menu:\n{menu_text}"

    if not available_menu:
        raise RecognitionError("NO_MENU", "No dishes available today. Please ask staff.")

    if AI_PROVIDER == "mock" or (not OPENAI_API_KEY and not GEMINI_API_KEY):
        # Demo mock
        return [
            AIItem(name="Rice", quantity=1, confidence=0.97),
            AIItem(name="Chicken", quantity=1, confidence=0.92),
            AIItem(name="Vegetables", quantity=1, confidence=0.88),
        ]

    try:
        if AI_PROVIDER == "gemini" and gemini_client:
            response = gemini_client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[
                    types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                    prompt
                ],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=AIResponse
                )
            )
            data = json.loads(response.text)
            if data.get("error_code"):
                raise RecognitionError(data["error_code"], data.get("error_message", "Unable to recognize food."))
            return [AIItem(**item) for item in data.get("items", [])]

        if AI_PROVIDER == "openai" and openai_client:
            b64 = base64.b64encode(image_bytes).decode()
            response = openai_client.chat.completions.create(
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
            if data.get("error_code"):
                raise RecognitionError(data["error_code"], data.get("error_message", "Unable to recognize food."))
            return [AIItem(**item) for item in data.get("items", [])]

    except RecognitionError:
        raise
    except Exception as e:
        raise RecognitionError("AI_SERVICE_ERROR", f"AI service temporarily unavailable: {str(e)[:100]}")

    raise RecognitionError("AI_SERVICE_ERROR", "No AI provider configured.")
