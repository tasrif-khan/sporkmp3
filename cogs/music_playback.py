import discord
import asyncio
import logging
import time
from datetime import timedelta
import os
from utils.track_manager import AudioTrack, TrackManager
from utils.database_manager import DatabaseManager

class MusicPlayback:
    """Handles audio playback functionality"""
    def __init__(self, bot, track_manager, music_state, db, music_ui, ffmpeg_path='ffmpeg'):
        self.bot = bot
        self.track_manager = track_manager
        self.music_state = music_state
        self.db = db
        self.music_ui = music_ui
        self.ffmpeg_path = ffmpeg_path
    
    def get_pcm_audio(self, track, start_time=0, speed=None):
        """Get PCM audio source for playback with variable bitrate and speed"""
        if track is None:
            logging.error("Cannot create audio source: track is None")
            raise ValueError("Track is None")
            
        # Check if track has necessary attributes
        if not hasattr(track, 'last_accessed'):
            logging.error("Track missing required 'last_accessed' attribute")
            raise ValueError("Track missing required attributes")
            
        track.last_accessed = time.time()
        
        # Ensure start_time is within valid range
        start_time = max(0, min(start_time, track.duration))
        
        # Format timestamp properly for FFmpeg
        timestamp = str(timedelta(seconds=int(start_time)))
        before_options = f'-ss {timestamp}'
        
        # Determine appropriate bitrate based on file type (keep variable bitrate)
        if hasattr(track, 'bitrate') and track.bitrate:
            # Use the bitrate as-is, but clamp to reasonable range
            track.bitrate = max(64, min(320, track.bitrate))
        elif track.downloaded_path.lower().endswith(('.flac', '.wav')):
            track.bitrate = 256  # High quality for lossless formats
        else:
            track.bitrate = 192  # Default bitrate for most formats
        
        try:
            # If no speed provided, get from the database
            if speed is None:
                guild_id = None
                for guild in self.bot.guilds:
                    for vc in guild.voice_channels:
                        if guild.voice_client and guild.voice_client.channel == vc:
                            guild_id = guild.id
                            break
                if guild_id:
                    speed = self.db.get_playback_speed(guild_id)
                else:
                    speed = 100  # Default to normal speed
            
            # Apply speed filter with optimized approach
            speed_filter = ""
            if speed != 100 and 50 <= speed <= 200:
                speed_value = speed / 100.0
                
                # Optimize filter based on speed value
                if 0.5 <= speed_value <= 2.0:
                    # Single atempo filter - most efficient
                    speed_filter = f"-filter:a atempo={speed_value}"
                elif speed_value > 2.0:
                    # Chain filters for speeds above 2x
                    # Break into multiple 2x filters for efficiency
                    remaining_speed = speed_value
                    filters = []
                    while remaining_speed > 2.0:
                        filters.append("atempo=2.0")
                        remaining_speed /= 2.0
                    filters.append(f"atempo={remaining_speed}")
                    speed_filter = f"-filter:a \"{','.join(filters)}\""
                else:  # speed_value < 0.5
                    # Chain filters for very slow speeds
                    remaining_speed = speed_value
                    filters = []
                    while remaining_speed < 0.5:
                        filters.append("atempo=0.5")
                        remaining_speed /= 0.5
                    filters.append(f"atempo={remaining_speed}")
                    speed_filter = f"-filter:a \"{','.join(filters)}\""
            
            # Optimized FFmpeg options with better buffering
            ffmpeg_options = (
                f'-vn '  # No video
                f'-b:a {track.bitrate}k '  # Audio bitrate
                f'-bufsize 512k '  # Buffer size for smoother playback
                f'{speed_filter}'  # Speed filter if applicable
            ).strip()
            
            # Create audio source based on file type
            audio_source = discord.FFmpegPCMAudio(
                track.downloaded_path,
                before_options=before_options,
                executable=self.ffmpeg_path,
                options=ffmpeg_options
            )
            
            logging.info(f"Created audio source: bitrate={track.bitrate}kbps, speed={speed}%")
            
            # Start tracking playback position
            track.start_playback(start_time)
            
            return discord.PCMVolumeTransformer(audio_source, volume=track.volume / 100)
            
        except Exception as e:
            logging.error(f"Error creating audio source: {e}")
            raise

    async def send_now_playing_message(self, guild, guild_state):
        """Send a 'Now Playing' message to the last used channel"""
        if not guild_state.last_channel_id:
            logging.info("No channel ID stored, can't send now playing message")
            return

        try:
            # Find the channel
            channel = guild.get_channel(guild_state.last_channel_id)
            if not channel:
                logging.warning(f"Channel {guild_state.last_channel_id} not found")
                return

            # Get current track
            current_track = guild_state.current_track
            if not current_track:
                return

            # Calculate current position
            current_position = current_track.get_current_position()
            
            # Create the progress bar
            progress_bar = self.music_ui.create_progress_bar(
                current_position,
                current_track.duration
            )
            
            # Get the current speed setting
            speed = self.db.get_playback_speed(guild.id)
            speed_emoji = "ðŸŒ" if speed < 100 else "ðŸš€" if speed > 100 else "â±ï¸"
            
            # Create the embed
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['play']} Now Playing",
                f"{self.music_ui.emoji['music']} **Track:** {current_track.filename}\n"
                f"{self.music_ui.emoji['microphone']} **Requested by:** {current_track.requester}\n"
                f"{self.music_ui.emoji['time']} **Duration:** {self.music_ui.format_duration(int(current_track.duration))}\n"
                f"ðŸŽšï¸ **Bitrate:** {current_track.bitrate}kbps\n"
                f"{speed_emoji} **Speed:** {speed}%\n"
                f"**Progress:** {progress_bar}",
                discord.Color.green()
            )
            
            # Add queue information if there are more tracks
            if len(guild_state.queue) > guild_state.queue_position + 1:
                next_track = guild_state.queue[guild_state.queue_position + 1]
                embed.add_field(
                    name=f"{self.music_ui.emoji['queue']} Up Next",
                    value=f"{self.music_ui.emoji['music']} {next_track.filename}\n"
                        f"{self.music_ui.emoji['microphone']} Requested by: {next_track.requester}",
                    inline=False
                )
            
            await channel.send(embed=embed)
        except Exception as e:
            logging.error(f"Error sending now playing message: {e}")
    
    async def play_next(self, guild, force_play=False, advance=True):
        """Play the next track in the queue
        
        Args:
            guild: The guild to play in
            force_play: Whether to override autoplay setting
            advance: Whether to advance to next track (False when seeking to specific position)
        """
        guild_state = await self.music_state.get_guild_state(guild.id)
        if guild_state.is_seeking:
            return
        
        # Early validation: check if queue is empty or position is invalid
        if not guild_state.queue:
            logging.info(f"Queue is empty in guild {guild.id}, skipping playback")
            
            # Mark previous file as inactive if it exists
            current_track = guild_state.current_track
            if current_track and current_track.downloaded_path:
                self.track_manager.mark_file_inactive(current_track.downloaded_path)
            
            # Check autodisconnect setting
            if self.db.get_autodisconnect_setting(guild.id):
                voice_client = guild.voice_client
                if voice_client and voice_client.is_connected():
                    try:
                        await voice_client.disconnect()
                        logging.info(f"Auto-disconnected from guild {guild.id} due to empty queue")
                    except Exception as e:
                        logging.error(f"Error during auto-disconnect in guild {guild.id}: {e}")
            return
                        
        try:
            # Get current track before moving position
            current_track = guild_state.current_track
            
            # Handle looping
            if guild_state.loop_enabled and current_track:
                # Check if we've reached max loops
                if guild_state.max_loops is not None:
                    guild_state.loop_count += 1
                    if guild_state.loop_count >= guild_state.max_loops:
                        guild_state.loop_enabled = False
                        guild_state.loop_count = 0
                        guild_state.max_loops = None
                        # Move to next track
                        if advance:
                            guild_state.queue_position += 1
                    # else: stay at current position for another loop
                # else: infinite loop, stay at current position
            else:
                # Move to next track only if advance is True
                if advance:
                    guild_state.queue_position += 1

            # Check if we've reached the end of queue
            if guild_state.queue_position >= len(guild_state.queue):
                # Mark previous file as inactive if it exists
                if current_track and current_track.downloaded_path:
                    self.track_manager.mark_file_inactive(current_track.downloaded_path)
                
                # Check autodisconnect setting and handle disconnection
                if self.db.get_autodisconnect_setting(guild.id):
                    voice_client = guild.voice_client
                    if voice_client and voice_client.is_connected():
                        try:
                            await voice_client.disconnect()
                            logging.info(f"Auto-disconnected from guild {guild.id} due to empty queue")
                        except Exception as e:
                            logging.error(f"Error during auto-disconnect in guild {guild.id}: {e}")
                return

            # Check autoplay setting - skip if disabled unless force_play is True
            if not force_play and not self.db.get_autoplay_setting(guild.id):
                return

            # Get voice client
            voice_client = guild.voice_client
            if not voice_client or not voice_client.is_connected():
                logging.warning(f"Voice client not connected in guild {guild.id}")
                return

            # Get next track and handle download
            try:
                next_track = guild_state.current_track  # This uses the property based on queue_position
                
                # Additional safety check
                if not next_track:
                    logging.error(f"next_track is None in guild {guild.id} at position {guild_state.queue_position}")
                    return
                
                # Mark previous file as inactive if different from next
                if current_track and current_track != next_track and current_track.downloaded_path:
                    self.track_manager.mark_file_inactive(current_track.downloaded_path)
                
                # Mark new file as active
                if next_track.downloaded_path:
                    self.track_manager.mark_file_active(next_track.downloaded_path)
                
                # Download if not already downloaded
                if not next_track.downloaded_path:
                    await self.track_manager.ensure_temp_folder()
                    await next_track.download(self.bot.config['temp_folder'])
                    
                # Update last activity time
                guild_state.last_activity = time.time()
                
            except Exception as e:
                logging.error(f"Failed to prepare track at position {guild_state.queue_position} in guild {guild.id}: {e}")
                if next_track and next_track.downloaded_path:
                    self.track_manager.mark_file_inactive(next_track.downloaded_path)
                # Try to play next track in queue if this one fails
                await self.play_next(guild, force_play)
                return

            # Clean up any old temporary files
            try:
                await self.track_manager.cleanup_temp_files()
            except Exception as e:
                logging.error(f"Error during temp file cleanup: {e}")

            # Create and configure audio source
            try:
                # Get the speed setting for this guild
                speed = self.db.get_playback_speed(guild.id)
                
                audio_source = self.get_pcm_audio(
                    next_track, 
                    next_track.position,
                    speed
                )
                
                # Set volume from guild state
                audio_source.volume = guild_state.volume / 100
                
            except Exception as e:
                logging.error(f"Error creating audio source in guild {guild.id}: {e}")
                if next_track and next_track.downloaded_path:
                    self.track_manager.mark_file_inactive(next_track.downloaded_path)
                # Try to play next track in queue if this one fails
                await self.play_next(guild, force_play)
                return

            # Define after-playing callback
            def after_playing(error):
                if error:
                    logging.error(f'Player error in guild {guild.id}: {error}')
                
                # Reset alone timer if it exists
                self.music_state.alone_since.pop(guild.id, None)
                
                # Update the last activity timestamp
                guild_state.last_activity = time.time()
                
                # Don't auto-advance if we're seeking or manually seeking queue
                if not guild_state.is_seeking and not guild_state.manual_queue_seek:
                    # Schedule next track
                    asyncio.run_coroutine_threadsafe(
                        self.play_next(guild), 
                        self.bot.loop
                    )
                    
            # Start playback
            try:
                voice_client.play(audio_source, after=after_playing)
                logging.info(f"Started playing '{next_track.filename}' (position {guild_state.queue_position + 1}/{len(guild_state.queue)}) in guild {guild.id}")
                
                # Reset alone timer if it exists since we're actively playing
                self.music_state.alone_since.pop(guild.id, None)
                
                # Send the now playing message if we have a channel to send to
                if guild_state.last_channel_id:
                    await self.send_now_playing_message(guild, guild_state)
                
            except Exception as e:
                logging.error(f"Error starting playback in guild {guild.id}: {e}")
                if next_track and next_track.downloaded_path:
                    self.track_manager.mark_file_inactive(next_track.downloaded_path)
                # Try to play next track in queue if this one fails
                await self.play_next(guild, force_play)
                return

        except Exception as e:
            logging.error(f"Unexpected error in play_next for guild {guild.id}: {e}")
            # Clean up if there was an error
            try:
                current = guild_state.current_track
                if current and current.downloaded_path:
                    self.track_manager.mark_file_inactive(current.downloaded_path)
            except Exception as cleanup_error:
                logging.error(f"Error during cleanup after playback failure in guild {guild.id}: {cleanup_error}")