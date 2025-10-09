import discord
import asyncio
import logging
import time

class VoiceConnectionHandler:
    """Enhanced voice connection handling with better error recovery"""
    
    def __init__(self, bot):
        self.bot = bot
        self.connection_attempts = {}  # Track failed attempts per guild
        self.last_attempt_time = {}    # Rate limiting
        
    async def connect_with_retry(self, voice_channel, max_retries=3, backoff_multiplier=2):
        """Enhanced connection method with exponential backoff"""
        guild_id = voice_channel.guild.id
        
        # Rate limiting - don't retry too frequently
        current_time = time.time()
        if guild_id in self.last_attempt_time:
            time_since_last = current_time - self.last_attempt_time[guild_id]
            if time_since_last < 30:  # Wait 30 seconds between retry sequences
                logging.warning(f"Rate limiting voice connection attempts for guild {guild_id}")
                return None
        
        self.last_attempt_time[guild_id] = current_time
        
        for attempt in range(max_retries):
            try:
                # Clear any existing connection first
                if voice_channel.guild.voice_client:
                    await voice_channel.guild.voice_client.disconnect(force=True)
                    await asyncio.sleep(1)
                
                # Check permissions before attempting connection
                permissions = voice_channel.permissions_for(voice_channel.guild.me)
                if not permissions.connect:
                    logging.error(f"Missing CONNECT permission in {voice_channel.name}")
                    raise discord.Forbidden(discord.HTTPException(), "Missing CONNECT permission")
                if not permissions.speak:
                    logging.error(f"Missing SPEAK permission in {voice_channel.name}")
                    raise discord.Forbidden(discord.HTTPException(), "Missing SPEAK permission")
                
                # Attempt connection
                logging.info(f"Attempting voice connection to {voice_channel.name} (attempt {attempt + 1})")
                voice_client = await voice_channel.connect(timeout=30.0, reconnect=False)
                
                # Reset failure counter on success
                self.connection_attempts.pop(guild_id, None)
                logging.info(f"Successfully connected to voice channel: {voice_channel.name}")
                return voice_client
                
            except discord.errors.ConnectionClosed as e:
                if e.code == 4006:  # Session no longer valid
                    logging.warning(f"Voice session invalidated (4006) for guild {guild_id}, attempt {attempt + 1}")
                    await asyncio.sleep(backoff_multiplier ** attempt)  # Exponential backoff
                    continue
                else:
                    logging.error(f"Voice connection closed with code {e.code}: {e}")
                    break
                    
            except discord.ClientException as e:
                if "already connected" in str(e).lower():
                    logging.info(f"Already connected to voice in guild {guild_id}")
                    return voice_channel.guild.voice_client
                logging.error(f"Client exception during voice connection: {e}")
                break
                
            except discord.Forbidden as e:
                logging.error(f"Permission denied for voice connection: {e}")
                break
                
            except asyncio.TimeoutError:
                logging.warning(f"Voice connection timeout for guild {guild_id}, attempt {attempt + 1}")
                await asyncio.sleep(backoff_multiplier ** attempt)
                continue
                
            except Exception as e:
                logging.error(f"Unexpected error during voice connection: {e}")
                await asyncio.sleep(backoff_multiplier ** attempt)
                continue
        
        # Track failed attempts
        self.connection_attempts[guild_id] = self.connection_attempts.get(guild_id, 0) + 1
        logging.error(f"Failed to connect to voice after {max_retries} attempts in guild {guild_id}")
        return None
    
    async def ensure_voice_connection(self, guild, channel_id=None):
        """Ensure we have a working voice connection"""
        voice_client = guild.voice_client
        
        # If we have a connection and it's working, return it
        if voice_client and voice_client.is_connected():
            return voice_client
        
        # If we don't have a channel ID, we can't reconnect
        if not channel_id:
            logging.warning(f"No channel ID provided for voice reconnection in guild {guild.id}")
            return None
        
        # Try to get the voice channel
        voice_channel = guild.get_channel(channel_id)
        if not voice_channel:
            logging.error(f"Voice channel {channel_id} not found in guild {guild.id}")
            return None
        
        # Attempt to connect
        return await self.connect_with_retry(voice_channel)

    async def handle_voice_disconnect(self, guild, music_state):
        """Handle unexpected voice disconnections"""
        try:
            guild_state = await music_state.get_guild_state(guild.id)
            
            # Clean up current track if playing
            if guild_state.current_track:
                logging.info(f"Cleaning up track after voice disconnect in guild {guild.id}")
                guild_state.current_track.cleanup()
                guild_state.current_track = None
            
            # Don't auto-reconnect - let users manually reconnect when ready
            logging.info(f"Voice disconnected in guild {guild.id}, waiting for manual reconnection")
            
        except Exception as e:
            logging.error(f"Error handling voice disconnect: {e}")
