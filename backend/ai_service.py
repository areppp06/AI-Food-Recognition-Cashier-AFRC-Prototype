import os
import json
import base64
from typing import List, Optional, Union
from openai import OpenAI
from google import genai
from google.genai import types
from .schemas import AIItem, AIResponse
import httpx

AI_PROVIDER = os.getenv("AI_PROVIDER", "grok").strip().lower()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
XAI_API_KEY = os.getenv("XAI_API_KEY")
XAI_MODEL = os.getenv("XAI_MODEL", "grok-4.7")

openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
xai_client = (
    OpenAI(api_key=XAI_API_KEY, base_url="https://api.x.ai/v1", timeout=120.0)
    if XAI_API_KEY
    else None
)

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

def _recognize_with_openai_compatible(
    client: OpenAI,
    model: str,
    image_bytes: bytes,
    prompt: str,
    extra_body: Optional[dict] = None,
) -> List[AIItem]:
    b64 = base64.b64encode(image_bytes).decode()
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Recognize the food on this plate. Reply with JSON only."},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "high"}},
                ],
            },
        ],
        response_format={"type": "json_object"},
        max_tokens=1500,
        extra_body=extra_body,
    )
    content = response.choices[0].message.content or ""
    data = json.loads(_extract_json(content))
    if data.get("error_code"):
        raise RecognitionError(data["error_code"], data.get("error_message", "Unable to recognize food."))
    return [AIItem(**item) for item in data.get("items", [])]


def _extract_json(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[: text.rfind("```")]
    return text.strip()


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

    if AI_PROVIDER == "mock":
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
            return _recognize_with_openai_compatible(
                openai_client, "gpt-4o", image_bytes, prompt
            )

        if AI_PROVIDER in ("grok", "xai"):
            if not xai_client:
                raise RecognitionError(
                    "AI_SERVICE_ERROR",
                    "Grok is selected but XAI_API_KEY is missing. Add it to .env.",
                )
            return _recognize_with_openai_compatible(
                xai_client,
                XAI_MODEL,
                image_bytes,
                prompt,
                extra_body={"reasoning_effort": "low"},
            )

    except RecognitionError:
        raise
    except Exception as e:
        raise RecognitionError("AI_SERVICE_ERROR", f"AI service temporarily unavailable: {str(e)[:100]}")

    raise RecognitionError("AI_SERVICE_ERROR", "No AI provider configured.")
