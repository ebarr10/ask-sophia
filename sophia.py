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

    if mentioned_ids or mentioned_usernames:
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
    except Exception as e:
        logger.error(f"Gemini error in slash command: {e}")
        await say("Sophia ran into an issue while answering that.")


# Slash Command: /joke-sophia
@app.command("/joke-sophia")
async def slash_joke_sophia(ack, body, logger):
    await ack()

    channel_id = body.get("channel_id")
    user_id = body.get("user_id")
    thread_ts = (
        body.get("thread_ts")
        or body.get("message_ts")
        or body.get("container", {}).get("thread_ts")
    )

    # Build the request for Gemini
    joke_prompt = """
        You are Sophia, a chaotic but helpful slack bot.
        Generate one short programming joke.
        Keep it concise, witty, and safe for work.
        It should not require explanation.
        Return ONLY the joke.
    """

    try:
        # Call Gemini using your existing helper
        joke = await ask_gemini(
            system_instruction="You are Sophia, an AI slack bot assistant.",
            prompt=joke_prompt,
            logger=logger,
        )

        if not joke:
            raise ValueError("Empty Gemini response")

    except Exception as e:
        logger.error(f"Gemini joke error: {e}")
        joke = "Why do programmers hate nature? Too many bugs."

    # Send ephemeral joke to user only
    await slack_client.chat_postEphemeral(
        channel=channel_id,
        user=user_id,
        text=f":robot_face: *Sophia's programming joke:*\n>{joke}",
        thread_ts=thread_ts,
    )

    logger.info(f"/joke-sophia joke sent to {user_id}: {joke}")


# Slash Command: /hungry-sophia
@app.command("/hungry-sophia")
async def slash_hungry_sophia(ack, body, logger):
    await ack()

    channel_id = body.get("channel_id")
    user_id = body.get("user_id")
    thread_ts = (
        body.get("thread_ts")
        or body.get("message_ts")
        or body.get("container", {}).get("thread_ts")
    )

    # 1% chance to trigger HARD MODE
    import random

    hard_mode = random.random() < 0.01

    if hard_mode:
        prompt = """
            You are Sophia, a chaotic Slack bot.
            The user asked: "What should I eat?"

            Generate ONE extremely over-the-top, 
            needlessly complex, 'final boss' level food idea.
            
            Requirements:
            - Must sound absolutely ridiculous.
            - Must be borderline impossible for a normal person.
            - Should lightly tease the user for asking.
            - Keep it short (2-4 sentences max).
            - You MUST end the response with: "Good luck."
        """
    else:
        prompt = """
            You are Sophia, a helpful but slightly sarcastic Slack bot.
            The user asked: "What should I eat?"

            Generate ONE practical suggestion.
            Choose one of:
            - simple homemade meal
            - easy snack
            - fast food idea
            - cheap/easy grocery-store food
            - lazy meal hacks (ramen + egg, tortilla pizzas, etc.)

            Keep it short and relatable.
            Only return the food idea, not explanations.
        """

    try:
        suggestion = await ask_gemini(
            system_instruction="You are Sophia, an AI who decides meals.",
            prompt=prompt,
            logger=logger,
        )

        if not suggestion:
            raise ValueError("Empty Gemini response")

    except Exception as e:
        logger.error(f"Sophia eat error: {e}")
        suggestion = "Peanut butter toast. It's foolproof."

    # Final ephemeral response
    await slack_client.chat_postEphemeral(
        channel=channel_id,
        user=user_id,
        text=f":fork_and_knife: *Sophia thinks you should eat:*\n>{suggestion}",
        thread_ts=thread_ts,
    )

    logger.info(f"/eat-sophia suggestion for {user_id}: {suggestion}")


# Slash Command: /roast-sophia
@app.command("/roast-sophia")
async def slash_roast_sophia(ack, body, logger):
    await ack()

    channel_id = body.get("channel_id")
    user_id = body.get("user_id")
    thread_ts = (
        body.get("thread_ts")
        or body.get("message_ts")
        or body.get("container", {}).get("thread_ts")
    )

    prompt = """
        You are Sophia, a chaotic Slack bot who roasts users in a funny, harmless way.

        Generate ONE short roast (1-2 sentences max).

        Rules:
        - Must be playful, not actually offensive.
        - Should sound like an internet meme or TikTok-level insult.
        - Avoid anything about protected classes, appearance, or anything sensitive.
        - Think: "You're dog water", "Built like a failed unit test", etc.
        - ONLY return the roast, nothing else.
    """

    try:
        roast = await ask_gemini(
            system_instruction="You are Sophia, a chaotic AI who roasts users.",
            prompt=prompt,
            logger=logger,
        )

        if not roast:
            raise ValueError("Empty Gemini response")

    except Exception as e:
        logger.error(f"Sophia roast error: {e}")
        roast = "You're the human equivalent of a semicolon in Python."

    await slack_client.chat_postEphemeral(
        channel=channel_id,
        user=user_id,
        text=f":fire: *Sophia has spoken:*\n>{roast}",
        thread_ts=thread_ts,
    )

    logger.info(f"/roast-sophia roast for {user_id}: {roast}")


# Slash Command: /roast-someone-sophia
@app.command("/roast-someone-sophia")
async def slash_roast_someone_sophia(ack, body, say, logger):
    await ack()

    channel_id = body.get("channel_id")
    user_id = body.get("user_id")
    command_text = body.get("text", "").strip()
    thread_ts = (
        body.get("thread_ts")
        or body.get("message_ts")
        or body.get("container", {}).get("thread_ts")
    )

    # Extract mentioned users from command text
    mentioned_users = await extract_users(command_text)

    if not mentioned_users:
        await slack_client.chat_postEphemeral(
            channel=channel_id,
            user=user_id,
            text=":warning: You need to mention a user to roast! Usage: `/roast-someone-sophia @username`",
            thread_ts=thread_ts,
        )
        return

    # Get the first mentioned user
    target_user = mentioned_users[0]
    target_user_id = target_user.get("id")

    # Resolve username if not already available
    target_username = target_user.get("username")
    if not target_username and target_user_id:
        usernames = await resolve_usernames([target_user_id])
        target_username = usernames[0] if usernames else target_user_id
    elif not target_username:
        target_username = "someone"

    prompt = f"""
        You are Sophia, a chaotic Slack bot who roasts users in a funny, harmless way.

        The user to roast is: {target_username}

        Generate ONE short roast (1-2 sentences max) specifically about {target_username}.

        Rules:
        - Must be playful, not actually offensive.
        - Should sound like an internet meme or TikTok-level insult.
        - Avoid anything about protected classes, appearance, or anything sensitive.
        - Think: "You're dog water", "Built like a failed unit test", etc.
        - Make it personal to {target_username} but keep it light and fun.
        - ONLY return the roast, nothing else.
    """

    try:
        roast = await ask_gemini(
            system_instruction="You are Sophia, a chaotic AI who roasts users.",
            prompt=prompt,
            logger=logger,
        )

        if not roast:
            raise ValueError("Empty Gemini response")

    except Exception as e:
        logger.error(f"Sophia roast error: {e}")
        roast = f"{target_username} is the human equivalent of a semicolon in Python."

    # Post publicly so everyone can see the roast
    try:
        await say(
            text=f":fire: *Sophia roasts <@{target_user_id}>:*\n>{roast}",
            thread_ts=thread_ts,
        )
    except Exception as e:
        logger.error(f"Failed to post roast: {e}")
        # Fallback: try posting as ephemeral to the user
        await slack_client.chat_postEphemeral(
            channel=channel_id,
            user=user_id,
            text=f":warning: Could not post roast in this conversation. Error: {str(e)}\n\n*The roast was:*\n>{roast}",
            thread_ts=thread_ts,
        )

    logger.info(f"/roast-someone-sophia roast for {target_username} ({target_user_id}): {roast}")


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
        await say(
            f"*You mentioned:* {question}\n\n*Sophia says:*\n{answer}", 
            thread_ts=thread_ts
        )
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
