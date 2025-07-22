import discord
from discord.ext import commands
from discord import app_commands
import yt_dlp
import os
import asyncio
import subprocess
import re
import requests
import json
from datetime import datetime
import base64

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

MAX_FILE_SIZE = 8 * 1024 * 1024  # 8 MB
MAX_VIDEO_LENGTH_SECONDS = 25 * 60  # 25 minutes

intents = discord.Intents.default()
intents.messages = True
intents.dm_messages = True

bot = commands.Bot(command_prefix="!", intents=intents)

video_cooldown = commands.CooldownMapping.from_cooldown(1, 120, commands.BucketType.user)

# --- UPDATED GITHUB REPO ---
GITHUB_REPO = "YTcord/YTcord-logs"
GITHUB_FILE_PATH = "logs.json"
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE_PATH}"
# ---------------------------

def compress_video(input_path: str, output_path: str, target_size=MAX_FILE_SIZE):
    result = subprocess.run(
        ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
         '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', input_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT
    )
    duration_str = result.stdout.decode().strip()
    try:
        duration = float(duration_str)
    except:
        duration = 60

    audio_bitrate = 128
    target_bitrate = ((target_size * 8) / duration) / 1000 - audio_bitrate
    if target_bitrate < 100:
        target_bitrate = 100

    passlogfile = "ffmpeg2pass"
    command_pass1 = [
        'ffmpeg', '-y', '-i', input_path,
        '-c:v', 'libx264',
        '-b:v', f'{int(target_bitrate)}k',
        '-pass', '1',
        '-an',
        '-f', 'mp4',
        '/dev/null' if os.name != 'nt' else 'NUL'
    ]
    command_pass2 = [
        'ffmpeg', '-y', '-i', input_path,
        '-c:v', 'libx264',
        '-b:v', f'{int(target_bitrate)}k',
        '-pass', '2',
        '-c:a', 'aac',
        '-b:a', f'{audio_bitrate}k',
        output_path
    ]

    subprocess.run(command_pass1, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(command_pass2, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    for ext in ['.log', '.log.mbtree']:
        try:
            os.remove(f"{passlogfile}{ext}")
        except FileNotFoundError:
            pass

def is_youtube_url(url: str) -> bool:
    pattern = r"^(https?://)?(www\.)?(youtube\.com|youtu\.be)/"
    return re.match(pattern, url) is not None

def github_log_user(user_id: int, username: str):
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }

    r = requests.get(GITHUB_API_URL, headers=headers)
    if r.status_code != 200:
        print(f"Failed to fetch logs.json from GitHub: {r.status_code}")
        return

    data = r.json()
    sha = data["sha"]
    content = data["content"]
    encoding = data["encoding"]

    if encoding != "base64":
        print("Unexpected encoding for logs.json")
        return

    decoded = base64.b64decode(content).decode()
    try:
        logs = json.loads(decoded)
    except Exception:
        logs = []

    logs.append({
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "user_id": user_id,
        "username": username
    })

    new_content = base64.b64encode(json.dumps(logs, indent=2).encode()).decode()

    commit_msg = f"Log usage by {username} ({user_id})"

    payload = {
        "message": commit_msg,
        "content": new_content,
        "sha": sha,
        "branch": "main"
    }

    put_r = requests.put(GITHUB_API_URL, headers=headers, data=json.dumps(payload))
    if put_r.status_code not in [200, 201]:
        print(f"Failed to update logs.json: {put_r.status_code} {put_r.text}")

class YTcord(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def get_cooldown_bucket(self, interaction):
        return video_cooldown.get_bucket(interaction)

    @app_commands.command(name="video", description="Download a YouTube video and send a copyable link")
    async def video(self, interaction: discord.Interaction, url: str):
        await interaction.response.defer(ephemeral=True)

        bucket = self.get_cooldown_bucket(interaction)
        retry_after = bucket.update_rate_limit()
        if retry_after:
            await interaction.followup.send(f"⏳ Please wait {int(retry_after)} seconds before using this command again.", ephemeral=True)
            return

        if not is_youtube_url(url):
            await interaction.followup.send("❌ The URL must be a valid YouTube link.", ephemeral=True)
            return

        os.makedirs("downloads", exist_ok=True)

        raw_filename = None
        compressed_filename = None

        try:
            ydl_opts_info = {'quiet': True, 'skip_download': True}
            with yt_dlp.YoutubeDL(ydl_opts_info) as ydl:
                info = ydl.extract_info(url, download=False)

            duration = info.get('duration', 0)
            if duration > MAX_VIDEO_LENGTH_SECONDS:
                await interaction.followup.send(f"❌ Video is too long! Max length allowed is 25 minutes.", ephemeral=True)
                return

            title = info.get('title', 'video').replace('/', '_')
            raw_filename = f"downloads/raw_{title}.mp4"
            compressed_filename = f"downloads/Made with YTcord '{title}'.mp4"

            ydl_opts = {
                'outtmpl': raw_filename,
                'format': 'mp4',
                'quiet': True
            }

            await interaction.followup.send("⬇️ Downloading video...", ephemeral=True)
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(ydl_opts).download([url]))

            raw_size = os.path.getsize(raw_filename)

            if raw_size > MAX_FILE_SIZE:
                await interaction.followup.send("⚙️ Compressing video to fit Discord size limit...", ephemeral=True)
                await loop.run_in_executor(None, compress_video, raw_filename, compressed_filename)
            else:
                os.rename(raw_filename, compressed_filename)

            final_path = compressed_filename

            user = interaction.user
            try:
                file_msg = await user.send(file=discord.File(final_path))
            except discord.Forbidden:
                await interaction.followup.send(
                    "❌ I can't DM you. Please enable DMs from server members and try again.\n"
                    "Here's a tutorial to help you enable DMs: https://www.youtube.com/watch?v=2Yol7mcvVSQ",
                    ephemeral=True
                )
                return

            # Log usage on GitHub
            try:
                github_log_user(user.id, str(user))
            except Exception as e:
                print(f"Failed to log user to GitHub: {e}")

            cdn_link = file_msg.attachments[0].url

            dm_view = discord.ui.View()
            dm_view.add_item(
                discord.ui.Button(label="Copy Link", style=discord.ButtonStyle.link, url=cdn_link)
            )
            await user.send(f"Here is your video link:\n{cdn_link}", view=dm_view)

            channel_view = discord.ui.View()
            channel_view.add_item(
                discord.ui.Button(label="Open DM", style=discord.ButtonStyle.link, url=f"https://discord.com/channels/@me/{user.id}")
            )
            await interaction.followup.send(
                "📬 I sent you the video link in your DMs!",
                view=channel_view,
                ephemeral=True
            )

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

        finally:
            for f in [raw_filename, compressed_filename]:
                if f and os.path.exists(f):
                    os.remove(f)

async def setup(bot):
    await bot.add_cog(YTcord(bot))

@bot.event
async def on_ready():
    print(f"YTcord is online as {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(e)

async def main():
    async with bot:
        await setup(bot)
        await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
