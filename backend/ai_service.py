import os
import json
import base64
import logging
import re
from pathlib import Path
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
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "x-ai/grok-4")

openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
xai_client = (
    OpenAI(api_key=XAI_API_KEY, base_url="https://api.x.ai/v1", timeout=120.0)
    if XAI_API_KEY
    else None
)
openrouter_client = (
    OpenAI(
        api_key=OPENROUTER_API_KEY,
        base_url="https://openrouter.ai/api/v1",
        timeout=120.0,
        default_headers={
            "HTTP-Referer": os.getenv("OPENROUTER_SITE_URL", "http://localhost:8000"),
            "X-Title": "AFRC Prototype",
        },
    )
    if OPENROUTER_API_KEY
    else None
)

log = logging.getLogger("uvicorn.error")
PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"

def _read_prompt(filename: str) -> str:
    path = PROMPT_DIR / filename
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RecognitionError("AI_SERVICE_ERROR", f"Prompt file {filename} could not be read.") from exc
    if not text:
        raise RecognitionError("AI_SERVICE_ERROR", f"Prompt file {filename} is empty.")
    return text

def _with_menu(template: str, menu_text: str) -> str:
    if "{menu}" in template:
        return template.replace("{menu}", menu_text)
    return f"{template}\n\nToday's available menu:\n{menu_text}"

def _menu_names(menu: List[dict]) -> List[str]:
    return [d["name"] for d in menu if d.get("available", True) and d.get("name")]


def _menu_text(menu: List[dict]) -> str:
    lines = []
    for dish in menu:
        if not dish.get("available", True) or not dish.get("name"):
            continue
        description = str(dish.get("description") or "").strip()
        try:
            group = int(dish.get("pieces_per_portion") or 0)
        except (TypeError, ValueError):
            group = 0
        notes = []
        if description:
            notes.append(description)
        if group > 1:
            notes.append("Count each separate piece you can see. quantity is the piece count. Do not convert pieces into portions.")
        if notes:
            lines.append(f"- {dish['name']}: " + " ".join(notes))
        else:
            lines.append(f"- {dish['name']}")
    return "\n".join(lines)


def _normalize_name(name: str) -> str:
    return " ".join(name.strip().casefold().split())


def _snap_to_menu(items: List[AIItem], allowed_names: List[str]) -> List[AIItem]:
    table = {_normalize_name(name): name for name in allowed_names}
    snapped = []
    for item in items:
        canonical = table.get(_normalize_name(item.name))
        if not canonical:
            log.warning("Dropped AI item %r because it is not on today's menu %s", item.name, allowed_names)
            continue
        snapped.append(AIItem(name=canonical, quantity=item.quantity))
    return snapped


def _response_schema(allowed_names: List[str]) -> dict:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "plate_items",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "name": {"type": "string", "enum": allowed_names},
                                "quantity": {"type": "number"},
                            },
                            "required": ["name", "quantity"],
                        },
                    },
                    "error_code": {"type": ["string", "null"]},
                },
                "required": ["items", "error_code"],
            },
        },
    }


def _recognize_with_openai_compatible(
    client: OpenAI,
    model: str,
    image_bytes: bytes,
    prompt: str,
    allowed_names: List[str],
    menu_text: str,
    extra_body: Optional[dict] = None,
) -> List[AIItem]:
    b64 = base64.b64encode(image_bytes).decode()
    user_text = _with_menu(_read_prompt("user.txt"), menu_text)
    messages = [
        {"role": "system", "content": prompt},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "high"}},
            ],
        },
    ]
    request = {
        "model": model,
        "messages": messages,
        "max_tokens": 1500,
        "extra_body": extra_body,
    }
    try:
        response = client.chat.completions.create(
            **request,
            response_format=_response_schema(allowed_names),
        )
    except Exception as e:
        message = str(e).lower()
        if "response_format" not in message and "json_schema" not in message:
            raise
        log.warning("Structured menu schema was rejected (%s). Retrying with JSON object mode.", str(e)[:160])
        response = client.chat.completions.create(
            **request,
            response_format={"type": "json_object"},
        )
    content = response.choices[0].message.content or ""
    _log_ai_reply(model, content)
    try:
        data = _loads_model_json(content)
    except json.JSONDecodeError:
        log.warning("Model %s returned invalid JSON", model)
        response = client.chat.completions.create(
            **request,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content or ""
        _log_ai_reply(model, content, retry=True)
        try:
            data = _loads_model_json(content)
        except json.JSONDecodeError:
            log.warning("Model %s retry was still invalid JSON", model)
            raise RecognitionError(
                "AI_SERVICE_ERROR",
                "The vision model returned invalid JSON. Please retake the photo.",
            )
    if data.get("error_code"):
        raise RecognitionError(data["error_code"], data.get("error_message") or "Unable to recognize food.")
    items = _snap_to_menu([AIItem(name=item["name"], quantity=item["quantity"]) for item in data.get("items", [])], allowed_names)
    log.info("Menu match from %s: %s", model, [item.name for item in items])
    return items


def _log_ai_reply(model: str, content: str, retry: bool = False) -> None:
    label = "AI reply retry" if retry else "AI reply"
    log.info("%s from %s:\n%s", label, model, content)


def _extract_json(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[: text.rfind("```")]
        text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        text = text[start:end + 1]
    text = re.sub(r",\s*([}\]])", r"\1", text)
    return text.strip()


def _loads_model_json(content: str) -> dict:
    text = _extract_json(content)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        repaired = re.sub(r'([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:', r'\1"\2":', text)
        return json.loads(repaired)


def get_available_menu_text(menu: List[dict]) -> str:
    return "\n".join([f"- {d['name']} ({d['unit']})" for d in menu if d['available']])

class RecognitionError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)

async def recognize_food(image_bytes: bytes, available_menu: List[dict]) -> List[AIItem]:
    allowed_names = _menu_names(available_menu)
    menu_text = _menu_text(available_menu)
    prompt = _with_menu(_read_prompt("system.txt"), menu_text)

    if not allowed_names:
        raise RecognitionError("NO_MENU", "No dishes available today. Please ask staff.")

    if AI_PROVIDER == "mock":
        return [
            AIItem(name="Rice", quantity=1),
            AIItem(name="Chicken", quantity=1),
            AIItem(name="Vegetables", quantity=1),
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
            _log_ai_reply("gemini-2.5-flash", response.text or "")
            data = json.loads(response.text)
            if data.get("error_code"):
                raise RecognitionError(data["error_code"], data.get("error_message", "Unable to recognize food."))
            return _snap_to_menu([AIItem(name=item["name"], quantity=item["quantity"]) for item in data.get("items", [])], allowed_names)

        if AI_PROVIDER == "openai" and openai_client:
            return _recognize_with_openai_compatible(
                openai_client, "gpt-4o", image_bytes, prompt, allowed_names, menu_text
            )

        if AI_PROVIDER == "openrouter":
            if not openrouter_client:
                raise RecognitionError(
                    "AI_SERVICE_ERROR",
                    "OpenRouter is selected but OPENROUTER_API_KEY is missing. Add it to .env.",
                )
            return _recognize_with_openai_compatible(
                openrouter_client,
                OPENROUTER_MODEL,
                image_bytes,
                prompt,
                allowed_names,
                menu_text,
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
                allowed_names,
                menu_text,
                extra_body={"reasoning_effort": "low"},
            )

    except RecognitionError:
        raise
    except Exception as e:
        raise RecognitionError("AI_SERVICE_ERROR", f"AI service temporarily unavailable: {str(e)[:100]}")

    raise RecognitionError("AI_SERVICE_ERROR", "No AI provider configured.")
