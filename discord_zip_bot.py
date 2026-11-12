import discord
from discord import app_commands
from discord.ext import commands
import io
import zipfile
import aiohttp
import asyncio
import os
from typing import Optional

# ================== CONFIG ==================
# Put your bot token here or use an environment variable
TOKEN = os.getenv("DISCORD_BOT_TOKEN")

# Maximum ZIP size Discord will accept (25 MB free / higher with boosts)
# The bot will refuse to upload if the zip exceeds this
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB
# ============================================

intents = discord.Intents.default()
intents.message_content = True  # needed to read message content if desired
intents.messages = True

bot = commands.Bot(command_prefix="!", intents=intents)


async def download_attachments(attachments: list[discord.Attachment]) -> list[tuple[str, bytes]]:
    """Download all attachments and return list of (filename, data)."""
    files = []
    async with aiohttp.ClientSession() as session:
        for att in attachments:
            try:
                async with session.get(att.url) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        # Avoid filename collisions
                        filename = att.filename
                        base, ext = os.path.splitext(filename)
                        counter = 1
                        while any(f[0] == filename for f in files):
                            filename = f"{base}_{counter}{ext}"
                            counter += 1
                        files.append((filename, data))
                    else:
                        print(f"Failed to download {att.filename}: HTTP {resp.status}")
            except Exception as e:
                print(f"Error downloading {att.filename}: {e}")
    return files


def create_zip(files: list[tuple[str, bytes]]) -> io.BytesIO:
    """Create an in-memory ZIP from (filename, data) pairs."""
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for filename, data in files:
            zf.writestr(filename, data)
    zip_buffer.seek(0)
    return zip_buffer


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"Failed to sync commands: {e}")


# ========== Context Menu: Right-click message → Download as ZIP ==========
@bot.tree.context_menu(name="Download as ZIP")
async def download_as_zip(interaction: discord.Interaction, message: discord.Message):
    await interaction.response.defer(thinking=True, ephemeral=True)

    if not message.attachments:
        await interaction.followup.send("This message has no attachments.", ephemeral=True)
        return

    # Download
    status_msg = await interaction.followup.send(
        f"Downloading {len(message.attachments)} attachment(s)...", ephemeral=True
    )

    files = await download_attachments(message.attachments)

    if not files:
        await status_msg.edit(content="Failed to download any attachments.")
        return

    # Zip
    zip_buffer = create_zip(files)
    zip_size = zip_buffer.getbuffer().nbytes

    if zip_size > MAX_UPLOAD_BYTES:
        await status_msg.edit(
            content=(
                f"ZIP is too large ({zip_size / (1024*1024):.1f} MB). "
                f"Discord limit is ~{MAX_UPLOAD_BYTES / (1024*1024):.0f} MB.\n"
                "Consider downloading fewer images or using a higher-boost server."
            )
        )
        return

    # Prepare filename
    zip_name = f"attachments_{message.id}.zip"

    discord_file = discord.File(zip_buffer, filename=zip_name)

    # Send the zip (ephemeral so only the user sees it)
    await interaction.followup.send(
        content=f"Here's your ZIP ({len(files)} file(s), {zip_size / 1024:.1f} KB):",
        file=discord_file,
        ephemeral=True,
    )
    await status_msg.delete()


# Optional: Slash command alternative (paste message link or ID)
@bot.tree.command(name="zipmessage", description="Download attachments from a message as ZIP")
@app_commands.describe(message_link="Message link or message ID")
async def zipmessage(interaction: discord.Interaction, message_link: str):
    await interaction.response.defer(thinking=True, ephemeral=True)

    try:
        # Support full Discord message links or raw IDs
        if "discord.com/channels/" in message_link:
            parts = message_link.strip().split("/")
            channel_id = int(parts[-2])
            message_id = int(parts[-1])
            channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
            message = await channel.fetch_message(message_id)
        else:
            # Assume it's a message ID in the current channel
            message = await interaction.channel.fetch_message(int(message_link))
    except Exception as e:
        await interaction.followup.send(f"Could not find that message: {e}", ephemeral=True)
        return

    if not message.attachments:
        await interaction.followup.send("That message has no attachments.", ephemeral=True)
        return

    files = await download_attachments(message.attachments)
    if not files:
        await interaction.followup.send("Failed to download attachments.", ephemeral=True)
        return

    zip_buffer = create_zip(files)
    zip_size = zip_buffer.getbuffer().nbytes

    if zip_size > MAX_UPLOAD_BYTES:
        await interaction.followup.send(
            f"ZIP too large ({zip_size / (1024*1024):.1f} MB). Limit ~{MAX_UPLOAD_BYTES/(1024*1024):.0f} MB.",
            ephemeral=True,
        )
        return

    discord_file = discord.File(zip_buffer, filename=f"attachments_{message.id}.zip")
    await interaction.followup.send(
        content=f"ZIP ready ({len(files)} files):",
        file=discord_file,
        ephemeral=True,
    )


# ========== New: Download attachments from an entire channel ==========
@bot.tree.command(name="zipchannel", description="Download attachments from recent messages in a channel")
@app_commands.describe(
    limit="How many recent messages to scan (max 1000)",
    images_only="Only include image files (default: true)"
)
async def zipchannel(
    interaction: discord.Interaction,
    limit: app_commands.Range[int, 1, 1000] = 100,
    images_only: bool = True
):
    await interaction.response.defer(thinking=True, ephemeral=True)

    channel = interaction.channel
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        await interaction.followup.send("This command only works in text channels or threads.", ephemeral=True)
        return

    status = await interaction.followup.send(
        f"Scanning the last {limit} messages for attachments...", ephemeral=True
    )

    all_attachments = []
    message_count = 0

    try:
        async for message in channel.history(limit=limit):
            message_count += 1
            if message.attachments:
                for att in message.attachments:
                    if images_only and not (att.content_type and att.content_type.startswith("image/")):
                        continue
                    all_attachments.append(att)
    except Exception as e:
        await status.edit(content=f"Error reading channel history: {e}")
        return

    if not all_attachments:
        await status.edit(content=f"No attachments found in the last {message_count} messages.")
        return

    await status.edit(content=f"Found {len(all_attachments)} attachment(s). Downloading...")

    files = await download_attachments(all_attachments)
    if not files:
        await status.edit(content="Failed to download any attachments.")
        return

    zip_buffer = create_zip(files)
    zip_size = zip_buffer.getbuffer().nbytes

    if zip_size > MAX_UPLOAD_BYTES:
        await status.edit(
            content=(
                f"ZIP is too large ({zip_size / (1024*1024):.1f} MB).\n"
                f"Discord upload limit is ~{MAX_UPLOAD_BYTES / (1024*1024):.0f} MB.\n"
                "Try a smaller `limit` or set `images_only` to true."
            )
        )
        return

    discord_file = discord.File(
        zip_buffer,
        filename=f"channel_{channel.id}_attachments.zip"
    )

    await interaction.followup.send(
        content=(
            f"ZIP ready from the last {message_count} messages "
            f"({len(files)} files, {zip_size / 1024:.1f} KB):"
        ),
        file=discord_file,
        ephemeral=True,
    )
    await status.delete()


if __name__ == "__main__":
    if TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("ERROR: Set your bot token in the script or via DISCORD_BOT_TOKEN env var.")
    else:
        bot.run(TOKEN)
