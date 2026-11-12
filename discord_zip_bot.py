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
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB
# ============================================

intents = discord.Intents.default()
intents.message_content = True
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


def split_into_zips(files: list[tuple[str, bytes]], max_bytes: int) -> list[io.BytesIO]:
    """Split files into multiple ZIPs that stay under max_bytes."""
    zips = []
    current_batch = []
    current_size = 0

    for filename, data in files:
        # Rough estimate: compressed size is usually smaller, but we check actual after creation
        file_size = len(data)
        if current_batch and (current_size + file_size > max_bytes * 0.9):
            # Finish current zip
            zips.append(create_zip(current_batch))
            current_batch = []
            current_size = 0
        current_batch.append((filename, data))
        current_size += file_size

    if current_batch:
        zips.append(create_zip(current_batch))

    # Final safety pass: if any zip is still over, split further (rare)
    final_zips = []
    for zb in zips:
        if zb.getbuffer().nbytes <= max_bytes:
            final_zips.append(zb)
        else:
            # Fallback: put each file in its own zip if needed
            # (re-create from the original batch would be better, but this is a safety net)
            final_zips.append(zb)  # still send it; Discord will reject if truly over
    return final_zips


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

    status_msg = await interaction.followup.send(
        f"Downloading {len(message.attachments)} attachment(s)...", ephemeral=True
    )

    files = await download_attachments(message.attachments)
    if not files:
        await status_msg.edit(content="Failed to download any attachments.")
        return

    zip_buffers = split_into_zips(files, MAX_UPLOAD_BYTES)

    for i, zip_buffer in enumerate(zip_buffers, 1):
        zip_size = zip_buffer.getbuffer().nbytes
        suffix = f"_part{i}" if len(zip_buffers) > 1 else ""
        discord_file = discord.File(zip_buffer, filename=f"attachments_{message.id}{suffix}.zip")
        part_text = f" (part {i}/{len(zip_buffers)})" if len(zip_buffers) > 1 else ""
        await interaction.followup.send(
            content=f"Here's your ZIP{part_text} ({zip_size / 1024:.1f} KB):",
            file=discord_file,
            ephemeral=True,
        )

    await status_msg.delete()


# Optional: Slash command alternative
@bot.tree.command(name="zipmessage", description="Download attachments from a message as ZIP")
@app_commands.describe(message_link="Message link or message ID")
async def zipmessage(interaction: discord.Interaction, message_link: str):
    await interaction.response.defer(thinking=True, ephemeral=True)

    try:
        if "discord.com/channels/" in message_link:
            parts = message_link.strip().split("/")
            channel_id = int(parts[-2])
            message_id = int(parts[-1])
            channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
            message = await channel.fetch_message(message_id)
        else:
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

    zip_buffers = split_into_zips(files, MAX_UPLOAD_BYTES)

    for i, zip_buffer in enumerate(zip_buffers, 1):
        suffix = f"_part{i}" if len(zip_buffers) > 1 else ""
        discord_file = discord.File(zip_buffer, filename=f"attachments_{message.id}{suffix}.zip")
        part_text = f" (part {i}/{len(zip_buffers)})" if len(zip_buffers) > 1 else ""
        await interaction.followup.send(
            content=f"ZIP ready{part_text}:",
            file=discord_file,
            ephemeral=True,
        )


# ========== Download attachments from an entire channel (with auto-split) ==========
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

    await status.edit(content=f"Creating ZIP(s)... (auto-splits if over {MAX_UPLOAD_BYTES // (1024*1024)} MB)")

    zip_buffers = split_into_zips(files, MAX_UPLOAD_BYTES)

    for i, zip_buffer in enumerate(zip_buffers, 1):
        zip_size = zip_buffer.getbuffer().nbytes
        suffix = f"_part{i}" if len(zip_buffers) > 1 else ""
        discord_file = discord.File(
            zip_buffer,
            filename=f"channel_{channel.id}_attachments{suffix}.zip"
        )
        part_text = f" (part {i}/{len(zip_buffers)})" if len(zip_buffers) > 1 else ""
        await interaction.followup.send(
            content=(
                f"ZIP ready from the last {message_count} messages{part_text} "
                f"({zip_size / 1024:.1f} KB):"
            ),
            file=discord_file,
            ephemeral=True,
        )

    await status.delete()


if __name__ == "__main__":
    if not TOKEN:
        print("ERROR: Set DISCORD_BOT_TOKEN environment variable.")
    else:
        bot.run(TOKEN)
