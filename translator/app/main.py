from fastapi import FastAPI, Request
import os, requests, json, logging
from pathlib import Path
from collections import deque, defaultdict
from datetime import datetime, timedelta
from langdetect import detect
from openai import OpenAI

app = FastAPI()
logger = logging.getLogger("wa-translator")

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
WA_API_BASE   = os.getenv("WA_API_BASE", "http://whatsapp-bot:8002")
TIMEOUT       = float(os.getenv("HTTP_TIMEOUT", "10"))
OWNER_CHAT_ID = os.getenv("OWNER_CHAT_ID", "YOUR_NUMBER@c.us")
SEEN_IDS = deque(maxlen=500)

# Persistent active-chats list
DATA_DIR = Path("/app/data")
ACTIVE_CHATS_FILE = DATA_DIR / "active_chats.json"

def load_active_chats() -> set:
    try:
        return set(json.loads(ACTIVE_CHATS_FILE.read_text()))
    except Exception:
        return set()

def save_active_chats():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ACTIVE_CHATS_FILE.write_text(json.dumps(sorted(ACTIVE_CHATS)))

ACTIVE_CHATS = load_active_chats()

# Persistent per-chat dictionaries
DICTIONARIES_FILE = DATA_DIR / "dictionaries.json"

def load_dictionaries() -> dict:
    try:
        return json.loads(DICTIONARIES_FILE.read_text())
    except Exception:
        return {}

def save_dictionaries():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DICTIONARIES_FILE.write_text(json.dumps(DICTIONARIES, indent=2))

DICTIONARIES = load_dictionaries()

# Message history per chat: {chat_id: deque of (timestamp, sender, body, lang)}
MESSAGE_HISTORY = defaultdict(lambda: deque(maxlen=50))
CONTEXT_MAX_AGE = timedelta(hours=1)
CONTEXT_MAX_MESSAGES = 10

FAMILY_SYS_PROMPT = (
    "You are translating messages in a private family WhatsApp group. "
    "Translate into {target}. Keep names and emojis. Keep it casual and informal. "
    "No notes, no brackets, no explanations, no transliteration. Just the translation."
)

FAMILY_SYS_PROMPT_WITH_CONTEXT = (
    "You are translating messages in a private family WhatsApp group. "
    "Translate into {target}. Keep names and emojis. Keep it casual and informal. "
    "No notes, no brackets, no explanations, no transliteration. Just the translation.\n\n"
    "For context, here are recent messages from the conversation (in the original language - DO NOT translate these):\n"
    "{context}\n\n"
    "Now translate ONLY the next message I send you."
)

MEDIA_TYPES = {"image", "video", "audio", "ptt", "sticker", "document", "location", "contact", "liveLocation"}

def get_context_messages(chat_id: str, source_lang: str) -> list:
    """Get up to 10 messages from the last hour in the source language."""
    now = datetime.now()
    cutoff = now - CONTEXT_MAX_AGE
    history = MESSAGE_HISTORY[chat_id]

    # Filter messages: same language, within last hour
    context = []
    for ts, sender, body, lang in reversed(history):
        if ts < cutoff:
            break
        if lang.startswith(source_lang[:2]):
            context.append((sender, body))
        if len(context) >= CONTEXT_MAX_MESSAGES:
            break

    # Reverse to chronological order
    return list(reversed(context))

def build_dictionary_prompt(dictionary: list) -> str:
    if not dictionary:
        return ""
    lines = "\n".join(f"- {a} <-> {b}" for a, b in dictionary)
    return (
        "\n\nYou MUST use the following dictionary for specific word translations. "
        "These are bidirectional pairs. When you encounter any of these words, "
        "always translate them using the paired word:\n" + lines
    )

def translate(text: str, target: str, context: list = None, dictionary: list = None) -> str:
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    if context and len(context) > 0:
        context_str = "\n".join(f"- {sender}: {body}" for sender, body in context)
        system_prompt = FAMILY_SYS_PROMPT_WITH_CONTEXT.format(target=target, context=context_str)
    else:
        system_prompt = FAMILY_SYS_PROMPT.format(target=target)

    system_prompt += build_dictionary_prompt(dictionary)

    r = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role":"system","content":system_prompt},
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
    from_me  = bool(data.get("fromMe") or data.get("authorIsMe"))

    # Ignore own messages and duplicates
    if not chat_id:
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

    # --- /translate command handling ---
    body_lower = body.strip().lower()
    if body_lower == "/translate":
        if chat_id in ACTIVE_CHATS:
            send_text(chat_id, "Translation is already active in this chat.")
        else:
            ACTIVE_CHATS.add(chat_id)
            save_active_chats()
            send_text(chat_id, "Translation activated! I'll now translate messages in this chat.")
        return {"ok": True}

    if body_lower == "/translate off":
        if chat_id in ACTIVE_CHATS:
            ACTIVE_CHATS.discard(chat_id)
            save_active_chats()
            send_text(chat_id, "Translation deactivated for this chat.")
        else:
            send_text(chat_id, "Translation is not active in this chat.")
        return {"ok": True}

    # --- /dictionary command handling ---
    if body_lower.startswith("/dictionary"):
        args = body.strip()[len("/dictionary"):].strip()
        args_lower = args.lower()

        if args_lower.startswith("add "):
            pair_str = args[4:].strip()
            if "," not in pair_str:
                send_text(chat_id, "Usage: /dictionary add word1, word2")
                return {"ok": True}
            word_a, word_b = pair_str.split(",", 1)
            word_a, word_b = word_a.strip(), word_b.strip()
            if not word_a or not word_b:
                send_text(chat_id, "Usage: /dictionary add word1, word2")
                return {"ok": True}
            # Case-insensitive duplicate check (both directions)
            chat_dict = DICTIONARIES.get(chat_id, [])
            for a, b in chat_dict:
                if (a.lower() == word_a.lower() and b.lower() == word_b.lower()) or \
                   (a.lower() == word_b.lower() and b.lower() == word_a.lower()):
                    send_text(chat_id, f"Duplicate: {a} <-> {b} already exists.")
                    return {"ok": True}
            chat_dict.append([word_a, word_b])
            DICTIONARIES[chat_id] = chat_dict
            save_dictionaries()
            send_text(chat_id, f"Added to dictionary: {word_a} <-> {word_b}")
            return {"ok": True}

        elif args_lower == "list":
            chat_dict = DICTIONARIES.get(chat_id, [])
            if not chat_dict:
                send_text(chat_id, "Dictionary is empty for this chat.")
            else:
                lines = [f"{i+1}. {a} <-> {b}" for i, (a, b) in enumerate(chat_dict)]
                send_text(chat_id, "\n".join(lines))
            return {"ok": True}

        elif args_lower.startswith("remove "):
            pair_str = args[7:].strip()
            chat_dict = DICTIONARIES.get(chat_id, [])
            if not chat_dict:
                send_text(chat_id, "Dictionary is empty for this chat.")
                return {"ok": True}

            if "," in pair_str:
                # Pair remove: match either direction, case-insensitive
                word_a, word_b = pair_str.split(",", 1)
                word_a, word_b = word_a.strip().lower(), word_b.strip().lower()
                new_dict = [
                    p for p in chat_dict
                    if not ((p[0].lower() == word_a and p[1].lower() == word_b) or
                            (p[0].lower() == word_b and p[1].lower() == word_a))
                ]
            else:
                # Single-word remove: remove ALL entries containing that word on either side
                word = pair_str.strip().lower()
                new_dict = [
                    p for p in chat_dict
                    if p[0].lower() != word and p[1].lower() != word
                ]

            removed = len(chat_dict) - len(new_dict)
            if removed == 0:
                send_text(chat_id, "No matching entries found.")
            else:
                if new_dict:
                    DICTIONARIES[chat_id] = new_dict
                else:
                    DICTIONARIES.pop(chat_id, None)
                save_dictionaries()
                send_text(chat_id, f"Removed {removed} entr{'y' if removed == 1 else 'ies'} from dictionary.")
            return {"ok": True}

        else:
            send_text(chat_id,
                "Usage:\n"
                "/dictionary add word1, word2\n"
                "/dictionary list\n"
                "/dictionary remove word1, word2\n"
                "/dictionary remove word"
            )
            return {"ok": True}

    # --- If chat is NOT active, forward notification to owner ---
    if chat_id not in ACTIVE_CHATS:
        sender_jid = data.get("author") or data.get("from") or ""
        if sender_jid != OWNER_CHAT_ID and chat_id != OWNER_CHAT_ID:
            sender = data.get("notifyName") or data.get("author") or "unknown"
            chat_name = data.get("chat", {}).get("name") or data.get("chatName") or chat_id
            send_text(OWNER_CHAT_ID, f"[Message from {sender} in {chat_name}]\n{body}")
        return {"ok": True}

    # --- Translation logic (only for active chats) ---
    try:
        lang = detect(body)
    except Exception:
        return {"ok": True}

    sender = data.get("notifyName") or data.get("author") or "unknown"
    chat_dict = DICTIONARIES.get(chat_id, [])

    # Store message in history (before translation, so it can be used as context for future messages)
    MESSAGE_HISTORY[chat_id].append((datetime.now(), sender, body, lang))

    if lang.startswith("en"):
        context = get_context_messages(chat_id, "en")
        # Exclude the current message from context (it's already the message to translate)
        context = [c for c in context if c[1] != body]
        translated = translate(body, "Polish", context, dictionary=chat_dict)
    elif lang.startswith("pl"):
        context = get_context_messages(chat_id, "pl")
        context = [c for c in context if c[1] != body]
        translated = translate(body, "English", context, dictionary=chat_dict)
    else:
        return {"ok": True}

    time_str = datetime.now().astimezone().strftime("%H:%M")
    formatted = f"{sender}/{time_str}: {translated}"

    send_text(chat_id, formatted)
    return {"ok": True}
