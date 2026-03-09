"""
DEVAD Discord Bot — Multi-tenant version
Loads all server→org mappings from the database dynamically.
New customers are picked up automatically without restarting.
"""

import os
import asyncio
import logging
import aiohttp
import discord
from discord.ext import commands, tasks

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

BACKEND_URL = os.environ.get("BACKEND_URL", "").rstrip("/")
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
DEFAULT_ORG_ID = os.environ.get("DEFAULT_ORG_ID", "")
DISCORD_SERVER_ID = os.environ.get("DISCORD_SERVER_ID", "")

WATCH_CHANNELS = {"help", "questions", "support", "dev-support", "developer-support"}

# In-memory map: server_id (str) → org_id (str)
server_org_map: dict = {}

# ── Load org mappings from backend ────────────────────────────────────────────

async def load_org_mappings():
    """Fetch all Discord server→org mappings from the backend."""
    global server_org_map

    # Always include the default server from env vars
    if DISCORD_SERVER_ID and DEFAULT_ORG_ID:
        server_org_map[str(DISCORD_SERVER_ID)] = DEFAULT_ORG_ID
        log.info(f"Pre-loaded default server {DISCORD_SERVER_ID} → org {DEFAULT_ORG_ID}")

    if not BACKEND_URL:
        log.warning("BACKEND_URL not set — using env var mappings only")
        return

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{BACKEND_URL}/integrations/discord",
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    integrations = await resp.json()
                    for item in integrations:
                        sid = str(item.get("server_id", ""))
                        oid = str(item.get("org_id", ""))
                        if sid and oid:
                            server_org_map[sid] = oid
                    log.info(f"Loaded {len(server_org_map)} org mappings from backend")
                else:
                    log.warning(f"Failed to load mappings: {resp.status}")
    except Exception as e:
        log.error(f"Error loading org mappings: {e}")


# ── Discord bot setup ──────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    log.info(f"DevRel AI bot online as {bot.user} (ID: {bot.user.id})")
    await load_org_mappings()
    log.info(f"Watching {len(bot.guilds)} server(s)")
    for guild in bot.guilds:
        org_id = server_org_map.get(str(guild.id), "unmapped")
        channels = [c.name for c in guild.channels if c.name in WATCH_CHANNELS]
        log.info(f"  Server {guild.id} ({guild.name}) → Org {org_id} | Channels: {set(channels)}")
    # Start background refresh
    refresh_mappings.start()


@tasks.loop(minutes=5)
async def refresh_mappings():
    """Refresh org mappings every 5 minutes to pick up new customers."""
    await load_org_mappings()
    log.info(f"Refreshed mappings — {len(server_org_map)} orgs loaded")


@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if message.channel.name not in WATCH_CHANNELS:
        return

    server_id = str(message.guild.id)
    org_id = server_org_map.get(server_id)

    if not org_id:
        log.warning(f"No org mapping for server {server_id} — ignoring message")
        return

    question = message.content.strip()
    if not question or len(question) < 5:
        return

    log.info(f"Question in #{message.channel.name} from {message.author.name} (org={org_id}): {question[:80]}")

    # Show typing indicator
    async with message.channel.typing():
        try:
            async with aiohttp.ClientSession() as session:
                payload = {
                    "org_id": org_id,
                    "platform": "discord",
                    "channel": f"#{message.channel.name}",
                    "author_username": str(message.author.name),
                    "author_external_id": str(message.author.id),
                    "content": question,
                    "thread_context": [],
                }
                async with session.post(
                    f"{BACKEND_URL}/questions/ask",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        answer = data.get("answer")
                        action = data.get("action", "escalated")
                        confidence = data.get("confidence", 0)

                        if action == "auto_posted" and answer:
                            # Split long answers to avoid Discord's 2000 char limit
                            if len(answer) > 1900:
                                chunks = [answer[i:i+1900] for i in range(0, len(answer), 1900)]
                                for chunk in chunks:
                                    await message.reply(chunk)
                            else:
                                await message.reply(answer)
                            log.info(f"Answered (confidence={confidence}%)")
                        else:
                            await message.reply(
                                "I'm not confident enough to answer this automatically. "
                                "A human will follow up shortly! 🙋"
                            )
                    else:
                        log.error(f"Backend error: {resp.status}")
                        await message.reply("Having trouble connecting to my brain right now. Please try again in a moment! 🔧")

        except asyncio.TimeoutError:
            await message.reply("That took too long to process. Please try again! ⏱️")
        except Exception as e:
            log.error(f"Error processing message: {e}")
            await message.reply("Something went wrong. Please try again! 🔧")

    await bot.process_commands(message)


@bot.command(name="ping")
async def ping(ctx):
    org_id = server_org_map.get(str(ctx.guild.id), "unmapped")
    await ctx.send(f"✅ DEVAD bot is online! Org: `{org_id}`")


@bot.command(name="status")
async def status(ctx):
    org_id = server_org_map.get(str(ctx.guild.id), "unmapped")
    await ctx.send(
        f"🤖 **DEVAD Bot Status**\n"
        f"• Org ID: `{org_id}`\n"
        f"• Watching: {', '.join(WATCH_CHANNELS)}\n"
        f"• Backend: `{BACKEND_URL}`\n"
        f"• Total orgs loaded: {len(server_org_map)}"
    )


if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN:
        log.error("DISCORD_BOT_TOKEN not set!")
    else:
        bot.run(DISCORD_BOT_TOKEN)
