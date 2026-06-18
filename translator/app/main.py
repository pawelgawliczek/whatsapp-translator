from fastapi import FastAPI, Request
import os, requests, json, logging, threading, time
from pathlib import Path
from collections import deque, defaultdict
from datetime import datetime, timedelta
from langdetect import detect, detect_langs
from openai import OpenAI, OpenAIError, RateLimitError

app = FastAPI()
logger = logging.getLogger("wa-translator")

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
WA_API_BASE   = os.getenv("WA_API_BASE", "http://whatsapp-bot:8002")
TIMEOUT       = float(os.getenv("HTTP_TIMEOUT", "10"))
OWNER_CHAT_ID = os.getenv("OWNER_CHAT_ID")
SEEN_IDS = deque(maxlen=500)
LOG_BODY_LIMIT = int(os.getenv("LOG_BODY_LIMIT", "240"))
POLL_ACTIVE_CHATS = os.getenv("POLL_ACTIVE_CHATS", "true").lower() not in {"0", "false", "no"}
POLL_INTERVAL_SECONDS = float(os.getenv("POLL_INTERVAL_SECONDS", "15"))
POLL_STARTUP_REPLAY_SECONDS = float(os.getenv("POLL_STARTUP_REPLAY_SECONDS", "600"))
POLL_SEEN_IDS = deque(maxlen=1000)
POLL_SEEN_SET = set()
CONTACT_NAME_CACHE = {}

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

# Slavic/similar languages that langdetect confuses with Polish for short texts
POLISH_LIKE_LANGS = {"pl", "sk", "cs", "sl", "hr", "bs"}
ENGLISH_LIKE_FALSE_POSITIVES = {"so", "sw", "id", "tl", "af", "nl", "fr"}
ENGLISH_HINT_WORDS = {
    "a", "about", "am", "are", "be", "been", "but", "dad", "for", "from",
    "going", "good", "got", "had", "has", "have", "hahah", "hahaha", "hahahah",
    "he", "hello", "her", "him", "his", "hour", "hours", "i", "in", "is", "it",
    "love", "me", "mama", "minute", "minutes", "mom", "mum", "my", "of", "on",
    "see", "she", "so", "thank",
    "thanks", "that", "the", "this", "to", "waiting", "we", "will", "with", "you", "your",
}

def normalize_token(token: str) -> str:
    return "".join(ch for ch in token.lower() if ch.isalpha())

def looks_like_english(text: str) -> bool:
    tokens = [normalize_token(part) for part in text.split()]
    tokens = [token for token in tokens if token]
    if not tokens:
        return False
    hints = sum(1 for token in tokens if token in ENGLISH_HINT_WORDS)
    return hints >= 2 or (len(tokens) <= 3 and hints >= 1)

def detect_supported_language(text: str) -> str:
    lang = detect(text)
    if lang.startswith("en") or lang.startswith("pl") or lang in POLISH_LIKE_LANGS:
        return lang
    if lang in ENGLISH_LIKE_FALSE_POSITIVES and looks_like_english(text):
        logger.warning("[LANG-FIX] treating %s as en for body=%r", lang, text[:80])
        return "en"
    return lang

def body_preview(text: str) -> str:
    text = (text or "").replace("\n", "\\n")
    if len(text) <= LOG_BODY_LIMIT:
        return text
    return text[:LOG_BODY_LIMIT] + "..."

def message_meta(data: dict, msg_id: str, chat_id: str, sender: str, body: str) -> dict:
    return {
        "msg_id": msg_id[:40],
        "chat_id": chat_id,
        "sender": sender,
        "author": data.get("author") or "",
        "from_me": bool(data.get("fromMe") or data.get("authorIsMe")),
        "type": data.get("type") or "",
        "media_type": data.get("mediaType") or "",
        "is_media": bool(data.get("isMedia") or data.get("isMediaMessage")),
        "mimetype": data.get("mimetype") or "",
        "body": body_preview(body),
    }

def log_message_decision(level: int, decision: str, **fields):
    detail = " ".join(f"{key}={value!r}" for key, value in fields.items())
    logger.log(level, "[WA] decision=%s %s", decision, detail)

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

def build_dictionary_prompt(dictionary: list, text: str) -> str:
    if not dictionary:
        return ""
    # Only include dictionary entries where a source word actually appears in the text
    text_lower = text.lower()
    relevant = [(a, b) for a, b in dictionary if a.lower() in text_lower or b.lower() in text_lower]
    if not relevant:
        return ""
    lines = "\n".join(f"- {a} <-> {b}" for a, b in relevant)
    return (
        "\n\nThe following dictionary defines translations for specific words. "
        "ONLY apply these when the exact word (or a close inflected form of it) appears in the message. "
        "Do NOT use these translations for other similar or related words:\n" + lines
    )

def translate(text: str, target: str, context: list = None, dictionary: list = None) -> str:
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    if context and len(context) > 0:
        context_str = "\n".join(f"- {sender}: {body}" for sender, body in context)
        system_prompt = FAMILY_SYS_PROMPT_WITH_CONTEXT.format(target=target, context=context_str)
    else:
        system_prompt = FAMILY_SYS_PROMPT.format(target=target)

    system_prompt += build_dictionary_prompt(dictionary, text)

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
    if not chat_id:
        return
    payload = {"args": {"to": chat_id, "content": text}}
    try:
        response = requests.post(
            f"{WA_API_BASE}/sendText",
            json=payload,
            timeout=TIMEOUT,
        )
        response.raise_for_status()
    except Exception as exc:
        response_text = ""
        if "response" in locals():
            response_text = response.text[:300]
        logger.warning("sendText failed for %s: %s response=%r", chat_id, exc, response_text)

def resolve_sender_name(data: dict) -> str:
    notify_name = (data.get("notifyName") or "").strip()
    if notify_name:
        return notify_name

    sender_data = data.get("sender") if isinstance(data.get("sender"), dict) else {}
    sender_id = data.get("author") or sender_data.get("id") or ""
    if not sender_id:
        return "Someone"
    if sender_id in CONTACT_NAME_CACHE:
        return CONTACT_NAME_CACHE[sender_id]

    try:
        response = requests.post(
            f"{WA_API_BASE}/getContact",
            json={"args": [sender_id]},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        contact = response.json().get("response") or {}
        name = (
            contact.get("name")
            or contact.get("pushname")
            or contact.get("shortName")
            or ""
        ).strip()
        if name:
            CONTACT_NAME_CACHE[sender_id] = name
            return name
    except Exception as exc:
        logger.warning("[CONTACT] failed to resolve %s: %s", sender_id, exc)

    return "Someone"

def notify_owner(text: str):
    if OWNER_CHAT_ID:
        send_text(OWNER_CHAT_ID, text)

def remember_poll_id(msg_id: str):
    if not msg_id or msg_id in POLL_SEEN_SET:
        return
    if len(POLL_SEEN_IDS) == POLL_SEEN_IDS.maxlen:
        oldest = POLL_SEEN_IDS.popleft()
        POLL_SEEN_SET.discard(oldest)
    POLL_SEEN_IDS.append(msg_id)
    POLL_SEEN_SET.add(msg_id)

def fetch_chat_messages(chat_id: str) -> list:
    response = requests.post(
        f"{WA_API_BASE}/getAllMessagesInChat",
        json={"args": [chat_id, True, False]},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("success"):
        raise RuntimeError(payload.get("error") or "OpenWA getAllMessagesInChat failed")
    return payload.get("response") or []

def poll_active_chats_once(seed_only: bool = False):
    startup_cutoff = time.time() - POLL_STARTUP_REPLAY_SECONDS
    for chat_id in list(ACTIVE_CHATS):
        try:
            messages = fetch_chat_messages(chat_id)[-30:]
        except Exception as exc:
            logger.warning("[POLL] failed to fetch %s: %s", chat_id, exc)
            continue

        for msg in messages:
            msg_id = str(msg.get("id") or "")
            if not msg_id:
                continue
            message_timestamp = float(msg.get("t") or msg.get("timestamp") or 0)
            replay_after_startup = (
                seed_only
                and message_timestamp >= startup_cutoff
                and not bool(msg.get("fromMe") or msg.get("authorIsMe"))
            )
            if seed_only and not replay_after_startup:
                remember_poll_id(msg_id)
                continue
            if msg_id in POLL_SEEN_SET or msg_id in SEEN_IDS:
                continue
            remember_poll_id(msg_id)
            if bool(msg.get("fromMe") or msg.get("authorIsMe")):
                continue
            logger.warning(
                "[POLL] replaying missed message chat_id=%r msg_id=%r body=%r",
                chat_id,
                msg_id[:40],
                body_preview(msg.get("body") or msg.get("content") or ""),
            )
            try:
                requests.post(
                    "http://127.0.0.1:8000/wa/webhook",
                    json={"event": "onMessage", "data": msg},
                    timeout=TIMEOUT,
                ).raise_for_status()
            except Exception as exc:
                logger.warning("[POLL] replay failed for %s: %s", msg_id[:40], exc)

def poll_active_chats_loop():
    logger.warning("[POLL] active chat polling enabled interval=%ss", POLL_INTERVAL_SECONDS)
    time.sleep(5)
    poll_active_chats_once(seed_only=True)
    while True:
        time.sleep(POLL_INTERVAL_SECONDS)
        poll_active_chats_once(seed_only=False)

@app.on_event("startup")
def start_active_chat_polling():
    if POLL_ACTIVE_CHATS:
        thread = threading.Thread(target=poll_active_chats_loop, daemon=True)
        thread.start()

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
    msg_type = (data.get("type") or "").lower()
    sender = resolve_sender_name(data)
    meta = message_meta(data, msg_id, chat_id, sender, body)

    log_message_decision(logging.WARNING, "received", **meta)

    # Ignore own messages and duplicates
    if not chat_id:
        log_message_decision(logging.WARNING, "drop_no_chat", **meta)
        return {"ok": True}
    if from_me or msg_id.startswith("true_"):
        log_message_decision(logging.INFO, "drop_from_me", **meta)
        return {"ok": True}
    if msg_id in SEEN_IDS:
        log_message_decision(logging.WARNING, "drop_duplicate", **meta)
        return {"ok": True}
    SEEN_IDS.append(msg_id)

    # Ignore media (images, audio, video, stickers, docs, etc.), even if there is a caption
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
        log_message_decision(logging.WARNING, "drop_media", **meta)
        return {"ok": True}

    # Nothing to translate
    if not body.strip():
        log_message_decision(logging.WARNING, "drop_empty_body", **meta)
        return {"ok": True}

    # --- /translate command handling ---
    body_lower = body.strip().lower()
    if body_lower == "/translate":
        if chat_id in ACTIVE_CHATS:
            send_text(chat_id, "Translation is already active in this chat.")
            log_message_decision(logging.WARNING, "command_translate_already_active", **meta)
        else:
            ACTIVE_CHATS.add(chat_id)
            save_active_chats()
            send_text(chat_id, "Translation activated! I'll now translate messages in this chat.")
            log_message_decision(logging.WARNING, "command_translate_activated", **meta)
        return {"ok": True}

    if body_lower == "/translate off":
        if chat_id in ACTIVE_CHATS:
            ACTIVE_CHATS.discard(chat_id)
            save_active_chats()
            send_text(chat_id, "Translation deactivated for this chat.")
            log_message_decision(logging.WARNING, "command_translate_deactivated", **meta)
        else:
            send_text(chat_id, "Translation is not active in this chat.")
            log_message_decision(logging.WARNING, "command_translate_not_active", **meta)
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
        log_message_decision(logging.WARNING, "drop_chat_not_active", active_chats=sorted(ACTIVE_CHATS), **meta)
        sender_jid = data.get("author") or data.get("from") or ""
        if sender_jid != OWNER_CHAT_ID and chat_id != OWNER_CHAT_ID:
            chat_name = data.get("chat", {}).get("name") or data.get("chatName") or chat_id
            send_text(OWNER_CHAT_ID, f"[Message from {sender} in {chat_name}]\n{body}")
        return {"ok": True}

    # --- Translation logic (only for active chats) ---
    try:
        lang = detect_supported_language(body)
    except Exception as exc:
        log_message_decision(logging.WARNING, "drop_langdetect_failed", error=str(exc), **meta)
        return {"ok": True}

    log_message_decision(logging.WARNING, "translate_attempt", detected_lang=lang, **meta)

    chat_dict = DICTIONARIES.get(chat_id, [])

    # Store message in history (before translation, so it can be used as context for future messages)
    MESSAGE_HISTORY[chat_id].append((datetime.now(), sender, body, lang))

    try:
        if lang.startswith("en"):
            context = get_context_messages(chat_id, "en")
            # Exclude the current message from context (it's already the message to translate)
            context = [c for c in context if c[1] != body]
            translated = translate(body, "Polish", context, dictionary=chat_dict)
        elif lang.startswith("pl") or lang in POLISH_LIKE_LANGS:
            if lang != "pl":
                logger.warning("[LANG-FIX] treating %s as pl for body=%r", lang, body[:80])
            context = get_context_messages(chat_id, "pl")
            context = [c for c in context if c[1] != body]
            translated = translate(body, "English", context, dictionary=chat_dict)
        else:
            log_message_decision(logging.WARNING, "drop_unsupported_lang", detected_lang=lang, **meta)
            return {"ok": True}
    except RateLimitError as exc:
        logger.error("[OPENAI] rate limit or quota error: %s", exc)
        log_message_decision(logging.ERROR, "drop_openai_rate_limit", detected_lang=lang, error=str(exc), **meta)
        notify_owner(
            "Translation failed because OpenAI returned a rate limit or quota error. "
            "Check the API key billing/usage limits."
        )
        return {"ok": True}
    except OpenAIError as exc:
        logger.error("[OPENAI] translation failed: %s", exc)
        log_message_decision(logging.ERROR, "drop_openai_error", detected_lang=lang, error=type(exc).__name__, **meta)
        notify_owner(f"Translation failed because OpenAI returned an error: {type(exc).__name__}.")
        return {"ok": True}

    time_str = datetime.now().astimezone().strftime("%H:%M")
    formatted = f"{sender}/{time_str}: {translated}"

    log_message_decision(
        logging.WARNING,
        "send_translation",
        detected_lang=lang,
        target="Polish" if lang.startswith("en") else "English",
        translated=body_preview(translated),
        **meta,
    )
    send_text(chat_id, formatted)
    return {"ok": True}


@app.get("/debug/chat/{chat_id}")
def debug_chat(chat_id: str):
    """Diagnostic endpoint: fetch recent messages from a chat via OpenWA API
    and correlate with in-memory translation history.

    Usage: GET /debug/chat/YOUR_NUMBER-1619174425@g.us
    """
    # Fetch recent messages from WhatsApp via OpenWA
    wa_messages = []
    try:
        resp = requests.post(
            f"{WA_API_BASE}/getChat",
            json={"args": {"contactId": f"{chat_id}"}},
            timeout=TIMEOUT,
        )
        if resp.ok:
            chat_data = resp.json().get("response", {})
            wa_messages = [
                {
                    "id": str(m.get("id", "")),
                    "body": (m.get("body") or "")[:120],
                    "from": m.get("from", ""),
                    "author": m.get("author", ""),
                    "sender": m.get("notifyName", ""),
                    "fromMe": m.get("fromMe", False),
                    "t": m.get("t", 0),
                    "type": m.get("type", ""),
                }
                for m in chat_data.get("msgs", [])
            ]
    except Exception as exc:
        wa_messages = [{"error": str(exc)}]

    # In-memory translation history
    history = []
    for ts, sender, body, lang in MESSAGE_HISTORY.get(chat_id, []):
        history.append({
            "time": ts.isoformat(),
            "sender": sender,
            "body": body[:120],
            "detected_lang": lang,
        })

    return {
        "chat_id": chat_id,
        "is_active": chat_id in ACTIVE_CHATS,
        "active_chats": sorted(ACTIVE_CHATS),
        "dictionary_entries": len(DICTIONARIES.get(chat_id, [])),
        "translation_history_count": len(history),
        "translation_history": history[-20:],
        "wa_recent_messages": wa_messages[-20:],
        "seen_ids_count": len(SEEN_IDS),
    }
