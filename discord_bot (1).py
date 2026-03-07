"""
DevRel AI — Discord Bot (Fixed)
Monitors the #help channel and answers questions using Claude AI.
"""

from __future__ import annotations

import asyncio
import logging
import os
import ssl
ssl._create_default_https_context = ssl._create_unverified_context

import discord
import httpx
from discord.ext import commands

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ── config ─────────────────────────────────────────────────────────────────────
BACKEND_URL       = os.environ.get("BACKEND_URL", "http://localhost:8000")
DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
DEFAULT_ORG_ID    = os.environ.get("DEFAULT_ORG_ID", "")
DISCORD_SERVER_ID = os.environ.get("DISCORD_SERVER_ID", "")

# Pre-populate mappings from environment variables so the bot works immediately
SERVER_TO_ORG: dict[str, str] = {}
WATCHED_CHANNELS: dict[str, set[str]] = {}

if DISCORD_SERVER_ID and DEFAULT_ORG_ID:
    SERVER_TO_ORG[DISCORD_SERVER_ID] = DEFAULT_ORG_ID
    WATCHED_CHANNELS[DISCORD_SERVER_ID] = {"help", "questions", "dev-support", "support"}
    log.info(f"Pre-loaded server {DISCORD_SERVER_ID} → org {DEFAULT_ORG_ID}")

REPLIED_TO: set[int] = set()

# ── intents ────────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.guilds           = True
intents.messages         = True
intents.reactions        = True

bot = commands.Bot(command_prefix="!", intents=intents)


# ═══════════════════════════════════════════════════════════════════════════════
# STARTUP
# ═══════════════════════════════════════════════════════════════════════════════

@bot.event
async def on_ready():
    log.info(f"DevRel AI bot online as {bot.user} (ID: {bot.user.id})")
    log.info(f"Watching {len(SERVER_TO_ORG)} server(s)")
    for sid, oid in SERVER_TO_ORG.items():
        log.info(f"  Server {sid} → Org {oid} | Channels: {WATCHED_CHANNELS.get(sid)}")


# ═══════════════════════════════════════════════════════════════════════════════
# MESSAGE HANDLING
# ═══════════════════════════════════════════════════════════════════════════════

@bot.event
async def on_message(message: discord.Message):
    # Ignore messages from bots
    if message.author.bot:
        return

    server_id = str(message.guild.id) if message.guild else None
    if not server_id:
        return

    # If no org mappings at all, use the default org for ALL servers
    if not SERVER_TO_ORG and DEFAULT_ORG_ID:
        org_id = DEFAULT_ORG_ID
    else:
        org_id = SERVER_TO_ORG.get(server_id)

    if not org_id:
        await bot.process_commands(message)
        return

    # Check if this is a watched channel
    channel_name = message.channel.name if hasattr(message.channel, "name") else ""
    watched = WATCHED_CHANNELS.get(server_id, {"help", "questions", "dev-support", "support"})
    if channel_name not in watched:
        await bot.process_commands(message)
        return

    if message.id in REPLIED_TO:
        return

    log.info(f"Question received in #{channel_name} from {message.author}: {message.content[:80]}")

    # Send to backend
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            r = await client.post(f"{BACKEND_URL}/questions/ask", json={
                "org_id":             org_id,
                "platform":           "discord",
                "channel":            f"#{channel_name}",
                "author_username":    str(message.author),
                "author_external_id": str(message.author.id),
                "content":            message.content,
                "thread_context":     [],
            })
            result = r.json()
            log.info(f"Backend response: action={result.get('action')}, confidence={result.get('confidence')}")
        except Exception as e:
            log.error(f"Backend request failed: {e}")
            # Reply with a fallback message so the user gets a response
            await message.reply("I'm having trouble connecting to my backend right now. Please try again in a moment!")
            return

    action = result.get("action")

    if action == "auto_posted" and result.get("answer"):
        await post_answer(message, result["answer"], result.get("confidence", 0))
        REPLIED_TO.add(message.id)

    elif action == "escalated":
        await post_escalation_notice(message, result.get("confidence", 0))
        REPLIED_TO.add(message.id)

    elif action == "ignored":
        pass

    else:
        # Fallback — reply with whatever answer we got
        answer = result.get("answer") or result.get("detail") or "I received your question but couldn't generate a response. Please try again."
        await message.reply(answer)
        REPLIED_TO.add(message.id)

    await bot.process_commands(message)


async def post_answer(message: discord.Message, answer: str, confidence: float):
    embed = discord.Embed(
        description=answer,
        color=discord.Color.blue()
    )
    embed.set_footer(text=f"DevRel AI • Confidence: {confidence:.0f}%")
    await message.reply(embed=embed)
    try:
        reply = await message.channel.fetch_message(message.id)
        await reply.add_reaction("👍")
        await reply.add_reaction("👎")
    except Exception:
        pass


async def post_escalation_notice(message: discord.Message, confidence: float):
    embed = discord.Embed(
        title="👋 Escalated to the team",
        description="This question needs a human touch. Our DevRel team has been notified and will respond shortly.",
        color=discord.Color.orange()
    )
    embed.set_footer(text=f"DevRel AI • Confidence too low: {confidence:.0f}%")
    await message.reply(embed=embed)


# ═══════════════════════════════════════════════════════════════════════════════
# PREFIX COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════

@bot.command(name="ask")
async def ask(ctx, *, question: str):
    """Ask the DevRel AI a question using !ask <question>"""
    org_id = DEFAULT_ORG_ID or (SERVER_TO_ORG.get(str(ctx.guild.id)) if ctx.guild else None)
    if not org_id:
        await ctx.send("Bot is not configured for this server yet.")
        return

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            r = await client.post(f"{BACKEND_URL}/questions/ask", json={
                "org_id":             org_id,
                "platform":           "discord",
                "channel":            f"#{ctx.channel.name}",
                "author_username":    str(ctx.author),
                "author_external_id": str(ctx.author.id),
                "content":            question,
                "thread_context":     [],
            })
            result = r.json()
            answer = result.get("answer", "I could not generate an answer. Please try again.")
        except Exception as e:
            answer = f"Backend error: {e}"

    embed = discord.Embed(description=answer, color=discord.Color.blue())
    embed.set_footer(text="DevRel AI")
    await ctx.send(embed=embed)


# ═══════════════════════════════════════════════════════════════════════════════
# RUN
# ═══════════════════════════════════════════════════════════════════════════════

bot.run(DISCORD_BOT_TOKEN)
