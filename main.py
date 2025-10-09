import discord
from discord.ext import commands
import json
import os
import logging
import time
from datetime import datetime
from cogs.music import Music
from utils.monitoring import BotMonitor

# Set up logging
def setup_logging():
    if not os.path.exists('logs'):
        os.makedirs('logs')
        
    log_file = f'logs/bot_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler()
        ]
    )

class SporkMP3(commands.Bot):
    def __init__(self):
        # Set up minimal intents
        intents = discord.Intents.none()
        intents.guilds = True  # Needed for basic guild operations
        intents.voice_states = True  # Needed for voice functionality
        intents.guild_messages = True  # Needed for message handling
        
        super().__init__(command_prefix="!", intents=intents)
        
        # Store startup time for health monitoring
        self.start_time = time.time()
        
        # Ensure opus is loaded for better audio performance
        try:
            if not discord.opus.is_loaded():
                discord.opus.load_opus('opus')
                logging.info("Opus library loaded successfully")
        except Exception as e:
            logging.warning(f"Could not load opus library: {e}. Using default.")
        
        # Load config
        try:
            with open('config.json', 'r') as f:
                self.config = json.load(f)
        except FileNotFoundError:
            logging.error("config.json not found!")
            raise
        except json.JSONDecodeError:
            logging.error("config.json is invalid!")
            raise

    async def setup_hook(self):
        try:
            # Add the music cog
            await self.add_cog(Music(self))
            
            # Run file validation on startup
            music_cog = self.get_cog('Music')
            if music_cog:
                logging.info("Running startup file validation...")
                orphaned_count = music_cog.db.validate_persistent_files()
                if orphaned_count > 0:
                    logging.info(f"Startup validation completed: {orphaned_count} orphaned entries cleaned")
                else:
                    logging.info("Startup validation completed: All files valid")
            
            # Sync commands
            await self.tree.sync()
            logging.info("Command tree synced successfully")
            
        except Exception as e:
            logging.error(f"Error in setup_hook: {e}")
            raise

    async def on_ready(self):
        logging.info(f'{self.user} is ready!')
        logging.info(f'Serving in {len(self.guilds)} servers')
        
        # Set the bot's activity status
        activity = discord.Game(name="your audio files")
        await self.change_presence(activity=activity)
        logging.info("Set activity status to 'Playing your audio files'")
        
        # Log startup completion time
        startup_time = time.time() - self.start_time
        logging.info(f"Bot startup completed in {startup_time:.2f} seconds")

    async def on_error(self, event_method: str, *args, **kwargs):
        logging.error(f'Error in {event_method}: ', exc_info=True)

    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.CommandNotFound):
            return
        logging.error(f'Command error: {error}')

    async def on_voice_state_update(self, member, before, after):
        """Handle voice state updates for voice connection monitoring"""
        # This will be handled by the music cog, but we can add additional monitoring here
        pass

    async def on_guild_join(self, guild):
        """Log when bot joins a new guild"""
        logging.info(f"Joined new guild: {guild.name} (ID: {guild.id}) with {guild.member_count} members")

    async def on_guild_remove(self, guild):
        """Log when bot leaves a guild and clean up"""
        logging.info(f"Left guild: {guild.name} (ID: {guild.id})")
        
        # Clean up guild data
        try:
            music_cog = self.get_cog('Music')
            if music_cog:
                # Clean up guild state
                if guild.id in music_cog.music_state.guild_states:
                    guild_state = music_cog.music_state.guild_states[guild.id]
                    
                    # Mark all files as inactive and clean up non-permanent tracks
                    for track in guild_state.queue:
                        if track.downloaded_path:
                            music_cog.track_manager.mark_file_inactive(track.downloaded_path)
                        if not track.is_permanent:
                            track.cleanup()
                    
                    # Remove guild state
                    del music_cog.music_state.guild_states[guild.id]
                    
                    logging.info(f"Cleaned up state for guild {guild.id}")
        except Exception as e:
            logging.error(f"Error cleaning up guild {guild.id}: {e}")

def main():
    setup_logging()
    logging.info("Starting SporkMP3 bot...")
    
    # Create required directories
    try:
        with open('config.json', 'r') as f:
            config = json.load(f)
        
        required_dirs = ['temp', 'logs', 'permanent']
        for directory in required_dirs:
            if not os.path.exists(directory):
                os.makedirs(directory)
                logging.info(f"Created directory: {directory}")
    
        # Initialize and run bot
        bot = SporkMP3()
        
        # Add error handling for the main run loop
        try:
            bot.run(config['token'])
        except discord.LoginFailure:
            logging.critical("Invalid bot token provided!")
            raise
        except discord.HTTPException as e:
            logging.critical(f"HTTP error occurred: {e}")
            raise
        except KeyboardInterrupt:
            logging.info("Bot shutdown requested by user")
        except Exception as e:
            logging.critical(f"Unexpected error during bot runtime: {e}")
            raise
            
    except FileNotFoundError:
        logging.critical("config.json file not found!")
        raise
    except json.JSONDecodeError:
        logging.critical("Invalid JSON in config.json!")
        raise
    except KeyError as e:
        logging.critical(f"Missing required config key: {e}")
        raise
    except Exception as e:
        logging.critical(f"Fatal error: {e}")
        raise
    finally:
        logging.info("Bot shutdown complete")

if __name__ == "__main__":
    main()
