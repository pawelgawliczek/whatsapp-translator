from fastapi import FastAPI, Request
import os, requests, json, logging
from collections import deque
from datetime import datetime
from langdetect import detect
from openai import OpenAI

app = FastAPI()
logger = logging.getLogger("wa-translator")

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
WA_API_BASE   = os.getenv("WA_API_BASE", "http://whatsapp-bot:8002")
TIMEOUT       = float(os.getenv("HTTP_TIMEOUT", "10"))
SEEN_IDS = deque(maxlen=500)

FAMILY_SYS_PROMPT = (
    "You are translating messages in a private family WhatsApp group. "
    "Translate into {target}. Keep names and emojis. Keep it casual and informal. "
    "No notes, no brackets, no explanations, no transliteration. Just the translation."
)

MEDIA_TYPES = {"image", "video", "audio", "ptt", "sticker", "document", "location", "contact", "liveLocation"}

def translate(text: str, target: str) -> str:
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    r = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role":"system","content":FAMILY_SYS_PROMPT.format(target=target)},
            {"role":"user","content":text}
        ],
        temperature=0
    )
    return r.choices[0].message.content.strip()

def send_text(chat_id: str, text: str):
    payload = {"args": {"to": chat_id, "content": text}}
    try:
        requests.post(
            f"{WA_API_BASE}/sendText",
            json=payload,
            timeout=TIMEOUT,
        ).raise_for_status()
    except Exception as exc:
        logger.warning("sendText failed for %s: %s", chat_id, exc)

@app.post("/wa/webhook")
async def wa_webhook(req: Request):
    payload = await req.json()
    if payload.get("event") != "onMessage":
        return {"ok": True}

    data = payload.get("data", {}) or {}
    msg_id   = str(data.get("id") or "")
    body     = data.get("body") or ""
    chat_id  = data.get("from") or ""
    is_group = chat_id.endswith("@g.us")
    from_me  = bool(data.get("fromMe") or data.get("authorIsMe"))

    # Ignore non-group, my own, duplicates
    if not (is_group and chat_id):
        return {"ok": True}
    if from_me or msg_id.startswith("true_") or msg_id in SEEN_IDS:
        return {"ok": True}
    SEEN_IDS.append(msg_id)

    # Ignore media (images, audio, video, stickers, docs, etc.), even if there is a caption
    msg_type = (data.get("type") or "").lower()
    mimetype = (data.get("mimetype") or "").lower()
    is_media_flag = bool(data.get("isMedia") or data.get("isMediaMessage"))
    mediaType = (data.get("mediaType") or "").lower()
    if (
        msg_type in MEDIA_TYPES
        or mediaType in MEDIA_TYPES
        or is_media_flag
        or mimetype.startswith("image/")
        or mimetype.startswith("video/")
        or mimetype.startswith("audio/")
        or mimetype.startswith("application/")  # documents
    ):
        return {"ok": True}

    # Nothing to translate
    if not body.strip():
        return {"ok": True}

    # Detect and route
    try:
        lang = detect(body)
    except Exception:
        return {"ok": True}

    if lang.startswith("en"):
        translated = translate(body, "Polish")
    elif lang.startswith("pl"):
        translated = translate(body, "English")
    else:
        return {"ok": True}

    sender = data.get("notifyName") or data.get("author") or "unknown"
    time_str = datetime.now().astimezone().strftime("%H:%M")
    formatted = f"{sender}/{time_str}: {translated}"

    send_text(chat_id, formatted)
    return {"ok": True}
