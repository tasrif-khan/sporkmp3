"""
Utility classes for the music bot.
Combines track management, UI, voice handling, permissions, and monitoring.
"""
import discord
from discord import app_commands
import os
import time
import asyncio
import logging
from functools import wraps
from typing import Set, Optional, List

# ============================================================================
# COLOR SCHEME
# ============================================================================
class Colors:
    """Color palette for embeds"""
    PRIMARY = 0x3e4566    # Dark blue-gray - Main/Playing/Info
    ACCENT = 0xff914d     # Orange - Success actions
    WARNING = 0xffc300    # Amber - Warnings
    ERROR = 0xf50b17      # Red - Errors only


# ============================================================================
# EMOJIS - Centralized emoji definitions
# ============================================================================
EMOJI = {
    'play':       '▶',   # U+25B6  BLACK RIGHT-POINTING TRIANGLE
    'pause':      '‖',   # U+2016  DOUBLE VERTICAL LINE
    'resume':     '▶',
    'stop':       '■',   # U+25A0  BLACK SQUARE
    'skip':       '▶▶',
    'queue':      '≡',   # U+2261  IDENTICAL TO (triple bar)
    'music':      '♪',   # U+266A  EIGHTH NOTE
    'warning':    '▲',   # U+25B2  BLACK UP-POINTING TRIANGLE
    'error':      '✕',   # U+2715  MULTIPLICATION X
    'success':    '✓',   # U+2713  CHECK MARK
    'time':       '◷',   # U+25F7  WHITE CIRCLE WITH UPPER RIGHT QUADRANT
    'loop':       '↻',   # U+21BB  CLOCKWISE OPEN CIRCLE ARROW
    'volume':     '◆',   # U+25C6  BLACK DIAMOND
    'mute':       '◇',   # U+25C7  WHITE DIAMOND
    'disconnect': '←',   # U+2190  LEFTWARDS ARROW
    'loading':    '…',   # U+2026  HORIZONTAL ELLIPSIS
    'microphone': '◎',   # U+25CE  BULLSEYE
    'cd':         '◆',
    'settings':   '◈',   # U+25C8  WHITE DIAMOND CONTAINING BLACK SMALL DIAMOND
    'user':       '●',   # U+25CF  BLACK CIRCLE
    'role':       '○',   # U+25CB  WHITE CIRCLE
    'info':       '·',   # U+00B7  MIDDLE DOT
    'fast':       '»',   # U+00BB  RIGHT-POINTING DOUBLE ANGLE QUOTATION MARK
    'slow':       '«',   # U+00AB  LEFT-POINTING DOUBLE ANGLE QUOTATION MARK
    'bar':        '━',   # U+2501  BOX DRAWINGS HEAVY HORIZONTAL
}


# ============================================================================
# UI HELPERS
# ============================================================================
def create_embed(title: str = None, description: str = None, color: int = None) -> discord.Embed:
    """Create a consistent embed with cleaner styling"""
    if color is None:
        color = Colors.PRIMARY
    
    embed = discord.Embed(color=color)
    
    if title:
        embed.title = title
    if description:
        embed.description = description
    
    embed.set_footer(text="SporkMP3")
    embed.timestamp = discord.utils.utcnow()
    
    return embed


def format_duration(seconds: float) -> str:
    """Format seconds as MM:SS or HH:MM:SS"""
    seconds = int(seconds)
    if seconds < 3600:
        return f"{seconds // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


def progress_bar(position: float, duration: float, length: int = 15) -> str:
    """Create a visual progress bar with better styling"""
    if duration <= 0:
        return f"`{EMOJI['bar'] * length}`"
    
    filled = int((position / duration) * length)
    empty = length - filled
    
    bar = '━' * filled + '╸' if filled < length else '━' * length
    bar += '─' * (empty - 1) if empty > 0 else ''
    
    return f"`{bar}`"


def error_embed(message: str, title: str = "Error") -> discord.Embed:
    """Quick error embed with red color"""
    embed = create_embed(color=Colors.ERROR)
    embed.title = f"{EMOJI['error']} {title}"
    embed.description = message
    return embed


def warning_embed(message: str, title: str = "Warning") -> discord.Embed:
    """Quick warning embed with amber color"""
    embed = create_embed(color=Colors.WARNING)
    embed.title = f"{EMOJI['warning']} {title}"
    embed.description = message
    return embed


def success_embed(title: str, message: str) -> discord.Embed:
    """Quick success/action embed with orange color"""
    embed = create_embed(color=Colors.ACCENT)
    embed.title = f"{EMOJI['success']} {title}"
    embed.description = message
    return embed


def info_embed(title: str, message: str) -> discord.Embed:
    """Quick info embed with primary color"""
    embed = create_embed(color=Colors.PRIMARY)
    embed.title = title
    embed.description = message
    return embed


# ============================================================================
# PERMISSION DECORATORS
# ============================================================================
def check_permissions():
    """Check if user can use the bot (not blacklisted, has required role)"""
    async def predicate(interaction: discord.Interaction) -> bool:
        music_cog = interaction.client.get_cog('Music')
        if not music_cog:
            return False
        
        # Admins bypass all checks
        if interaction.user.guild_permissions.administrator:
            return True
        
        # Check blacklist
        if music_cog.db.is_blacklisted(interaction.guild_id, interaction.user.id):
            await interaction.response.send_message(
                embed=error_embed("You are blacklisted from using this bot."),
                ephemeral=True
            )
            return False
        
        # Check role whitelist
        whitelisted = music_cog.db.get_whitelisted_roles(interaction.guild_id)
        if whitelisted:
            user_roles = {r.id for r in interaction.user.roles}
            if not user_roles & set(whitelisted):
                await interaction.response.send_message(
                    embed=error_embed("You don't have the required role."),
                    ephemeral=True
                )
                return False
        
        return True
    return app_commands.check(predicate)


def admin_only():
    """Require administrator permissions"""
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                embed=error_embed("This command requires administrator privileges."),
                ephemeral=True
            )
            return False
        return True
    return app_commands.check(predicate)


def safe_defer(func):
    """Decorator to safely defer interactions and handle errors"""
    @wraps(func)
    async def wrapper(self, interaction: discord.Interaction, *args, **kwargs):
        try:
            if not interaction.response.is_done():
                await interaction.response.defer()
            return await func(self, interaction, *args, **kwargs)
        except discord.NotFound:
            logging.warning(f"Interaction expired for {func.__name__}")
        except discord.HTTPException as e:
            logging.error(f"HTTP error in {func.__name__}: {e}")
        except Exception as e:
            logging.error(f"Error in {func.__name__}: {e}")
            try:
                embed = error_embed("An unexpected error occurred.")
                if interaction.response.is_done():
                    await interaction.followup.send(embed=embed, ephemeral=True)
                else:
                    await interaction.response.send_message(embed=embed, ephemeral=True)
            except:
                pass
    return wrapper


# ============================================================================
# TRACK MANAGER
# ============================================================================
class TrackManager:
    """Manages audio file downloads, cleanup, and resource limits"""
    
    def __init__(self, config: dict):
        self.temp_folder = config['temp_folder']
        self.max_queue_size = config['max_queue_size_mb'] * 1024 * 1024
        self.max_tracks = config['resource_limits']['max_tracks_per_guild']
        self.max_duration = config['resource_limits']['max_track_duration_minutes'] * 60
        self.cleanup_interval = config['resource_limits']['cleanup_interval_minutes'] * 60
        self.file_max_age = config['resource_limits']['inactive_timeout_minutes'] * 60
        
        self._active_files: Set[str] = set()
        self._last_cleanup = time.time()
        
        os.makedirs(self.temp_folder, exist_ok=True)
    
    def mark_active(self, path: str):
        if path:
            self._active_files.add(path)
    
    def mark_inactive(self, path: str):
        if path:
            self._active_files.discard(path)
    
    def get_queue_size(self, queue: list) -> int:
        return sum(t.file_size for t in queue)
    
    def can_add(self, queue: list, file_size: int) -> bool:
        """Check if file can be added to queue"""
        if self.get_queue_size(queue) + file_size > self.max_queue_size:
            return False
        return len(queue) < self.max_tracks
    
    def validate_duration(self, duration: float) -> bool:
        return duration <= self.max_duration
    
    async def cleanup_temp_files(self):
        """Remove old temporary files"""
        now = time.time()
        if now - self._last_cleanup < self.cleanup_interval:
            return
        
        self._last_cleanup = now
        cleaned = 0
        
        if not os.path.exists(self.temp_folder):
            return
        
        for filename in os.listdir(self.temp_folder):
            filepath = os.path.join(self.temp_folder, filename)
            if filepath in self._active_files:
                continue
            
            try:
                if now - os.path.getctime(filepath) > self.file_max_age:
                    os.remove(filepath)
                    cleaned += 1
            except Exception as e:
                logging.error(f"Cleanup error for {filename}: {e}")
        
        if cleaned:
            logging.info(f"Cleaned up {cleaned} temp files")
    
    async def ensure_temp_folder(self):
        """Ensure temp folder exists and is writable"""
        os.makedirs(self.temp_folder, exist_ok=True)
        test_file = os.path.join(self.temp_folder, '.write_test')
        try:
            with open(test_file, 'w') as f:
                f.write('test')
            os.remove(test_file)
        except Exception as e:
            logging.error(f"Temp folder not writable: {e}")
            raise


# ============================================================================
# VOICE CONNECTION HANDLER
# ============================================================================
class VoiceHandler:
    """Handles voice connections with retry logic"""
    
    def __init__(self):
        self.last_attempt: dict = {}
        self.failure_count: dict = {}
    
    async def connect(self, channel: discord.VoiceChannel, max_retries: int = 3) -> Optional[discord.VoiceClient]:
        """Connect to voice channel with retries"""
        guild_id = channel.guild.id
        
        # Rate limit check
        now = time.time()
        if guild_id in self.last_attempt and now - self.last_attempt[guild_id] < 30:
            logging.warning(f"Voice connection rate limited for guild {guild_id}")
            return None
        
        self.last_attempt[guild_id] = now
        
        for attempt in range(max_retries):
            try:
                # Disconnect existing connection
                if channel.guild.voice_client:
                    await channel.guild.voice_client.disconnect(force=True)
                    await asyncio.sleep(1)
                
                # Check permissions
                perms = channel.permissions_for(channel.guild.me)
                if not perms.connect or not perms.speak:
                    logging.error(f"Missing voice permissions in {channel.name}")
                    return None
                
                # Connect
                voice_client = await channel.connect(timeout=30.0, reconnect=False)
                self.failure_count.pop(guild_id, None)
                logging.info(f"Connected to voice: {channel.name}")
                return voice_client
                
            except discord.errors.ConnectionClosed as e:
                if e.code == 4006:
                    logging.warning(f"Session invalidated, retrying... (attempt {attempt + 1})")
                    await asyncio.sleep(2 ** attempt)
                    continue
                break
            except discord.ClientException as e:
                if "already connected" in str(e).lower():
                    return channel.guild.voice_client
                break
            except asyncio.TimeoutError:
                logging.warning(f"Voice connection timeout (attempt {attempt + 1})")
                await asyncio.sleep(2 ** attempt)
                continue
            except Exception as e:
                logging.error(f"Voice connection error: {e}")
                await asyncio.sleep(2 ** attempt)
                continue
        
        self.failure_count[guild_id] = self.failure_count.get(guild_id, 0) + 1
        return None


# ============================================================================
# HEALTH MONITOR
# ============================================================================
class HealthMonitor:
    """Simple health monitoring for the bot"""
    
    def __init__(self, bot):
        self.bot = bot
        self.voice_failures: dict = {}
    
    def log_failure(self, guild_id: int, error_code: int):
        """Log a voice connection failure"""
        if guild_id not in self.voice_failures:
            self.voice_failures[guild_id] = []
        
        self.voice_failures[guild_id].append({
            'time': time.time(),
            'code': error_code
        })
        
        # Keep only last 10
        self.voice_failures[guild_id] = self.voice_failures[guild_id][-10:]
    
    async def get_stats(self) -> dict:
        """Get bot health statistics"""
        connected = len([g for g in self.bot.guilds if g.voice_client])
        uptime = (time.time() - self.bot.start_time) / 3600 if hasattr(self.bot, 'start_time') else 0
        
        recent_failures = sum(
            len([f for f in fails if time.time() - f['time'] < 3600])
            for fails in self.voice_failures.values()
        )
        
        return {
            'guilds': len(self.bot.guilds),
            'voice_connections': connected,
            'recent_failures': recent_failures,
            'uptime_hours': uptime
        }
    
    async def monitor_loop(self):
        """Background task to monitor voice connections"""
        while True:
            try:
                for guild in self.bot.guilds:
                    vc = guild.voice_client
                    if vc and not vc.is_connected():
                        logging.warning(f"Unhealthy voice connection in guild {guild.id}")
                await asyncio.sleep(300)
            except Exception as e:
                logging.error(f"Health monitor error: {e}")
                await asyncio.sleep(60)
