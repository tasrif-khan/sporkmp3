import discord
import logging
import asyncio
import os
import time
from utils.permission_checks import check_permissions, admin_only
from utils.track_manager import AudioTrack
from utils.permission_checker import PermissionChecker, safe_interaction
from utils.voice_handler import VoiceConnectionHandler

class MusicCommands:
    """Handles command logic for the music bot - but doesn't register commands"""
    def __init__(self, bot, db, track_manager, music_state, music_ui, music_playback):
        self.bot = bot
        self.db = db
        self.track_manager = track_manager
        self.music_state = music_state
        self.music_ui = music_ui
        self.music_playback = music_playback
        self.voice_handler = VoiceConnectionHandler(bot)
    
    # NOTE: These are not commands themselves, just methods implementing command logic
    
    @safe_interaction
    async def blacklist(self, interaction, action, user):
        try:
            if action.lower() not in ['add', 'remove']:
                embed = self.music_ui.create_embed(
                    f"{self.music_ui.emoji['warning']} Invalid Action",
                    "Action must be either 'add' or 'remove'.",
                    discord.Color.yellow()
                )
                await interaction.followup.send(embed=embed)
                return

            if action.lower() == 'add':
                self.db.add_to_blacklist(interaction.guild_id, user.id)
                action_text = "added to"
            else:
                self.db.remove_from_blacklist(interaction.guild_id, user.id)
                action_text = "removed from"

            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['success']} Blacklist Updated",
                f"{user.mention} has been {action_text} the blacklist.",
                discord.Color.green()
            )
            await interaction.followup.send(embed=embed)
        except Exception as e:
            logging.error(f"Error managing blacklist: {e}")
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['error']} Error",
                "Failed to update blacklist.",
                discord.Color.red()
            )
            await interaction.followup.send(embed=embed)

    @safe_interaction
    async def role_config(self, interaction, action, role):
        try:
            if action.lower() not in ['add', 'remove']:
                embed = self.music_ui.create_embed(
                    f"{self.music_ui.emoji['warning']} Invalid Action",
                    "Action must be either 'add' or 'remove'.",
                    discord.Color.yellow()
                )
                await interaction.followup.send(embed=embed)
                return

            if action.lower() == 'add':
                self.db.add_to_role_whitelist(interaction.guild_id, role.id)
                action_text = "added to"
            else:
                self.db.remove_from_role_whitelist(interaction.guild_id, role.id)
                action_text = "removed from"

            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['success']} Role Whitelist Updated",
                f"{role.mention} has been {action_text} the role whitelist.",
                discord.Color.green()
            )
            await interaction.followup.send(embed=embed)
        except Exception as e:
            logging.error(f"Error managing role whitelist: {e}")
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['error']} Error",
                "Failed to update role whitelist.",
                discord.Color.red()
            )
            await interaction.followup.send(embed=embed)

    @safe_interaction
    async def autodisconnect(self, interaction, enabled):
        try:
            self.db.set_autodisconnect_setting(interaction.guild_id, enabled)
            status = "enabled" if enabled else "disabled"
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['success']} Auto-Disconnect Updated",
                f"Auto-disconnect when queue is empty has been {status}.",
                discord.Color.green()
            )
            await interaction.followup.send(embed=embed)
        except Exception as e:
            logging.error(f"Error setting autodisconnect: {e}")
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['error']} Error",
                "Failed to update auto-disconnect setting.",
                discord.Color.red()
            )
            await interaction.followup.send(embed=embed)

    @safe_interaction
    async def autoplay(self, interaction, enabled):
        try:
            self.db.set_autoplay_setting(interaction.guild_id, enabled)
            status = "enabled" if enabled else "disabled"
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['success']} Autoplay Updated",
                f"Autoplay has been {status}.",
                discord.Color.green()
            )
            self.music_state.alone_since.pop(interaction.guild_id, None)
            await interaction.followup.send(embed=embed)
        except Exception as e:
            logging.error(f"Error setting autoplay: {e}")
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['error']} Error",
                "Failed to update autoplay setting.",
                discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
    
    @safe_interaction
    async def speed(self, interaction, speed):
        """Set playback speed"""
        if speed < 50 or speed > 200:
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['warning']} Invalid Speed",
                "Speed must be between 50% and 200%!",
                discord.Color.yellow()
            )
            await interaction.followup.send(embed=embed)
            return

        try:
            # Store the new speed in the database
            self.db.set_playback_speed(interaction.guild_id, speed)
            
            speed_emoji = "ðŸŒ" if speed < 100 else "ðŸš€" if speed > 100 else "â±ï¸"
            
            # No song playing, just update the setting
            embed = self.music_ui.create_embed(
                f"{speed_emoji} Playback Speed Changed",
                f"Set speed to **{speed}%**\n"
                f"**Note:** This setting will apply to all future tracks.",
                discord.Color.blue()
            )
                
            await interaction.followup.send(embed=embed)
        except Exception as e:
            logging.error(f"Error setting playback speed: {e}")
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['error']} Speed Error",
                "Failed to set playback speed!",
                discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
    
    @safe_interaction
    async def play(self, interaction, name=None):
        try:
            # Check if user is in a voice channel
            if not interaction.user.voice:
                embed = self.music_ui.create_embed(
                    f"{self.music_ui.emoji['warning']} Voice Channel Required",
                    "You need to be in a voice channel to use this command!",
                    discord.Color.yellow()
                )
                await interaction.followup.send(embed=embed)
                return

            voice_channel = interaction.user.voice.channel
            bot_member = interaction.guild.me
            
            # Check voice permissions
            missing_voice_perms = PermissionChecker.check_voice_permissions(voice_channel, bot_member)
            if missing_voice_perms:
                embed = PermissionChecker.get_permission_error_embed(missing_voice_perms, "voice")
                await interaction.followup.send(embed=embed)
                return
            
            # Check text permissions
            missing_text_perms = PermissionChecker.check_text_permissions(interaction.channel, bot_member)
            if missing_text_perms:
                embed = PermissionChecker.get_permission_error_embed(missing_text_perms, "text")
                await interaction.followup.send(embed=embed)
                return

            # Get guild state
            guild_state = await self.music_state.get_guild_state(interaction.guild_id)
            
            # If a name is provided, add from the sound library to queue
            if name:
                # Verify file exists and is accessible
                track_info, error_msg = self.db.verify_file_before_play(interaction.guild_id, name)
                if not track_info:
                    embed = self.music_ui.create_embed(
                        f"{self.music_ui.emoji['warning']} File Error",
                        error_msg,
                        discord.Color.yellow()
                    )
                    await interaction.followup.send(embed=embed)
                    return
                
                # Create a track from the library file
                try:
                    # Create an AudioTrack object for the library file
                    track = AudioTrack(
                        None,  # No URL needed since file is local
                        track_info["filename"],
                        track_info["uploaded_by"],
                        track_info["file_size"],
                        is_permanent=True #mark as permanent so it doesn't delete it
                    )
                    
                    # Set the downloaded_path to the permanent file
                    track.downloaded_path = track_info["file_path"]
                    
                    # Get audio metadata
                    track.duration = track.get_audio_metadata(track_info["file_path"])
                    
                    # Add to queue instead of playing directly
                    guild_state.queue.append(track)
                    
                    # Create success embed for adding to queue
                    embed = self.music_ui.create_embed(
                        f"{self.music_ui.emoji['success']} Added to Queue",
                        f"{self.music_ui.emoji['music']} **Name:** {name}\n"
                        f"{self.music_ui.emoji['microphone']} **Uploaded by:** {track_info['uploaded_by']}\n"
                        f"{self.music_ui.emoji['time']} **Duration:** {self.music_ui.format_duration(int(track.duration))}\n"
                        f"Ã°Å¸Å½Å¡Ã¯Â¸ **Bitrate:** {track.bitrate}kbps\n"
                        f"{self.music_ui.emoji['queue']} **Queue position:** #{len(guild_state.queue)}",
                        discord.Color.green()
                    )
                    await interaction.followup.send(embed=embed)
                    
                    # Get or create voice client
                    voice_client = interaction.guild.voice_client
                    if not voice_client:
                        try:
                            voice_client = await self.voice_handler.connect_with_retry(voice_channel)
                            if not voice_client:
                                embed = self.music_ui.create_embed(
                                    f"{self.music_ui.emoji['error']} Connection Failed",
                                    "Failed to connect to voice channel after multiple attempts.",
                                    discord.Color.red()
                                )
                                await interaction.followup.send(embed=embed)
                                return
                        except Exception as e:
                            logging.error(f"Failed to connect to voice channel: {e}")
                            embed = self.music_ui.create_embed(
                                f"{self.music_ui.emoji['error']} Connection Failed",
                                "Failed to connect to voice channel!",
                                discord.Color.red()
                            )
                            await interaction.followup.send(embed=embed)
                            return
                    
                    # If nothing is currently playing, start playback
                    if not voice_client.is_playing():
                        # Start at position 0 if queue_position is -1 (nothing played yet)
                        if guild_state.queue_position == -1:
                            guild_state.queue_position = 0
                        await self.music_playback.play_next(interaction.guild, force_play=True, advance=False)
                                        
                except Exception as e:
                    logging.error(f"Error adding library song to queue: {e}")
                    embed = self.music_ui.create_embed(
                        f"{self.music_ui.emoji['error']} Queue Error",
                        f"Failed to add sound to queue: {str(e)}",
                        discord.Color.red()
                    )
                    await interaction.followup.send(embed=embed)
                    return
            else:
                # Original queue-based play logic (no changes here)
                # Check queue
                if not guild_state.queue:
                    embed = self.music_ui.create_embed(
                        f"{self.music_ui.emoji['warning']} Empty Queue",
                        "No songs in queue! Add some audio files first.",
                        discord.Color.yellow()
                    )
                    await interaction.followup.send(embed=embed)
                    return

                # Get or create voice client
                voice_client = interaction.guild.voice_client
                if not voice_client:
                    try:
                        voice_client = await self.voice_handler.connect_with_retry(voice_channel)
                        if not voice_client:
                            embed = self.music_ui.create_embed(
                                f"{self.music_ui.emoji['error']} Connection Failed",
                                "Failed to connect to voice channel after multiple attempts.",
                                discord.Color.red()
                            )
                            await interaction.followup.send(embed=embed)
                            return
                    except Exception as e:
                        logging.error(f"Failed to connect to voice channel: {e}")
                        embed = self.music_ui.create_embed(
                            f"{self.music_ui.emoji['error']} Connection Failed",
                            "Failed to connect to voice channel!",
                            discord.Color.red()
                        )
                        await interaction.followup.send(embed=embed)
                        return

                # Check if already playing
                if voice_client.is_playing():
                    embed = self.music_ui.create_embed(
                        f"{self.music_ui.emoji['warning']} Already Playing",
                        "A track is already playing! Use /skip to play the next track.",
                        discord.Color.yellow()
                    )
                    await interaction.followup.send(embed=embed)
                    return

                try:
                    # Attempt to play next track with force_play=True to override autoplay setting
                    await self.music_playback.play_next(interaction.guild, force_play=True)
                    
                    # Check if track started playing successfully
                    if guild_state.current_track:
                        # Get current position using the new tracking method
                        current_position = guild_state.current_track.get_current_position()
                        
                        # Calculate progress bar
                        progress_bar = self.music_ui.create_progress_bar(
                            current_position, 
                            guild_state.current_track.duration
                        )
                        
                        # Get speed setting
                        speed = self.db.get_playback_speed(interaction.guild_id)
                        speed_emoji = "Ã°Å¸Å’" if speed < 100 else "Ã°Å¸Å¡â‚¬" if speed > 100 else "Ã¢Â±Ã¯Â¸"
                        
                        # Create success embed with detailed information
                        embed = self.music_ui.create_embed(
                            f"{self.music_ui.emoji['play']} Now Playing",
                            f"{self.music_ui.emoji['music']} **Track:** {guild_state.current_track.filename}\n"
                            f"{self.music_ui.emoji['microphone']} **Requested by:** {guild_state.current_track.requester}\n"
                            f"{self.music_ui.emoji['time']} **Duration:** {self.music_ui.format_duration(int(guild_state.current_track.duration))}\n"
                            f"Ã°Å¸Å½Å¡Ã¯Â¸ **Bitrate:** {guild_state.current_track.bitrate}kbps\n"
                            f"{speed_emoji} **Speed:** {speed}%\n"
                            f"**Progress:** {progress_bar}",
                            discord.Color.green()
                        )
                        
                        # Add queue information if there are more tracks
                        if guild_state.queue:
                            next_track = guild_state.queue[0]
                            embed.add_field(
                                name=f"{self.music_ui.emoji['queue']} Up Next",
                                value=f"{self.music_ui.emoji['music']} {next_track.filename}\n"
                                    f"{self.music_ui.emoji['microphone']} Requested by: {next_track.requester}",
                                inline=False
                            )
                        
                        await interaction.followup.send(embed=embed)
                    else:
                        embed = self.music_ui.create_embed(
                            f"{self.music_ui.emoji['error']} Playback Failed",
                            "Failed to start playback. Please try again or check the file.",
                            discord.Color.red()
                        )
                        await interaction.followup.send(embed=embed)

                except Exception as e:
                    logging.error(f"Error playing track: {e}")
                    embed = self.music_ui.create_embed(
                        f"{self.music_ui.emoji['error']} Playback Error",
                        f"An error occurred while trying to play: {str(e)}",
                        discord.Color.red()
                    )
                    await interaction.followup.send(embed=embed)
                    
                    # Attempt to clean up if there was an error
                    try:
                        if guild_state.current_track:
                            guild_state.current_track.cleanup()
                            guild_state.current_track = None
                    except Exception as cleanup_error:
                        logging.error(f"Error during cleanup after playback failure: {cleanup_error}")

        except Exception as e:
            logging.error(f"Unexpected error in play command: {e}")
            # This will be handled by the @safe_interaction decorator
            raise
            
    @safe_interaction
    async def upload(self, interaction, name, file):
        try:
            # Check permissions (admin or DJ role)
            has_dj_role = False
            for role in interaction.user.roles:
                if role.name.lower() in ['dj', 'music', 'audio']:
                    has_dj_role = True
                    break
                    
            if not has_dj_role and not interaction.user.guild_permissions.administrator:
                embed = self.music_ui.create_embed(
                    f"{self.music_ui.emoji['error']} Permission Denied",
                    "You need to be an administrator or have a DJ role to upload sounds.",
                    discord.Color.red()
                )
                await interaction.followup.send(embed=embed)
                return
                
            # Validate name format (alphanumeric and underscores only)
            if not name.replace('_', '').isalnum():
                embed = self.music_ui.create_embed(
                    f"{self.music_ui.emoji['warning']} Invalid Name",
                    "Sound names can only contain letters, numbers, and underscores.",
                    discord.Color.yellow()
                )
                await interaction.followup.send(embed=embed)
                return
                
            # Check if file is attached
            if not file:
                embed = self.music_ui.create_embed(
                    f"{self.music_ui.emoji['warning']} Missing File",
                    "You must attach an audio file with this command.",
                    discord.Color.yellow()
                )
                await interaction.followup.send(embed=embed)
                return
                
            # Check file type
            SUPPORTED_FORMATS = ('.mp3', '.wav', '.ogg', '.m4a', '.flac', '.aac', '.mp4')
            if not any(file.filename.lower().endswith(fmt) for fmt in SUPPORTED_FORMATS):
                embed = self.music_ui.create_embed(
                    f"{self.music_ui.emoji['error']} Invalid File Type",
                    f"Please provide audio files in one of these formats: {', '.join(SUPPORTED_FORMATS)}",
                    discord.Color.red()
                )
                await interaction.followup.send(embed=embed)
                return
                
            # Check if name already exists
            existing_track = self.db.get_persistent_track(interaction.guild_id, name)
            if existing_track:
                embed = self.music_ui.create_embed(
                    f"{self.music_ui.emoji['warning']} Name Already Exists",
                    f"A sound with the name '{name}' already exists. Please choose another name or remove the existing sound first.",
                    discord.Color.yellow()
                )
                await interaction.followup.send(embed=embed)
                return
                
            # Check storage limit
            if not self.db.can_add_to_storage(interaction.guild_id, file.size):
                storage = self.db.get_guild_storage(interaction.guild_id)
                used_mb = storage["used"] / (1024 * 1024)
                max_mb = storage["max"] / (1024 * 1024)
                
                embed = self.music_ui.create_embed(
                    f"{self.music_ui.emoji['warning']} Storage Limit Reached",
                    f"Server storage limit reached: {used_mb:.2f}MB / {max_mb:.2f}MB used. Remove some sounds first.",
                    discord.Color.yellow()
                )
                await interaction.followup.send(embed=embed)
                return
                
            # Download and save the file
            persistent_folder = self.bot.config.get('persistent_storage_folder', 'permanent')
            server_folder = os.path.join(persistent_folder, str(interaction.guild_id))
            
            # Create server folder if it doesn't exist
            if not os.path.exists(server_folder):
                os.makedirs(server_folder)
                
            # Create safe filename: name + original extension
            _, file_extension = os.path.splitext(file.filename)
            safe_filename = f"{name}{file_extension}"
            file_path = os.path.join(server_folder, safe_filename)
            
            # Download the file
            await file.save(file_path)
            
            # Add to database
            self.db.add_persistent_track(
                interaction.guild_id,
                name,
                file.filename,
                file_path,
                interaction.user.display_name,
                file.size
            )
            
            # Update storage usage
            self.db.increase_guild_storage(interaction.guild_id, file.size)
            
            # Create success embed
            storage = self.db.get_guild_storage(interaction.guild_id)
            used_mb = storage["used"] / (1024 * 1024)
            max_mb = storage["max"] / (1024 * 1024)
            
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['success']} Sound Uploaded",
                f"{self.music_ui.emoji['music']} **Name:** {name}\n"
                f"{self.music_ui.emoji['cd']} **Original filename:** {file.filename}\n"
                f"{self.music_ui.emoji['user']} **Uploaded by:** {interaction.user.display_name}\n"
                f"{self.music_ui.emoji['time']} **Size:** {file.size / (1024 * 1024):.2f}MB\n\n"
                f"**Server Storage:** {used_mb:.2f}MB / {max_mb:.2f}MB",
                discord.Color.green()
            )
            await interaction.followup.send(embed=embed)
                
        except Exception as e:
            logging.error(f"Error uploading sound: {e}")
            # This will be handled by the @safe_interaction decorator
            raise

    @safe_interaction
    async def library(self, interaction):
        try:
            tracks = self.db.list_persistent_tracks(interaction.guild_id)
            
            if not tracks:
                embed = self.music_ui.create_embed(
                    f"{self.music_ui.emoji['music']} Sound Library",
                    "No sounds in the library yet. Use /upload to add sounds!",
                    discord.Color.blue()
                )
                await interaction.followup.send(embed=embed)
                return
                
            # Get storage info
            storage = self.db.get_guild_storage(interaction.guild_id)
            used_mb = storage["used"] / (1024 * 1024)
            max_mb = storage["max"] / (1024 * 1024)
            
            # Create list of tracks
            track_list = []
            for i, track in enumerate(tracks, 1):
                upload_date = time.strftime('%Y-%m-%d', time.localtime(track["upload_date"]))
                size_mb = track["file_size"] / (1024 * 1024)
                
                track_list.append(
                    f"`{i}.` **{track['track_name']}**\n"
                    f"â”— {self.music_ui.emoji['user']} {track['uploaded_by']} | "
                    f"{self.music_ui.emoji['time']} {upload_date} | "
                    f"{self.music_ui.emoji['cd']} {size_mb:.2f}MB"
                )
            
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['music']} Sound Library",
                "\n".join(track_list) + f"\n\n**Storage Used:** {used_mb:.2f}MB / {max_mb:.2f}MB",
                discord.Color.blue()
            )
            await interaction.followup.send(embed=embed)
            
        except Exception as e:
            logging.error(f"Error listing library: {e}")
            # This will be handled by the @safe_interaction decorator
            raise

    @safe_interaction
    async def remove_sound(self, interaction, name):
        try:
            # Check permissions (admin or DJ role)
            has_dj_role = False
            for role in interaction.user.roles:
                if role.name.lower() in ['dj', 'music', 'audio']:
                    has_dj_role = True
                    break
                    
            if not has_dj_role and not interaction.user.guild_permissions.administrator:
                embed = self.music_ui.create_embed(
                    f"{self.music_ui.emoji['error']} Permission Denied",
                    "You need to be an administrator or have a DJ role to remove sounds.",
                    discord.Color.red()
                )
                await interaction.followup.send(embed=embed)
                return
                
            # Check if track exists
            track = self.db.get_persistent_track(interaction.guild_id, name)
            if not track:
                embed = self.music_ui.create_embed(
                    f"{self.music_ui.emoji['warning']} Sound Not Found",
                    f"No sound found with the name '{name}'.",
                    discord.Color.yellow()
                )
                await interaction.followup.send(embed=embed)
                return
                
            # Remove from database (this also updates storage)
            file_path = self.db.remove_persistent_track(interaction.guild_id, name)
            
            # Delete the actual file
            if file_path and os.path.exists(file_path):
                os.remove(file_path)
                
            # Get updated storage info
            storage = self.db.get_guild_storage(interaction.guild_id)
            used_mb = storage["used"] / (1024 * 1024)
            max_mb = storage["max"] / (1024 * 1024)
            
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['success']} Sound Removed",
                f"Successfully removed sound '{name}'\n\n"
                f"**Server Storage:** {used_mb:.2f}MB / {max_mb:.2f}MB",
                discord.Color.green()
            )
            await interaction.followup.send(embed=embed)
            
        except Exception as e:
            logging.error(f"Error removing sound: {e}")
            # This will be handled by the @safe_interaction decorator
            raise
            
    @safe_interaction
    async def pause(self, interaction):
        voice_client = interaction.guild.voice_client
        if not voice_client or not voice_client.is_playing():
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['warning']} Not Playing",
                "Nothing is currently playing!",
                discord.Color.yellow()
            )
            await interaction.followup.send(embed=embed)
            return

        try:
            guild_state = await self.music_state.get_guild_state(interaction.guild_id)
            
            # Mark track as paused
            if guild_state.current_track:
                guild_state.current_track.pause_playback()
                
            voice_client.pause()
            
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['pause']} Paused",
                f"Paused: **{guild_state.current_track.filename}**\n"
                f"Use `/resume` to continue playback",
                discord.Color.blue()
            )
            await interaction.followup.send(embed=embed)
        except Exception as e:
            logging.error(f"Error pausing playback: {e}")
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['error']} Pause Error",
                "Failed to pause the music!",
                discord.Color.red()
            )
            await interaction.followup.send(embed=embed)

    @safe_interaction
    async def resume(self, interaction):
        voice_client = interaction.guild.voice_client
        if not voice_client or not voice_client.is_paused():
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['warning']} Not Paused",
                "Nothing is currently paused!",
                discord.Color.yellow()
            )
            await interaction.followup.send(embed=embed)
            return

        try:
            guild_state = await self.music_state.get_guild_state(interaction.guild_id)
            
            # Resume track timing
            if guild_state.current_track:
                guild_state.current_track.resume_playback()
                
            voice_client.resume()
            
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['resume']} Resumed",
                f"Resumed: **{guild_state.current_track.filename}**",
                discord.Color.green()
            )
            await interaction.followup.send(embed=embed)
        except Exception as e:
            logging.error(f"Error resuming playback: {e}")
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['error']} Resume Error",
                "Failed to resume the music!",
                discord.Color.red()
            )
            await interaction.followup.send(embed=embed)

    @safe_interaction
    async def queue(self, interaction):
        guild_state = await self.music_state.get_guild_state(interaction.guild_id)
        if not guild_state.queue:
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['queue']} Queue Empty",
                "No tracks in queue! Add some audio files to get started.",
                discord.Color.blue()
            )
            await interaction.followup.send(embed=embed)
            return

        try:
            current_size_mb = self.track_manager.get_queue_size(guild_state.queue) / (1024 * 1024)
            max_size_mb = self.bot.config['max_queue_size_mb']
            
            # Build queue list with indicator for current track
            queue_list = []
            for idx, track in enumerate(guild_state.queue):
                # Add indicator based on position
                if idx == guild_state.queue_position:
                    prefix = "â–¶ "  # Currently playing
                elif idx < guild_state.queue_position:
                    prefix = "  "  # Before current position
                else:
                    prefix = "  "  # After current position (upcoming)
                
                queue_list.append(
                    f"`{idx + 1}.` {prefix}{self.music_ui.emoji['music']} **{track.filename}**\n"
                    f"   â””â”€ {self.music_ui.emoji['microphone']} Requested by: {track.requester}"
                )
            
            queue_text = "\n".join(queue_list)
            
            # Add position info
            position_info = ""
            if guild_state.queue_position >= 0:
                position_info = f"{self.music_ui.emoji['play']} **Current Position:** {guild_state.queue_position + 1} / {len(guild_state.queue)}\n\n"
            
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['queue']} Current Queue",
                f"{position_info}"
                f"{queue_text}\n\n"
                f"{self.music_ui.emoji['cd']} **Queue Size:** {current_size_mb:.1f}MB / {max_size_mb}MB\n"
                f"{self.music_ui.emoji['music']} **Total Tracks:** {len(guild_state.queue)}",
                discord.Color.blue()
            )
            await interaction.followup.send(embed=embed)
        except Exception as e:
            logging.error(f"Error displaying queue: {e}")
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['error']} Queue Error",
                "Failed to display the queue!",
                discord.Color.red()
            )
            await interaction.followup.send(embed=embed)

    @safe_interaction
    async def playing(self, interaction):
        guild_state = await self.music_state.get_guild_state(interaction.guild_id)
        if not guild_state.current_track:
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['warning']} Not Playing",
                "Nothing is currently playing!",
                discord.Color.yellow()
            )
            await interaction.followup.send(embed=embed)
            return

        try:
            # Get current position using the new tracking method
            current_position = guild_state.current_track.get_current_position()
            
            # Format the position and duration
            current_position_str = self.music_ui.format_duration(int(current_position))
            total_duration = self.music_ui.format_duration(int(guild_state.current_track.duration))
            
            # Create progress bar
            progress_bar = self.music_ui.create_progress_bar(
                current_position,
                guild_state.current_track.duration
            )
            
            # Get speed setting
            speed = self.db.get_playback_speed(interaction.guild_id)
            speed_emoji = "ðŸŒ" if speed < 100 else "ðŸš€" if speed > 100 else "â±ï¸"
            
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['play']} Now Playing",
                f"{self.music_ui.emoji['music']} **Track:** {guild_state.current_track.filename}\n"
                f"{self.music_ui.emoji['microphone']} **Requested by:** {guild_state.current_track.requester}\n"
                f"{self.music_ui.emoji['time']} **Time:** `{current_position_str} / {total_duration}`\n"
                f"ðŸŽšï¸ **Bitrate:** {guild_state.current_track.bitrate}kbps\n"
                f"{speed_emoji} **Speed:** {speed}%\n"
                f"**Progress:** {progress_bar}",
                discord.Color.green()
            )
            await interaction.followup.send(embed=embed)
        except Exception as e:
            logging.error(f"Error displaying current track: {e}")
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['error']} Display Error",
                "Failed to display current track info!",
                discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
            
    @safe_interaction
    async def volume(self, interaction, volume):
        if volume < 0 or volume > 120:
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['warning']} Invalid Volume",
                "Volume must be between 0 and 120!",
                discord.Color.yellow()
            )
            await interaction.followup.send(embed=embed)
            return

        try:
            guild_state = await self.music_state.get_guild_state(interaction.guild_id)
            guild_state.volume = volume
            
            voice_client = interaction.guild.voice_client
            if voice_client and voice_client.source:
                voice_client.source.volume = volume / 100
            
            # Choose appropriate volume emoji
            volume_emoji = self.music_ui.emoji['volume']
            if volume == 0:
                volume_emoji = self.music_ui.emoji['mute']
            elif volume < 50:
                volume_emoji = self.music_ui.emoji['low_volume']
            
            embed = self.music_ui.create_embed(
                f"{volume_emoji} Volume Updated",
                f"Set volume to **{volume}%**",
                discord.Color.blue()
            )
            await interaction.followup.send(embed=embed)
        except Exception as e:
            logging.error(f"Error setting volume: {e}")
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['error']} Volume Error",
                "Failed to set volume!",
                discord.Color.red()
            )
            await interaction.followup.send(embed=embed)

    @safe_interaction
    async def forward(self, interaction, seconds):
        guild_state = await self.music_state.get_guild_state(interaction.guild_id)
        if not guild_state.current_track:
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['warning']} Not Playing",
                "Nothing is currently playing!",
                discord.Color.yellow()
            )
            await interaction.followup.send(embed=embed)
            return

        if seconds <= 0:
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['warning']} Invalid Time",
                "Please specify a positive number of seconds!",
                discord.Color.yellow()
            )
            await interaction.followup.send(embed=embed)
            return

        try:
            # Get current position using tracking method
            current_pos = guild_state.current_track.get_current_position()
            new_position = min(current_pos + seconds, guild_state.current_track.duration - 1)
            
            voice_client = interaction.guild.voice_client
            if not voice_client or not voice_client.is_connected():
                embed = self.music_ui.create_embed(
                    f"{self.music_ui.emoji['error']} Not Connected",
                    "Bot is not connected to a voice channel!",
                    discord.Color.red()
                )
                await interaction.followup.send(embed=embed)
                return

            guild_state.is_seeking = True
            guild_state.current_track.position = new_position
            
            if voice_client.is_playing():
                voice_client.stop()
            
            await asyncio.sleep(0.5)
            
            # Get speed setting
            speed = self.db.get_playback_speed(interaction.guild_id)
            
            audio_source = self.music_playback.get_pcm_audio(
                guild_state.current_track, 
                new_position,
                speed
            )
            
            def after_seeking(error):
                guild_state.is_seeking = False
                if error:
                    logging.error(f'Seeking error: {error}')
            
            voice_client.play(audio_source, after=after_seeking)
            
            # Calculate actual seconds moved forward
            actual_skip = new_position - current_pos
            
            # Calculate progress bar
            progress_bar = self.music_ui.create_progress_bar(
                new_position,
                guild_state.current_track.duration
            )
            
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['time']} Skipped Forward",
                f"{self.music_ui.emoji['music']} **Track:** {guild_state.current_track.filename}\n"
                f"{self.music_ui.emoji['time']} **Skipped:** {actual_skip} seconds forward\n"
                f"**New Position:** {self.music_ui.format_duration(new_position)} / {self.music_ui.format_duration(int(guild_state.current_track.duration))}\n"
                f"**Progress:** {progress_bar}",
                discord.Color.blue()
            )
            await interaction.followup.send(embed=embed)
            
        except Exception as e:
            guild_state.is_seeking = False
            logging.error(f"Error during forward seek: {e}")
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['error']} Forward Error",
                "An error occurred while seeking forward!",
                discord.Color.red()
            )
            await interaction.followup.send(embed=embed)

    @safe_interaction
    async def backward(self, interaction, seconds):
        guild_state = await self.music_state.get_guild_state(interaction.guild_id)
        if not guild_state.current_track:
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['warning']} Not Playing",
                "Nothing is currently playing!",
                discord.Color.yellow()
            )
            await interaction.followup.send(embed=embed)
            return

        if seconds <= 0:
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['warning']} Invalid Time",
                "Please specify a positive number of seconds!",
                discord.Color.yellow()
            )
            await interaction.followup.send(embed=embed)
            return

        try:
            # Get current position using tracking method
            current_pos = guild_state.current_track.get_current_position()
            new_position = max(0, current_pos - seconds)
            
            voice_client = interaction.guild.voice_client
            if not voice_client or not voice_client.is_connected():
                embed = self.music_ui.create_embed(
                    f"{self.music_ui.emoji['error']} Not Connected",
                    "Bot is not connected to a voice channel!",
                    discord.Color.red()
                )
                await interaction.followup.send(embed=embed)
                return

            guild_state.is_seeking = True
            guild_state.current_track.position = new_position
            
            if voice_client.is_playing():
                voice_client.stop()
            
            await asyncio.sleep(0.5)
            
            # Get speed setting
            speed = self.db.get_playback_speed(interaction.guild_id)
            
            audio_source = self.music_playback.get_pcm_audio(
                guild_state.current_track, 
                new_position,
                speed
            )
            
            def after_seeking(error):
                guild_state.is_seeking = False
                if error:
                    logging.error(f'Seeking error: {error}')
            
            voice_client.play(audio_source, after=after_seeking)
            
            # Calculate actual seconds moved backward
            actual_skip = current_pos - new_position
            
            # Calculate progress bar
            progress_bar = self.music_ui.create_progress_bar(
                new_position,
                guild_state.current_track.duration
            )
            
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['time']} Skipped Backward",
                f"{self.music_ui.emoji['music']} **Track:** {guild_state.current_track.filename}\n"
                f"{self.music_ui.emoji['time']} **Skipped:** {actual_skip} seconds backward\n"
                f"**New Position:** {self.music_ui.format_duration(new_position)} / {self.music_ui.format_duration(int(guild_state.current_track.duration))}\n"
                f"**Progress:** {progress_bar}",
                discord.Color.blue()
            )
            await interaction.followup.send(embed=embed)
            
        except Exception as e:
            guild_state.is_seeking = False
            logging.error(f"Error during backward seek: {e}")
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['error']} Backward Error",
                "An error occurred while seeking backward!",
                discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
            
    @safe_interaction
    async def timestamp(self, interaction, hours, minutes, seconds):
        guild_state = await self.music_state.get_guild_state(interaction.guild_id)
        if not guild_state.current_track:
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['warning']} Not Playing",
                "Nothing is currently playing!",
                discord.Color.yellow()
            )
            await interaction.followup.send(embed=embed)
            return

        if hours < 0 or minutes < 0 or seconds < 0 or seconds >= 60 or minutes >= 60:
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['warning']} Invalid Time",
                "Invalid timestamp! Format: hours >= 0, 0 <= minutes < 60, 0 <= seconds < 60",
                discord.Color.yellow()
            )
            await interaction.followup.send(embed=embed)
            return

        new_position = (hours * 3600) + (minutes * 60) + seconds
        
        if new_position >= guild_state.current_track.duration:
            total_duration = self.music_ui.format_duration(int(guild_state.current_track.duration))
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['warning']} Invalid Position",
                f"Cannot seek beyond the end of the track! Maximum duration is {total_duration}",
                discord.Color.yellow()
            )
            await interaction.followup.send(embed=embed)
            return

        voice_client = interaction.guild.voice_client
        if not voice_client or not voice_client.is_connected():
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['error']} Not Connected",
                "Bot is not connected to a voice channel!",
                discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
            return

        try:
            guild_state.is_seeking = True
            guild_state.current_track.position = new_position
            
            if voice_client.is_playing():
                voice_client.stop()
            
            await asyncio.sleep(0.5)
            
            # Get speed setting
            speed = self.db.get_playback_speed(interaction.guild_id)
            
            audio_source = self.music_playback.get_pcm_audio(
                guild_state.current_track, 
                guild_state.current_track.position,
                speed
            )
            
            def after_seeking(error):
                guild_state.is_seeking = False
                if error:
                    logging.error(f'Seeking error: {error}')
            
            voice_client.play(audio_source, after=after_seeking)
            
            new_timestamp = self.music_ui.format_duration(new_position)
            total_duration = self.music_ui.format_duration(int(guild_state.current_track.duration))
            
            # Calculate progress bar
            progress_bar = self.music_ui.create_progress_bar(
                new_position,
                guild_state.current_track.duration
            )
            
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['time']} Position Updated",
                f"{self.music_ui.emoji['music']} **Track:** {guild_state.current_track.filename}\n"
                f"{self.music_ui.emoji['time']} **New Position:** {new_timestamp} / {total_duration}\n"
                f"**Progress:** {progress_bar}",
                discord.Color.blue()
            )
            await interaction.followup.send(embed=embed)
        except Exception as e:
            guild_state.is_seeking = False
            logging.error(f"Error during timestamp seek: {e}")
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['error']} Seeking Error",
                "An error occurred while seeking!",
                discord.Color.red()
            )
            await interaction.followup.send(embed=embed)

    @safe_interaction
    async def skip(self, interaction):
        guild_state = await self.music_state.get_guild_state(interaction.guild_id)
        voice_client = interaction.guild.voice_client
        if not voice_client or not voice_client.is_playing():
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['warning']} Not Playing",
                "Nothing is currently playing!",
                discord.Color.yellow()
            )
            await interaction.followup.send(embed=embed)
            return

        try:
            skipped_track = guild_state.current_track.filename
            voice_client.stop()  # This will trigger play_next via the after callback
            
            # Show next track info if available
            next_track_info = (f"\n{self.music_ui.emoji['play']} **Up next:** {guild_state.queue[0].filename}" 
                             if guild_state.queue else "\nQueue is now empty")
            
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['skip']} Track Skipped",
                f"{self.music_ui.emoji['music']} **Skipped:** {skipped_track}{next_track_info}",
                discord.Color.blue()
            )
            await interaction.followup.send(embed=embed)
        except Exception as e:
            logging.error(f"Error skipping track: {e}")
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['error']} Skip Error",
                "Failed to skip the track!",
                discord.Color.red()
            )
            await interaction.followup.send(embed=embed)

    @safe_interaction
    async def clear(self, interaction):
        """Clear the entire queue and stop playback"""
        guild_state = await self.music_state.get_guild_state(interaction.guild_id)
        try:
            queue_length = len(guild_state.queue)
            
            # Stop current playback
            voice_client = interaction.guild.voice_client
            if voice_client and voice_client.is_playing():
                voice_client.stop()
            
            # Mark all files as inactive and clean up non-permanent tracks
            for track in guild_state.queue:
                if track.downloaded_path:
                    self.track_manager.mark_file_inactive(track.downloaded_path)
                if not track.is_permanent:
                    track.cleanup()
            
            # Clear queue and reset position
            guild_state.queue.clear()
            guild_state.queue_position = -1
            
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['success']} Queue Cleared",
                f"{self.music_ui.emoji['stop']} Stopped playback\n"
                f"{self.music_ui.emoji['music']} Cleared {queue_length} tracks from the queue\n"
                f"{self.music_ui.emoji['queue']} Queue is now empty",
                discord.Color.green()
            )
            await interaction.followup.send(embed=embed)
        except Exception as e:
            logging.error(f"Error clearing queue: {e}")
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['error']} Clear Error",
                "Failed to clear the queue!",
                discord.Color.red()
            )
            await interaction.followup.send(embed=embed)

    @safe_interaction
    async def stop(self, interaction):
        """Stop the current playback without clearing the queue"""
        voice_client = interaction.guild.voice_client
        if not voice_client or not voice_client.is_playing():
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['warning']} Not Playing",
                "Nothing is currently playing!",
                discord.Color.yellow()
            )
            await interaction.followup.send(embed=embed)
            return
    
        try:
            guild_state = await self.music_state.get_guild_state(interaction.guild_id)
            current_track_name = guild_state.current_track.filename if guild_state.current_track else "Unknown"
            
            voice_client.stop()
            
            if guild_state.current_track:
                self.track_manager.mark_file_inactive(guild_state.current_track.downloaded_path)
                guild_state.current_track.cleanup()
                guild_state.current_track = None
    
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['stop']} Playback Stopped",
                f"{self.music_ui.emoji['music']} **Stopped playing:** {current_track_name}\n"
                f"{self.music_ui.emoji['queue']} Queue remains unchanged",
                discord.Color.blue()
            )
            await interaction.followup.send(embed=embed)
        except Exception as e:
            logging.error(f"Error stopping playback: {e}")
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['error']} Stop Error",
                "Failed to stop the playback!",
                discord.Color.red()
            )
            await interaction.followup.send(embed=embed)

    @safe_interaction
    async def disconnect(self, interaction):
        guild_state = await self.music_state.get_guild_state(interaction.guild_id)
        voice_client = interaction.guild.voice_client
        if voice_client and voice_client.is_connected():
            try:
                # Stop playback first
                if voice_client.is_playing():
                    voice_client.stop()
                
                # Wait a moment for stop to complete
                await asyncio.sleep(0.5)
                
                # Mark all files as inactive and clean up non-permanent tracks
                for track in guild_state.queue:
                    if track.downloaded_path:
                        self.track_manager.mark_file_inactive(track.downloaded_path)
                    if not track.is_permanent:
                        track.cleanup()
                
                queue_length = len(guild_state.queue)
                
                # Reset all playback state using the helper method
                guild_state.reset_playback_state()
                
                # Disconnect from voice
                await voice_client.disconnect()
                
                # Clear voice connection rate limit for this guild
                self.voice_handler.last_attempt_time.pop(interaction.guild_id, None)
                self.voice_handler.connection_attempts.pop(interaction.guild_id, None)
                logging.info(f"Cleared voice connection rate limits for guild {interaction.guild_id}")
                
                embed = self.music_ui.create_embed(
                    f"{self.music_ui.emoji['disconnect']} Disconnected",
                    f"{self.music_ui.emoji['success']} Successfully disconnected from voice channel\n"
                    f"{self.music_ui.emoji['queue']} Cleared {queue_length} tracks from queue\n"
                    f"{self.music_ui.emoji['stop']} All resources cleaned up",
                    discord.Color.blue()
                )
                await interaction.followup.send(embed=embed)
            except Exception as e:
                logging.error(f"Error disconnecting: {e}")
                embed = self.music_ui.create_embed(
                    f"{self.music_ui.emoji['error']} Disconnect Error",
                    f"Failed to disconnect properly: {str(e)}",
                    discord.Color.red()
                )
                await interaction.followup.send(embed=embed)
        else:
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['warning']} Not Connected",
                "Not connected to a voice channel!",
                discord.Color.yellow()
            )
            await interaction.followup.send(embed=embed)

    @safe_interaction
    async def loop(self, interaction, times=None):
        guild_state = await self.music_state.get_guild_state(interaction.guild_id)
        
        if not guild_state.current_track:
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['warning']} Not Playing",
                "Nothing is currently playing!",
                discord.Color.yellow()
            )
            await interaction.followup.send(embed=embed)
            return
    
        try:
            if times is not None and times <= 0:
                embed = self.music_ui.create_embed(
                    f"{self.music_ui.emoji['warning']} Invalid Input",
                    "Please specify a positive number of loops or leave empty for infinite loop.",
                    discord.Color.yellow()
                )
                await interaction.followup.send(embed=embed)
                return
    
            # Toggle loop mode
            guild_state.loop_enabled = not guild_state.loop_enabled if times is None else True
            guild_state.max_loops = times
            guild_state.loop_count = 0
    
            if guild_state.loop_enabled:
                loop_msg = "infinitely" if times is None else f"{times} times"
                embed = self.music_ui.create_embed(
                    f"{self.music_ui.emoji['success']} Loop Enabled",
                    f"{self.music_ui.emoji['music']} **Track:** {guild_state.current_track.filename}\n"
                    f"ðŸ”„ **Loop Mode:** Will loop {loop_msg}",
                    discord.Color.green()
                )
            else:
                embed = self.music_ui.create_embed(
                    f"{self.music_ui.emoji['success']} Loop Disabled",
                    f"{self.music_ui.emoji['music']} **Track:** {guild_state.current_track.filename}\n"
                    f"ðŸ”„ **Loop Mode:** Disabled",
                    discord.Color.blue()
                )
    
            await interaction.followup.send(embed=embed)
            
        except Exception as e:
            logging.error(f"Error toggling loop mode: {e}")
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['error']} Loop Error",
                "Failed to toggle loop mode!",
                discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
    
    @safe_interaction
    async def remove(self, interaction, position):
        """Remove a specific song from the queue"""
        guild_state = await self.music_state.get_guild_state(interaction.guild_id)
        
        # Check if queue is empty
        if not guild_state.queue:
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['warning']} Empty Queue",
                "The queue is empty!",
                discord.Color.yellow()
            )
            await interaction.followup.send(embed=embed)
            return
        
        # Check if position is valid
        if position < 1 or position > len(guild_state.queue):
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['warning']} Invalid Position",
                f"Please enter a valid position between 1 and {len(guild_state.queue)}",
                discord.Color.yellow()
            )
            await interaction.followup.send(embed=embed)
            return
        
        try:
            # Remove the track (position-1 because users will see queue starting at 1)
            removed_track = guild_state.queue.pop(position-1)
            
            # Clean up the removed track
            removed_track.cleanup()
            
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['success']} Track Removed",
                f"{self.music_ui.emoji['music']} Removed: **{removed_track.filename}**\n"
                f"{self.music_ui.emoji['queue']} Queue position: **#{position}**\n"
                f"{self.music_ui.emoji['user']} Requested by: {removed_track.requester}",
                discord.Color.green()
            )
            await interaction.followup.send(embed=embed)
            
        except Exception as e:
            logging.error(f"Error removing track: {e}")
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['error']} Error",
                "Failed to remove the track from queue!",
                discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
    
    @safe_interaction
    async def seekqueue(self, interaction, position):
        """Jump to a specific position in the queue"""
        guild_state = await self.music_state.get_guild_state(interaction.guild_id)
        
        if not guild_state.queue:
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['warning']} Empty Queue",
                "The queue is empty!",
                discord.Color.yellow()
            )
            await interaction.followup.send(embed=embed)
            return
        
        if position < 1 or position > len(guild_state.queue):
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['warning']} Invalid Position",
                f"Please enter a position between 1 and {len(guild_state.queue)}",
                discord.Color.yellow()
            )
            await interaction.followup.send(embed=embed)
            return
        
        try:
            # Set a flag to prevent after_playing callback from auto-advancing
            guild_state.manual_queue_seek = True
            
            # Set position (convert from 1-indexed to 0-indexed)
            guild_state.queue_position = position - 1
            
            # Stop current playback (this will trigger after_playing callback)
            voice_client = interaction.guild.voice_client
            if voice_client and voice_client.is_playing():
                voice_client.stop()
            
            # Wait a moment for stop to complete
            await asyncio.sleep(0.5)
            
            # Play the selected track WITHOUT advancing
            await self.music_playback.play_next(interaction.guild, force_play=True, advance=False)
            
            # Reset the flag after starting playback
            guild_state.manual_queue_seek = False
            
            track = guild_state.current_track
            embed = self.music_ui.create_embed(
                f"{self.music_ui.emoji['play']} Jumped to Position #{position}",
                f"{self.music_ui.emoji['music']} **Track:** {track.filename}\n"
                f"{self.music_ui.emoji['microphone']} **Requested by:** {track.requester}\n"
                f"{self.music_ui.emoji['queue']} **Position:** {position} / {len(guild_state.queue)}",
                discord.Color.green()
            )
            await interaction.followup.send(embed=embed)
            
        except Exception as e:
            logging.error(f"Error seeking queue: {e}")
            raise

    @safe_interaction
    async def help(self, interaction):
        commands_list = [
            f"{self.music_ui.emoji['play']} **/play [name]** - Play from queue or add library sound to queue",
            f"{self.music_ui.emoji['music']} **/upload <name>** - Upload a sound to the library (Admin/DJ)",
            f"{self.music_ui.emoji['queue']} **/library** - Show all sounds in the library",
            f"{self.music_ui.emoji['stop']} **/remove_sound <name>** - Remove a sound from the library (Admin/DJ)",
            f"{self.music_ui.emoji['stop']} **/stop** - Stop the current playback",
            f"{self.music_ui.emoji['pause']} **/pause** - Pause the current song",
            f"{self.music_ui.emoji['resume']} **/resume** - Resume the current song",
            f"{self.music_ui.emoji['queue']} **/queue** - Show the queue (â–¶ = now playing, âœ“ = played)",
            f"{self.music_ui.emoji['queue']} **/seekqueue <position>** - Jump to a specific track in the queue",
            f"{self.music_ui.emoji['music']} **/playing** - Show what's currently playing",
            f"{self.music_ui.emoji['stop']} **/clear** - Clear entire queue and stop playback",
            f"{self.music_ui.emoji['queue']} **/remove <position>** - Remove a specific song from the queue",
            f"{self.music_ui.emoji['volume']} **/volume <0-120>** - Set the volume",
            f"{self.music_ui.emoji['time']} **/speed <50-200>** - Set playback speed percentage",
            f"{self.music_ui.emoji['skip']} **/skip** - Skip to the next song",
            f"{self.music_ui.emoji['loop']} **/loop <times>** - Toggle loop mode (optional: specify number of loops)",
            f"{self.music_ui.emoji['time']} **/forward <seconds>** - Skip forward by specified seconds",
            f"{self.music_ui.emoji['time']} **/backward <seconds>** - Skip backward by specified seconds",
            f"{self.music_ui.emoji['time']} **/timestamp <hours> <minutes> <seconds>** - Set track position",
            f"{self.music_ui.emoji['disconnect']} **/disconnect** - Disconnect the bot\n",
            f"{self.music_ui.emoji['settings']} **/autoplay <true/false>** - Enable or disable autoplay (Admin)",
            f"{self.music_ui.emoji['settings']} **/autodisconnect <true/false>** - Enable/disable auto-disconnect when queue ends (Admin)",
            f"{self.music_ui.emoji['user']} **/blacklist <add/remove> <user>** - Manage blacklisted users (Admin)",
            f"{self.music_ui.emoji['role']} **/role_config <add/remove> <role>** - Manage role whitelist (Admin)"
        ]
        embed = self.music_ui.create_embed(
            f"{self.music_ui.emoji['music']} SporkMP3 Bot Commands",
            f"{self.music_ui.emoji['cd']} **File Upload:** Mention the bot and attach audio file(s) (up to 10 at once!)\n\n" +
            "**Available Commands:**\n" + "\n".join(commands_list),
            discord.Color.blue()
        )
        
        # Add quick tips field
        embed.add_field(
            name=f"{self.music_ui.emoji['success']} Quick Tips",
            value="â€¢ Upload audio files by mentioning the bot\n"
                "â€¢ Use /queue to see all tracks (â–¶ shows current position)\n"
                "â€¢ Use /seekqueue to jump to any track\n"
                "â€¢ Use /playing to see current track progress\n"
                "â€¢ Use /library to view permanent sound collection\n"
                "â€¢ Use /upload to add sounds to the library\n"
                "â€¢ All files stay in queue until /clear or /disconnect\n"
                "â€¢ MP4 audio is supported!",
            inline=False
        )
        
        await interaction.followup.send(embed=embed)

    # Add health check command
    @safe_interaction 
    async def health(self, interaction):
        """Health check command for administrators"""
        from utils.health_monitor import HealthMonitor
        
        try:
            # Check voice connections
            connected_guilds = len([g for g in self.bot.guilds if g.voice_client])
            
            # Check file integrity
            orphaned_files = self.db.validate_persistent_files()
            
            # Get uptime if available
            uptime_hours = 0
            if hasattr(self.bot, 'start_time'):
                uptime_hours = (time.time() - self.bot.start_time) / 3600
            
            embed = discord.Embed(title="ðŸ¥ Bot Health Check", color=discord.Color.green())
            embed.add_field(name="Voice Connections", value=f"{connected_guilds} active", inline=True)
            embed.add_field(name="File Integrity", value=f"{orphaned_files} orphaned entries cleaned", inline=True)
            embed.add_field(name="Guilds", value=len(self.bot.guilds), inline=True)
            embed.add_field(name="Uptime", value=f"{uptime_hours:.1f} hours", inline=True)
            
            await interaction.followup.send(embed=embed)
        except Exception as e:
            logging.error(f"Error in health check: {e}")
            # This will be handled by the @safe_interaction decorator
            raise

   