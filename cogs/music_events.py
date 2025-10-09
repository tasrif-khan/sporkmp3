import discord
import asyncio
import logging
import time
import re
import aiohttp
import os
from urllib.parse import urlparse
from utils.track_manager import AudioTrack
from utils.permission_checker import PermissionChecker
from utils.voice_handler import VoiceConnectionHandler

class MusicEvents:
    """Handles Discord event listeners for the music bot"""
    def __init__(self, bot, db, track_manager, music_state, music_ui, music_playback):
        self.bot = bot
        self.db = db
        self.track_manager = track_manager
        self.music_state = music_state
        self.music_ui = music_ui
        self.music_playback = music_playback
        self.voice_handler = VoiceConnectionHandler(bot)
    
    async def track_playback_activity(self):
        """Update activity timestamps for guilds with active playback"""
        while True:
            try:
                for guild in self.bot.guilds:
                    if guild.voice_client and guild.voice_client.is_playing():
                        guild_state = await self.music_state.get_guild_state(guild.id)
                        guild_state.last_activity = time.time()
                        logging.debug(f"Updated activity timestamp for guild {guild.id} during playback")
                
                # Check every 15 minutes
                await asyncio.sleep(900)
            except Exception as e:
                logging.error(f"Error in track_playback_activity: {e}")
                await asyncio.sleep(60)

    async def periodic_cleanup(self):
        """Cleanup inactive guilds, temporary files, and stale alone timers"""
        while True:
            try:
                current_time = time.time()
                inactive_guilds = []
                cleaned_files = 0
                error_count = 0

                # 1. Clean up alone timers (5 minute threshold)
                for guild_id in list(self.music_state.alone_since.keys()):
                    try:
                        if current_time - self.music_state.alone_since[guild_id] > 300:  # 5 minutes
                            guild = self.bot.get_guild(guild_id)
                            if guild and guild.voice_client:
                                voice_channel = guild.voice_client.channel
                                if len(voice_channel.members) == 1:  # Still alone
                                    await guild.voice_client.disconnect()
                                    logging.info(f"Disconnected from guild {guild_id} after being alone for 5 minutes")
                                
                            self.music_state.alone_since.pop(guild_id, None)
                    except Exception as e:
                        logging.error(f"Error cleaning up alone timer for guild {guild_id}: {e}")
                        error_count += 1

                # 2. Clean up inactive guilds (3 hour threshold)
                for guild_id, state in list(self.music_state.guild_states.items()):
                    try:
                        if current_time - state.last_activity > 10800:  # 3 hours
                            # Clean up voice client if still connected
                            guild = self.bot.get_guild(guild_id)
                            if guild and guild.voice_client:
                                await guild.voice_client.disconnect()

                            # Clean up current track
                            if state.current_track:
                                state.current_track.cleanup()

                            # Clean up queued tracks
                            for track in state.queue:
                                track.cleanup()
                            
                            # Remove guild state
                            del self.music_state.guild_states[guild_id]
                            inactive_guilds.append(guild_id)
                            
                            logging.info(f"Cleaned up inactive guild {guild_id}")
                    except Exception as e:
                        logging.error(f"Error cleaning up inactive guild {guild_id}: {e}")
                        error_count += 1

                # 3. Clean up rate limits (60 second threshold)
                try:
                    self.music_state.rate_limits = {
                        guild_id: time for guild_id, time in self.music_state.rate_limits.items()
                        if current_time - time < 60
                    }
                except Exception as e:
                    logging.error(f"Error cleaning up rate limits: {e}")
                    error_count += 1

                # 4. Clean up temporary files
                try:
                    await self.track_manager.cleanup_temp_files()
                    cleaned_files += 1
                except Exception as e:
                    logging.error(f"Error cleaning up temporary files: {e}")
                    error_count += 1

                # 5. Validate persistent files every hour
                try:
                    if hasattr(self, '_last_file_validation'):
                        if current_time - self._last_file_validation > 3600:  # 1 hour
                            orphaned_count = self.db.validate_persistent_files()
                            if orphaned_count > 0:
                                logging.info(f"Periodic file validation: cleaned {orphaned_count} orphaned entries")
                            self._last_file_validation = current_time
                    else:
                        self._last_file_validation = current_time
                except Exception as e:
                    logging.error(f"Error in periodic file validation: {e}")
                    error_count += 1

                # Log cleanup summary if anything was cleaned
                if inactive_guilds or cleaned_files or error_count:
                    logging.info(
                        f"Cleanup completed: {len(inactive_guilds)} inactive guilds removed, "
                        f"{cleaned_files} temp file cleanups, "
                        f"{error_count} errors encountered"
                    )

                # Wait before next cleanup cycle (5 minutes)
                await asyncio.sleep(300)

            except Exception as e:
                logging.error(f"Error in periodic cleanup main loop: {e}")
                # If main loop encounters error, wait 1 minute before retrying
                await asyncio.sleep(60)
    
    async def on_voice_state_update(self, member, before, after):
        """Handle bot disconnection when alone in channel"""
        if member.bot:
            return

        if before.channel is not None:
            # Check if bot is in the channel that was left
            voice_client = before.channel.guild.voice_client
            if voice_client and voice_client.channel == before.channel:
                # Check if the bot is alone in the channel
                if len(before.channel.members) == 1:
                    # Record the time when bot was left alone
                    self.music_state.alone_since[before.channel.guild.id] = time.time()
                    logging.info(f"Bot left alone in guild {before.channel.guild.id}, starting 5-minute timer")
                else:
                    # If we're not alone anymore, remove the alone_since entry
                    if before.channel.guild.id in self.music_state.alone_since:
                        self.music_state.alone_since.pop(before.channel.guild.id, None)
                        logging.info(f"Bot no longer alone in guild {before.channel.guild.id}, cancelling timer")

    async def extract_discord_cdn_urls(self, message_content):
        """Extract Discord CDN URLs from message content"""
        # Improved pattern to match Discord CDN URLs with query parameters
        cdn_pattern = r'https://cdn\.discordapp\.com/attachments/\d+/\d+/[^?\s]+(?:\?[^\s]*)?'
        urls = re.findall(cdn_pattern, message_content)
        
        # Debug logging
        logging.info(f"Searching for CDN URLs in message: {message_content}")
        logging.info(f"Found {len(urls)} potential CDN URLs: {urls}")
        
        validated_urls = []
        SUPPORTED_FORMATS = ('.mp3', '.wav', '.ogg', '.m4a', '.flac', '.aac', '.mp4')
        
        for url in urls:
            # Extract filename from URL (ignore query parameters)
            parsed_url = urlparse(url)
            filename = os.path.basename(parsed_url.path)
            
            logging.info(f"Processing URL: {url}")
            logging.info(f"Extracted filename: {filename}")
            
            # Check if it's a supported audio format
            if any(filename.lower().endswith(fmt) for fmt in SUPPORTED_FORMATS):
                try:
                    # Get file size with HEAD request
                    async with aiohttp.ClientSession() as session:
                        async with session.head(url) as resp:
                            if resp.status == 200:
                                file_size = int(resp.headers.get('content-length', 0))
                                validated_urls.append({
                                    'url': url,
                                    'filename': filename,
                                    'size': file_size
                                })
                                logging.info(f"Successfully validated Discord CDN audio file: {filename} ({file_size} bytes)")
                            else:
                                logging.warning(f"Could not access Discord CDN URL: {url} (status: {resp.status})")
                except Exception as e:
                    logging.error(f"Error processing Discord CDN URL {url}: {e}")
            else:
                logging.info(f"Skipping non-audio file: {filename}")
        
        logging.info(f"Final validated URLs: {len(validated_urls)} files")
        return validated_urls

    async def on_message(self, message):
        """Enhanced message handler with permission checking and Discord CDN URL support"""
        if message.author.bot:
            return

        bot_mention = f'<@{self.bot.user.id}>'
        if bot_mention in message.content:
            try:
                # Store the channel ID for this guild for now playing messages
                guild_state = await self.music_state.get_guild_state(message.guild.id)
                guild_state.last_channel_id = message.channel.id
                
                # Check text permissions first
                missing_text_perms = PermissionChecker.check_text_permissions(message.channel, message.guild.me)
                if missing_text_perms:
                    # Can't send embeds, try basic message if we have send_messages permission
                    if "Send Messages" not in missing_text_perms:
                        try:
                            await message.channel.send("❌ I'm missing required permissions to function properly.")
                        except:
                            pass
                    return

                # Check if user is in voice channel
                if not message.author.voice:
                    embed = self.music_ui.create_embed(
                        f"{self.music_ui.emoji['warning']} Voice Channel Required",
                        "You need to be in a voice channel to use this command!",
                        discord.Color.yellow()
                    )
                    await message.channel.send(embed=embed)
                    return

                voice_channel = message.author.voice.channel
                
                # Check voice permissions
                missing_voice_perms = PermissionChecker.check_voice_permissions(voice_channel, message.guild.me)
                if missing_voice_perms:
                    embed = PermissionChecker.get_permission_error_embed(missing_voice_perms, "voice")
                    await message.channel.send(embed=embed)
                    return

                # Combine regular attachments and Discord CDN URLs
                all_audio_files = []
                
                # Process regular attachments
                SUPPORTED_FORMATS = ('.mp3', '.wav', '.ogg', '.m4a', '.flac', '.aac', '.mp4')
                if message.attachments:
                    audio_attachments = [
                        att for att in message.attachments 
                        if any(att.filename.lower().endswith(fmt) for fmt in SUPPORTED_FORMATS)
                    ]
                    
                    for att in audio_attachments:
                        all_audio_files.append({
                            'url': att.url,
                            'filename': att.filename,
                            'size': att.size
                        })

                # Process Discord CDN URLs from message content
                cdn_urls = await self.extract_discord_cdn_urls(message.content)
                all_audio_files.extend(cdn_urls)

                if not all_audio_files:
                    embed = self.music_ui.create_embed(
                        f"{self.music_ui.emoji['warning']} No Audio Files Found",
                        f"Please attach audio files or provide Discord CDN links to audio files when mentioning the bot.\n\n"
                        f"**Supported formats:** {', '.join(SUPPORTED_FORMATS)}",
                        discord.Color.yellow()
                    )
                    await message.channel.send(embed=embed)
                    return

                # Calculate total size of all audio files
                total_size = sum(file_info['size'] for file_info in all_audio_files)
                
                # Check queue size limits
                if not self.track_manager.can_add_to_queue(guild_state.queue, total_size):
                    current_size_mb = self.track_manager.get_queue_size(guild_state.queue) / (1024 * 1024)
                    embed = self.music_ui.create_embed(
                        f"{self.music_ui.emoji['warning']} Queue Full",
                        f"Queue size limit reached! Current size: {current_size_mb:.1f}MB",
                        discord.Color.yellow()
                    )
                    await message.channel.send(embed=embed)
                    return

                # Add all tracks to queue
                added_tracks = []
                skipped_tracks = []
                for file_info in all_audio_files:
                    try:
                        # Create track
                        track = AudioTrack(
                            file_info['url'],
                            file_info['filename'],
                            message.author.display_name,
                            file_info['size'],
                            is_permanent=False
                        )
                        
                        # Add to queue
                        guild_state.queue.append(track)
                        added_tracks.append(file_info['filename'])
                    except Exception as e:
                        logging.error(f"Error adding track {file_info['filename']}: {e}")
                        skipped_tracks.append(file_info['filename'])

                # Prepare and send status message
                status_message = []
                if added_tracks:
                    status_message.append(f"✅ Added {len(added_tracks)} tracks to queue:")
                    for i, track in enumerate(added_tracks, 1):
                        status_message.append(f"{i}. {track}")

                if skipped_tracks:
                    status_message.append(f"\n❌ Failed to add {len(skipped_tracks)} tracks:")
                    for track in skipped_tracks:
                        status_message.append(f"• {track}")

                current_size_mb = self.track_manager.get_queue_size(guild_state.queue) / (1024 * 1024)
                max_size_mb = self.bot.config['max_queue_size_mb']
                status_message.append(f"\n{self.music_ui.emoji['cd']} Queue Size: {current_size_mb:.1f}MB / {max_size_mb}MB")

                embed = self.music_ui.create_embed(
                    f"{self.music_ui.emoji['success']} Batch Upload Complete",
                    "\n".join(status_message),
                    discord.Color.green()
                )
                await message.channel.send(embed=embed)

                # Increment upload count and check if we should ask for a rating
                if added_tracks:
                    # Get current values from database
                    current_count = self.db.get_upload_count(message.guild.id)
                    last_request = self.db.get_last_rating_request(message.guild.id)
                    
                    # Increment the counter
                    new_count = current_count + len(added_tracks)
                    self.db.increment_upload_count(message.guild.id, len(added_tracks))
                    
                    current_time = int(time.time())
                    
                    # Only ask for rating if:
                    # 1. They've uploaded at least 10 files
                    # 2. We haven't asked in the last 7 days (604800 seconds)
                    if (new_count >= 10 and 
                            (current_time - last_request > 604800)):
                        
                        # Reset the counter and update last request time
                        self.db.reset_upload_count(message.guild.id)
                        self.db.update_last_rating_request(message.guild.id, current_time)
                        
                        rating_embed = self.music_ui.create_embed(
                            f"{self.music_ui.emoji['success']} Enjoying SporkMP3?",
                            "If you're enjoying the bot, please consider rating us on top.gg!\n"
                            "Your support helps more people discover the bot.\n\n"
                            "[Rate SporkMP3 on top.gg](https://top.gg/bot/1318106283760680970)",
                            discord.Color.gold()
                        )
                        await message.channel.send(embed=rating_embed)

                # Ensure queue position is valid before attempting playback
                if guild_state.queue_position >= len(guild_state.queue):
                    guild_state.queue_position = -1
                    logging.warning(f"Reset invalid queue_position for guild {message.guild.id}")

                # Connect to voice channel if not already connected
                if not message.guild.voice_client and added_tracks:
                    try:
                        # Check rate limit before attempting connection
                        current_time = time.time()
                        if message.guild.id in self.voice_handler.last_attempt_time:
                            time_since_last = current_time - self.voice_handler.last_attempt_time[message.guild.id]
                            if time_since_last < 30:  # 30 second rate limit
                                wait_time = int(30 - time_since_last)
                                embed = self.music_ui.create_embed(
                                    f"{self.music_ui.emoji['warning']} Connection Rate Limited",
                                    f"Please wait **{wait_time} seconds** before reconnecting to voice.\n\n"
                                    f"**Tip:** Files have been added to queue. Use `/play` to start playback after the cooldown.",
                                    discord.Color.yellow()
                                )
                                await message.channel.send(embed=embed)
                                return
                        
                        voice_client = await self.voice_handler.connect_with_retry(voice_channel)
                        if voice_client:
                            # Check autoplay setting before starting playback
                            if self.db.get_autoplay_setting(message.guild.id):
                                await self.music_playback.play_next(message.guild)
                        else:
                            embed = self.music_ui.create_embed(
                                f"{self.music_ui.emoji['error']} Connection Failed",
                                "Failed to connect to voice channel after multiple attempts.\n\n"
                                f"**Files added to queue:** Use `/play` to retry playback.",
                                discord.Color.red()
                            )
                            await message.channel.send(embed=embed)
                    except discord.Forbidden:
                        logging.error("Failed to connect to voice channel - Missing permissions")
                        embed = self.music_ui.create_embed(
                            f"{self.music_ui.emoji['error']} Connection Error",
                            "Failed to connect to voice channel due to missing permissions!",
                            discord.Color.red()
                        )
                        await message.channel.send(embed=embed)
                    except Exception as e:
                        logging.error(f"Error connecting to voice channel: {e}")
                        embed = self.music_ui.create_embed(
                            f"{self.music_ui.emoji['error']} Connection Error",
                            "Failed to connect to voice channel!\n\n"
                            f"**Files added to queue:** Use `/play` to retry playback.",
                            discord.Color.red()
                        )
                        await message.channel.send(embed=embed)
                        return

                # If already connected and nothing is playing, start playback if autoplay is enabled
                elif (message.guild.voice_client and 
                    not message.guild.voice_client.is_playing() and 
                    self.db.get_autoplay_setting(message.guild.id) and 
                    added_tracks):
                    await self.music_playback.play_next(message.guild)

            except discord.Forbidden as e:
                logging.error(f"Permission error in on_message handler: {e}")
                # Just log the error and return if we don't have permissions
                return
            except Exception as e:
                logging.error(f"Error in batch upload handler: {e}")
                try:
                    embed = self.music_ui.create_embed(
                        f"{self.music_ui.emoji['error']} Error",
                        "An unexpected error occurred while processing your request.",
                        discord.Color.red()
                    )
                    await message.channel.send(embed=embed)
                except:
                    logging.error("Failed to send error message")
                
                # Attempt to clean up if there was an error
                try:
                    guild_state = await self.music_state.get_guild_state(message.guild.id)
                    if guild_state.current_track:
                        guild_state.current_track.cleanup()
                except Exception as cleanup_error:
                    logging.error(f"Error during cleanup after batch upload error: {cleanup_error}")