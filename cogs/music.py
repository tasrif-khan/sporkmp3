import discord
from discord.ext import commands
from discord import app_commands
import logging

# Import permission checks
from utils.permission_checks import check_permissions, admin_only

# Import components
from .music_state import MusicState
from .music_ui import MusicUI
from .music_playback import MusicPlayback
from .music_events import MusicEvents
from .music_commands import MusicCommands

# Import utilities
from utils.track_manager import TrackManager
from utils.database_manager import DatabaseManager
from utils.health_monitor import HealthMonitor

class Music(commands.Cog):
    """Main music bot cog that coordinates all components"""
    def __init__(self, bot):
        self.bot = bot
        
        # Emojis for various states and actions
        self.emoji = {
            'play': '▶️',
            'pause': '⏸️',
            'resume': '⏯️',
            'stop': '⏹️',
            'skip': '⏭️',
            'queue': '📜',
            'music': '🎵',
            'warning': '⚠️',
            'error': '❌',
            'success': '✅',
            'time': '⏰',
            'loop': '🔄',
            'volume': '🔊',
            'low_volume': '🔈',
            'mute': '🔇',
            'disconnect': '👋',
            'loading': '⏳',
            'microphone': '🎙️',
            'cd': '💿',
            'settings' : '⚙️',
            'user': '👤',
            'role': '👥' 
        }
        
        # Initialize components
        self.db = DatabaseManager()
        self.music_state = MusicState()
        self.music_ui = MusicUI(self.emoji)
        self.track_manager = TrackManager(bot.config, bot)
        self.music_playback = MusicPlayback(bot, self.track_manager, self.music_state, self.db, self.music_ui)
        self.music_events = MusicEvents(bot, self.db, self.track_manager, self.music_state, self.music_ui, self.music_playback)
        self.music_commands = MusicCommands(bot, self.db, self.track_manager, self.music_state, self.music_ui, self.music_playback)
        self.health_monitor = HealthMonitor(bot)
        
        # Set up event handlers
        self.bot.loop.create_task(self.music_events.periodic_cleanup())
        self.bot.loop.create_task(self.music_events.track_playback_activity())
        self.bot.loop.create_task(self.health_monitor.monitor_voice_connections())
        
        logging.info("Music extension loaded successfully")
    
    async def cog_load(self):
        """Called when the cog is loaded"""
        logging.info("Initializing music cog...")
        try:
            await self.track_manager.ensure_temp_folder()
            await self.track_manager.cleanup_temp_files()
            logging.info("Music cog initialization completed")
        except Exception as e:
            logging.error(f"Error during cog initialization: {e}")
    
    async def cog_unload(self):
        """Called when the cog is unloaded"""
        logging.info("Cleaning up music cog resources...")
        try:
            # Disconnect from all voice clients
            for guild in self.bot.guilds:
                if guild.voice_client:
                    await guild.voice_client.disconnect()
            
            # Cleanup all tracks
            for guild_id, state in self.music_state.guild_states.items():
                for track in state.queue:
                    if not track.is_permanent:
                        track.cleanup()
            
            # Final cleanup of temp files
            await self.track_manager.cleanup_temp_files()
            logging.info("Music cog cleanup completed")
        except Exception as e:
            logging.error(f"Error during cog cleanup: {e}")
    
    # Helper method to update last channel ID
    async def update_last_channel(self, interaction):
        guild_state = await self.music_state.get_guild_state(interaction.guild_id)
        guild_state.last_channel_id = interaction.channel_id
    
    # Event handlers
    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        await self.music_events.on_voice_state_update(member, before, after)
    
    @commands.Cog.listener()
    async def on_message(self, message):
        if not message.author.bot and message.guild:
            # Update the last channel ID when the bot is mentioned
            if f'<@{self.bot.user.id}>' in message.content and message.attachments:
                guild_state = await self.music_state.get_guild_state(message.guild.id)
                guild_state.last_channel_id = message.channel.id
        
        await self.music_events.on_message(message)
    
    # Command registration - directly implement the callback functions that call music_commands methods
    
    # Admin commands
    @app_commands.command(name="blacklist", description="Add or remove a user from the blacklist")
    @admin_only()
    async def blacklist(self, interaction: discord.Interaction, action: str, user: discord.Member):
        await self.update_last_channel(interaction)
        await self.music_commands.blacklist(interaction, action, user)

    @app_commands.command(name="role_config", description="Add or remove a role from the whitelist")
    @admin_only()
    async def role_config(self, interaction: discord.Interaction, action: str, role: discord.Role):
        await self.update_last_channel(interaction)
        await self.music_commands.role_config(interaction, action, role)

    @app_commands.command(name="autodisconnect", description="Enable or disable auto-disconnect when queue is empty")
    @admin_only()
    async def autodisconnect(self, interaction: discord.Interaction, enabled: bool):
        await self.update_last_channel(interaction)
        await self.music_commands.autodisconnect(interaction, enabled)

    @app_commands.command(name="autoplay", description="Enable or disable autoplay")
    @admin_only()
    async def autoplay(self, interaction: discord.Interaction, enabled: bool):
        await self.update_last_channel(interaction)
        await self.music_commands.autoplay(interaction, enabled)

    @app_commands.command(name="health", description="Check bot health status")
    @admin_only()
    async def health(self, interaction: discord.Interaction):
        await self.update_last_channel(interaction)
        await self.music_commands.health(interaction)
    
    # User commands
    @app_commands.command(name="play", description="Play a track from queue or from the sound library")
    @app_commands.describe(name="Optional: Name of the sound from the library to play")
    @check_permissions()
    async def play(self, interaction: discord.Interaction, name: str = None):
        await self.update_last_channel(interaction)
        await self.music_commands.play(interaction, name)
    
    @app_commands.command(name="pause", description="Pause the current song")
    @check_permissions()
    async def pause(self, interaction: discord.Interaction):
        await self.update_last_channel(interaction)
        await self.music_commands.pause(interaction)
    
    @app_commands.command(name="resume", description="Resume the current song")
    @check_permissions()
    async def resume(self, interaction: discord.Interaction):
        await self.update_last_channel(interaction)
        await self.music_commands.resume(interaction)
    
    @app_commands.command(name="queue", description="Show the current queue")
    @check_permissions()
    async def queue(self, interaction: discord.Interaction):
        await self.update_last_channel(interaction)
        await self.music_commands.queue(interaction)
    
    @app_commands.command(name="seekqueue", description="Jump to a specific position in the queue")
    @app_commands.describe(position="Queue position to jump to (1 = first track)")
    @check_permissions()
    async def seekqueue(self, interaction: discord.Interaction, position: int):
        await self.update_last_channel(interaction)
        await self.music_commands.seekqueue(interaction, position)
    
    @app_commands.command(name="playing", description="Show what's currently playing")
    @check_permissions()
    async def playing(self, interaction: discord.Interaction):
        await self.update_last_channel(interaction)
        await self.music_commands.playing(interaction)
    
    @app_commands.command(name="volume", description="Set the volume (0-120)")
    @check_permissions()
    async def volume(self, interaction: discord.Interaction, volume: int):
        await self.update_last_channel(interaction)
        await self.music_commands.volume(interaction, volume)
    
    @app_commands.command(name="speed", description="Set the playback speed (50-200%)")
    @check_permissions()
    async def speed(self, interaction: discord.Interaction, speed: int):
        await self.update_last_channel(interaction)
        await self.music_commands.speed(interaction, speed)
    
    @app_commands.command(name="forward", description="Skip forward by specified seconds")
    @check_permissions()
    async def forward(self, interaction: discord.Interaction, seconds: int):
        await self.update_last_channel(interaction)
        await self.music_commands.forward(interaction, seconds)
    
    @app_commands.command(name="backward", description="Skip backward by specified seconds")
    @check_permissions()
    async def backward(self, interaction: discord.Interaction, seconds: int):
        await self.update_last_channel(interaction)
        await self.music_commands.backward(interaction, seconds)
    
    @app_commands.command(name="timestamp", description="Set the current song position (hh:mm:ss)")
    @check_permissions()
    async def timestamp(self, interaction: discord.Interaction, hours: int, minutes: int, seconds: int):
        await self.update_last_channel(interaction)
        await self.music_commands.timestamp(interaction, hours, minutes, seconds)
    
    @app_commands.command(name="skip", description="Skip the current song")
    @check_permissions()
    async def skip(self, interaction: discord.Interaction):
        await self.update_last_channel(interaction)
        await self.music_commands.skip(interaction)
    
    @app_commands.command(name="clear", description="Clear the entire queue and stop playback")
    @check_permissions()
    async def clear(self, interaction: discord.Interaction):
        await self.update_last_channel(interaction)
        await self.music_commands.clear(interaction)
    
    @app_commands.command(name="stop", description="Stop the current playback")
    @check_permissions()
    async def stop(self, interaction: discord.Interaction):
        await self.update_last_channel(interaction)
        await self.music_commands.stop(interaction)
    
    @app_commands.command(name="disconnect", description="Disconnect the bot from voice")
    @check_permissions()
    async def disconnect(self, interaction: discord.Interaction):
        await self.update_last_channel(interaction)
        await self.music_commands.disconnect(interaction)
    
    @app_commands.command(name="loop", description="Toggle loop mode for the current track")
    @app_commands.describe(times="Number of times to loop (optional, leave empty for infinite)")
    @check_permissions()
    async def loop(self, interaction: discord.Interaction, times: int = None):
        await self.update_last_channel(interaction)
        await self.music_commands.loop(interaction, times)
    
    @app_commands.command(name="remove", description="Remove a specific song from the queue by its position number")
    @check_permissions()
    async def remove(self, interaction: discord.Interaction, position: int):
        await self.update_last_channel(interaction)
        await self.music_commands.remove(interaction, position)
    
    @app_commands.command(name="upload", description="Upload a sound to the server's permanent library")
    @check_permissions()
    async def upload(self, interaction: discord.Interaction, name: str, file: discord.Attachment = None):
        await self.update_last_channel(interaction)
        await self.music_commands.upload(interaction, name, file)

    @app_commands.command(name="library", description="List all sounds in the server's sound library")
    @check_permissions()
    async def library(self, interaction: discord.Interaction):
        await self.update_last_channel(interaction)
        await self.music_commands.library(interaction)
    
    @app_commands.command(name="remove_sound", description="Remove a sound from the server's library")
    @check_permissions()
    async def remove_sound(self, interaction: discord.Interaction, name: str):
        await self.update_last_channel(interaction)
        await self.music_commands.remove_sound(interaction, name)
    
    @app_commands.command(name="help", description="Show all available commands")
    async def help(self, interaction: discord.Interaction):
        await self.update_last_channel(interaction)
        await self.music_commands.help(interaction)

def setup(bot):
    bot.add_cog(Music(bot))