import os
import asyncio
import re
from dotenv import load_dotenv

from typing import List, Dict, Optional
from prompts import SYSTEM_INSTRUCTION, PROMPT
from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.aiohttp import AsyncSocketModeHandler
from slack_sdk.web.async_client import AsyncWebClient

from google import genai

# Load environment variables from .env
load_dotenv()

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_MODEL = os.environ["GEMINI_MODEL"]

# Slack setup (Socket Mode)
app = AsyncApp(token=SLACK_BOT_TOKEN)
slack_client = AsyncWebClient(token=SLACK_BOT_TOKEN)

# Gemini client (google-genai)
client = genai.Client(api_key=GEMINI_API_KEY)

# Logging setup
import logging

# # Show only INFO and ERROR but hide WARNING
logging.basicConfig(level=logging.INFO)

# # Silence Slack Bolt & Slack SDK warnings
# logging.getLogger("slack_bolt").setLevel(logging.ERROR)
# logging.getLogger("slack_sdk").setLevel(logging.ERROR)
# logging.getLogger("aiohttp").setLevel(logging.ERROR)


# Bot User ID
async def get_bot_user_id():
    auth = await slack_client.auth_test()
    return auth.get("user_id", "")

BOT_USER_ID = asyncio.run(get_bot_user_id())


# Helper: channel history helpers
async def fetch_recent_channel_messages(channel_id: str, limit: int = 10) -> List[str]:
    msgs = []
    try:
        resp = await slack_client.conversations_history(channel=channel_id, limit=limit)
        for m in reversed(resp.get("messages", [])):
            txt = extract_message_text(m)
            user = m.get("user", "unknown")
            msgs.append(f"{user}: {txt}")
    except:
        pass
    return msgs


async def fetch_recent_thread_messages(
    channel_id: str, thread_ts: str, limit: int = 10
):
    msgs = []
    try:
        resp = await slack_client.conversations_replies(
            channel=channel_id, ts=thread_ts, limit=limit
        )
        for m in resp.get("messages", []):
            txt = extract_message_text(m)
            user = m.get("user", "unknown")
            msgs.append(f"{user}: {txt}")
    except:
        pass
    return msgs


# Helper Functions
async def extract_users(text: str) -> List[Dict]:
    """
    Extract Slack users from message text.
    Supports both:
        1. <@U12345> real Slack mentions
        2. @username plain text references
    Returns list of: { "id": "Uxxxx", "username": "name" }
    """
    results = []

    # Case 1: real Slack mentions: <@U12345> or <@U12345|username>
    real_matches = re.findall(r"<@([A-Z0-9]+)(?:\|[^>]+)?>", text)
    for uid in real_matches:
        results.append({"id": uid})

    # Case 2: plain @username
    plain_matches = re.findall(r"@([a-zA-Z0-9._-]+)", text)

    # Remove anything that *might* have matched real IDs
    plain_matches = [u for u in plain_matches if not u.upper().startswith("U")]

    # Resolve plain usernames → Slack users
    try:
        users = await slack_client.users_list()
        members = users.get("members", [])

        for uname in plain_matches:
            for m in members:
                profile = m.get("profile", {})
                # match ANY of Slack’s username formats
                if (
                    m.get("name") == uname
                    or profile.get("display_name") == uname
                    or profile.get("display_name_normalized") == uname
                    or profile.get("real_name_normalized") == uname
                ):
                    results.append({"id": m["id"], "username": m["name"]})
    except:
        pass

    return results


async def resolve_usernames(user_ids: List[str]) -> List[str]:
    """Resolve user IDs to usernames."""
    usernames = []
    for uid in user_ids:
        try:
            info = await slack_client.users_info(user=uid)
            uname = info.get("user", {}).get("name")
            if uname:
                usernames.append(uname)
        except:
            pass
    return usernames


def extract_message_text(m: dict) -> str:
    parts = []

    # Main text
    if m.get("text"):
        parts.append(m["text"].replace("\n", " "))

    # Images/files
    for f in m.get("files", []) or []:
        title = f.get("title") or f.get("name") or "file"
        mimetype = f.get("mimetype", "")
        if "image" in mimetype:
            parts.append(f"(uploaded an image: {title})")
        else:
            parts.append(f"(uploaded a file: {title})")

    # Rich text blocks
    for b in m.get("blocks", []) or []:
        if b.get("type") == "rich_text":
            for elem in b.get("elements", []):
                if elem.get("type") == "rich_text_section":
                    txt = "".join(
                        x.get("text", "")
                        for x in elem.get("elements", [])
                        if x.get("type") == "text"
                    )
                    if txt:
                        parts.append(txt)

    if not parts:
        return "(non-text message)"

    return " ".join(parts)


async def get_relevant_messages(
    question: str,
    channel_id: str,
    thread_ts: Optional[str],
    logger: any,
    max_results: int = 20,
) -> str:

    collected = []

    # 1. Extract mentioned users (real <@U123> + plain @username)
    extracted = await extract_users(question)

    mentioned_ids = set()
    mentioned_usernames = set()

    for e in extracted:
        uname = e.get("username")
        uid = e.get("id")
        if uname:
            mentioned_usernames.add(uname)
        if uid:
            mentioned_ids.add(uid)

    # Resolve any IDs that didn't have usernames
    unresolved_ids = set([uid for uid in mentioned_ids if uid not in mentioned_usernames])
    resolved = await resolve_usernames(unresolved_ids)
    for uname in resolved:
        mentioned_usernames.add(uname)

    logger.info(f"Mentioned IDs: {mentioned_ids}")
    logger.info(f"Mentioned usernames: {mentioned_usernames}")

    # STRICT MODE: User was explicitly mentioned
    mentioned_ids = list(mentioned_ids)
    if mentioned_ids:
        logger.info("Running STRICT USER MODE (manual scan)…")

        target_id = mentioned_ids[0]

        # 1. Manually scan channel history for user's messages
        try:
            resp = await slack_client.conversations_history(
                channel=channel_id, limit=1000
            )
            msgs = resp.get("messages", [])

            for m in msgs:
                if m.get("user") == target_id:
                    msg_text = extract_message_text(m)
                    collected.append(f"{target_id}: {msg_text}")

                    # record thread_ts if they started a thread
                    if m.get("thread_ts"):
                        thread_ts = m["thread_ts"]

        except Exception as e:
            logger.error(f"Manual history scan failed: {e}")

        # 2. Pull thread replies ONLY from the same user
        if thread_ts:
            try:
                replies = await slack_client.conversations_replies(
                    channel=channel_id, ts=thread_ts, limit=50
                )
                for m in replies.get("messages", []):
                    if m.get("user") == target_id:
                        msg_text = extract_message_text(m)
                        collected.append(f"{target_id}: {msg_text}")
            except Exception as e:
                logger.error(f"Thread scan failed: {e}")

        # Deduplicate
        seen = set()
        deduped = []
        for msg in collected:
            if msg not in seen:
                seen.add(msg)
                deduped.append(msg)

        if not deduped:
            return "(No messages found for that user.)"

        return "\n".join(deduped[:max_results])

    # No user mention → fallback to search + context
    logger.info("Running HYBRID MODE…")

    # Build search query
    query_parts = []

    # restrict to channel
    try:
        info = await slack_client.conversations_info(channel=channel_id)
        channel_name = info.get("channel", {}).get("name")
        if channel_name:
            query_parts.append(f"in:{channel_name}")
    except:
        pass

    clean_question = strip_bot_mention(question).strip()
    if clean_question:
        query_parts.append(clean_question)

    search_query = " ".join(query_parts)
    logger.info(f"Search query: {search_query}")

    # Search Slack
    try:
        resp = await slack_client.search_messages(query=search_query, count=max_results)
        matches = resp.get("messages", {}).get("matches", [])
    except:
        matches = []

    thread_ts_set = set()

    # Add search hits
    for m in matches:
        user = m.get("username") or m.get("user") or "unknown"
        msg_text = extract_message_text(m)
        collected.append(f"{user}: {msg_text}")
        thread_ts_set.add(m.get("thread_ts") or m.get("ts"))

    # Pull threads from matches
    for ts in thread_ts_set:
        try:
            replies = await slack_client.conversations_replies(
                channel=channel_id, ts=ts, limit=50
            )
            for m in replies.get("messages", []):
                user = m.get("user", "unknown")
                msg_text = extract_message_text(m)
                collected.append(f"{user}: {msg_text}")
        except:
            pass

    # Add recent thread context
    if thread_ts:
        collected.extend(
            await fetch_recent_thread_messages(channel_id, thread_ts, limit=10)
        )

    # Add recent channel context
    collected.extend(await fetch_recent_channel_messages(channel_id, limit=10))

    # Dedupe
    seen = set()
    deduped = []
    for msg in collected:
        if msg not in seen:
            seen.add(msg)
            deduped.append(msg)

    if not deduped:
        return "(No relevant messages found.)"

    return "\n".join(deduped[:max_results])


# Helper: clean question text (strip bot mention tags)
def strip_bot_mention(text: str) -> str:
    """
    Remove only the bot's own mention, not other users.
    """
    pattern = rf"<@{BOT_USER_ID}>"
    return re.sub(pattern, "", text).strip()


# Gemini call helper
async def ask_gemini(system_instruction: str, prompt: str, logger: any) -> str:
    # Ensure system_instruction and prompt are not None
    system_instruction = system_instruction or ""
    prompt = prompt or ""

    loop = asyncio.get_running_loop()

    def run_sync():
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[system_instruction, prompt],
        )
        return response.text

    # Run blocking sync Gemini call in a thread pool so it doesn't block Slack
    answer = await loop.run_in_executor(None, run_sync)

    return answer or "Sorry, I couldn't generate a response."


# Slash Command: /ask-sophia
@app.command("/ask-sophia")
async def slash_ask_sophia(ack, body, say, logger):
    logger.info("FULL_SLASH_PAYLOAD:")
    logger.info(body)
    await ack()

    question = body.get("text", "").strip()
    channel_id = body.get("channel_id")
    user_id = body.get("user_id")
    thread_ts = body.get("thread_ts") or body.get("ts")

    logger.info(f"/ask-sophia question: {question}")

    # Get relevant messages via Slack search
    relevant = await get_relevant_messages(
        question, channel_id=channel_id, thread_ts=thread_ts, logger=logger, max_results=20
    )

    prompt = PROMPT.format(question=question, relevant=relevant)

    try:
        answer = await ask_gemini(SYSTEM_INSTRUCTION, prompt, logger)
        await slack_client.chat_postEphemeral(
            channel=channel_id,
            user=user_id,
            text=f"*You asked:* {question}\n\n*Sophia says:*\n{answer}",
            thread_ts=thread_ts,
        )
        # await say(f"*You asked:* {question}\n\n*Sophia says:*\n{answer}")
    except Exception as e:
        logger.error(f"Gemini error in slash command: {e}")
        await say("Sophia ran into an issue while answering that.")


# Event: @Sophia mention
@app.event("app_mention")
async def handle_mention(event, say, logger):
    logger.info(f"@mention event: {event}")
    text = event.get("text", "")
    channel_id = event.get("channel")
    user_id = event.get("user")
    thread_ts = event.get("thread_ts") or event.get("ts")

    question = strip_bot_mention(text)
    logger.info(f"@mention question: {question}")

    relevant = await get_relevant_messages(
        question, channel_id=channel_id, thread_ts=thread_ts, logger=logger, max_results=20
    )

    prompt = PROMPT.format(question=question, relevant=relevant)

    try:
        answer = await ask_gemini(SYSTEM_INSTRUCTION, prompt, logger)
        await slack_client.chat_postEphemeral(
            channel=channel_id,
            user=user_id,
            text=f"*You asked:* {question}\n\n*Sophia says:*\n{answer}",
            thread_ts=thread_ts,
        )
        # await say(f"*You mentioned:* {question}\n\n*Sophia says:*\n{answer}")
    except Exception as e:
        logger.error(f"Gemini error in mention handler: {e}")
        await say("I had trouble handling that mention.")


# Function: Handle Extra Messages
@app.event("*")
async def catch_all_events(event, logger):
    logger.info(f"[UNHANDLED EVENT] {event}")


# Opens the WebSocket to Slack
async def main():
    handler = AsyncSocketModeHandler(app, SLACK_APP_TOKEN)
    await handler.start_async()


if __name__ == "__main__":
    asyncio.run(main())
