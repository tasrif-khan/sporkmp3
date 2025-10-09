import os
import time
import aiohttp
import logging
from mutagen.mp3 import MP3
from mutagen import File
from mutagen.mp3 import MP3
from mutagen.wave import WAVE
from mutagen.ogg import OggFileType
from mutagen.oggvorbis import OggVorbis
from mutagen.flac import FLAC
from mutagen.aac import AAC
from mutagen.mp4 import MP4
from datetime import timedelta

class AudioTrack:
    def __init__(self, url, filename, requester, file_size, is_permanent=False):
        self.url = url
        self.filename = filename
        self.requester = requester
        self.position = 0
        self.duration = 0
        self.downloaded_path = None
        self.file_size = file_size
        self.last_accessed = time.time()
        self.download_retries = 3
        self.volume = 100  # Default volume level
        self.bitrate = None  # Will be determined after download
        self.playback_start_time = None  # Used to track playback position
        self.paused_position = 0  # Keeps track of position when paused
        self.is_paused = False  # Indicates if track is paused
        self.is_permanent = is_permanent
    
    def get_audio_metadata(self, file_path):
        """Get audio metadata regardless of format"""
        try:
            # Try different audio formats
            if file_path.lower().endswith('.mp3'):
                audio = MP3(file_path)
                if hasattr(audio.info, 'bitrate'):
                    self.bitrate = audio.info.bitrate // 1000  # Convert to kbps
            elif file_path.lower().endswith('.wav'):
                audio = WAVE(file_path)
                # WAV bitrate can be calculated from sample rate and bit depth
                if hasattr(audio.info, 'sample_rate') and hasattr(audio.info, 'bits_per_sample'):
                    self.bitrate = (audio.info.sample_rate * audio.info.bits_per_sample) // 1000
            elif file_path.lower().endswith('.ogg'):
                try:
                    audio = OggVorbis(file_path)
                    if hasattr(audio.info, 'bitrate'):
                        self.bitrate = audio.info.bitrate // 1000
                except Exception as e:
                    logging.error(f"Error loading OGG file with OggVorbis: {e}")
                    # Fallback to generic parser if OggVorbis fails
                    audio = File(file_path)
            elif file_path.lower().endswith('.flac'):
                audio = FLAC(file_path)
                # FLAC doesn't have a constant bitrate, estimate from file size and duration
                if hasattr(audio.info, 'length'):
                    file_size_bits = os.path.getsize(file_path) * 8
                    self.bitrate = int(file_size_bits / audio.info.length / 1000)
            elif file_path.lower().endswith(('.m4a', '.aac')):
                audio = File(file_path)
                if hasattr(audio.info, 'bitrate'):
                    self.bitrate = audio.info.bitrate // 1000
            elif file_path.lower().endswith('.mp4'):
                audio = MP4(file_path)
                if hasattr(audio.info, 'bitrate'):
                    self.bitrate = audio.info.bitrate // 1000
            else:
                # Fallback to generic audio file handling
                audio = File(file_path)
                
            if audio is None:
                raise ValueError(f"Could not determine audio format for {file_path}")
                
            # Get duration - handle different audio file types
            if hasattr(audio, 'info') and hasattr(audio.info, 'length'):
                duration = audio.info.length
            elif hasattr(audio, 'info') and hasattr(audio.info, 'duration'):
                duration = audio.info.duration
            elif hasattr(audio, 'length'):
                duration = audio.length
            else:
                raise ValueError(f"Could not determine duration for {file_path}")
            
            # Set a default bitrate if we couldn't determine it
            if not self.bitrate:
                logging.info(f"Could not determine bitrate for {file_path}, using default")
                self.bitrate = 192
                
            # Add debug logging
            logging.info(f"Successfully extracted duration {duration} and bitrate {self.bitrate}kbps from {file_path}")
            return duration
            
        except Exception as e:
            logging.error(f"Error processing audio file {file_path}: {str(e)}")
            raise ValueError(f"Error processing audio file {file_path}: {str(e)}")

    async def download(self, temp_folder):
        """Download the audio file and get its metadata"""
        # Create a safe filename to prevent path traversal
        safe_filename = ''.join(c for c in self.filename if c.isalnum() or c in '._- ')
        self.downloaded_path = os.path.join(temp_folder, safe_filename)
        
        for attempt in range(self.download_retries):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(self.url) as resp:
                        if resp.status == 200:
                            with open(self.downloaded_path, 'wb') as f:
                                while True:
                                    chunk = await resp.content.read(8192)
                                    if not chunk:
                                        break
                                    f.write(chunk)
                            
                            try:
                                # Get audio metadata using the instance method
                                self.duration = self.get_audio_metadata(self.downloaded_path)
                                self.last_accessed = time.time()
                                logging.info(f"Successfully downloaded and processed: {self.filename} (Bitrate: {self.bitrate}kbps)")
                                return
                            except Exception as metadata_error:
                                logging.error(f"Metadata extraction failed: {metadata_error}")
                                raise
                        else:
                            logging.warning(f"Download failed with status {resp.status}: {self.filename}")
            except Exception as e:
                logging.error(f"Download attempt {attempt + 1} failed for {self.filename}: {str(e)}")
                if os.path.exists(self.downloaded_path):
                    try:
                        os.remove(self.downloaded_path)
                    except Exception as remove_error:
                        logging.error(f"Error removing failed download {self.downloaded_path}: {remove_error}")
                if attempt == self.download_retries - 1:
                    raise

    def cleanup(self):
        """Remove the downloaded file and reset track state"""
        # Only delete the file if it's not a permanent library file
        if not self.is_permanent and self.downloaded_path and os.path.exists(self.downloaded_path):
            try:
                os.remove(self.downloaded_path)
                logging.info(f"Cleaned up file: {self.filename}")
            except Exception as e:
                logging.error(f"Error removing file {self.downloaded_path}: {e}")
        elif self.is_permanent:
            logging.debug(f"Skipping cleanup of permanent library file: {self.filename}")
        
        # Always reset the track state
        self.downloaded_path = None
        self.position = 0
        self.playback_start_time = None
        self.paused_position = 0
        self.is_paused = False

    def to_dict(self):
        """Convert track information to dictionary for display"""
        return {
            'filename': self.filename,
            'requester': self.requester,
            'duration': str(timedelta(seconds=int(self.duration))),
            'position': str(timedelta(seconds=int(self.get_current_position()))),
            'size_mb': self.file_size / (1024 * 1024),
            'volume': self.volume,
            'bitrate': f"{self.bitrate}kbps" if self.bitrate else "Unknown"
        }
    
    def start_playback(self, position=None):
        """Mark the track as started playing"""
        if position is not None:
            self.position = position
        self.playback_start_time = time.time() - self.position
        self.is_paused = False
        logging.debug(f"Started playback of {self.filename} at position {self.position}")
    
    def pause_playback(self):
        """Mark the track as paused and store current position"""
        if not self.is_paused and self.playback_start_time:
            self.paused_position = time.time() - self.playback_start_time
            self.is_paused = True
            logging.debug(f"Paused {self.filename} at position {self.paused_position}")
    
    def resume_playback(self):
        """Resume playback from paused position"""
        if self.is_paused:
            self.playback_start_time = time.time() - self.paused_position
            self.is_paused = False
            logging.debug(f"Resumed {self.filename} from position {self.paused_position}")
    
    def get_current_position(self):
        """Get the current playback position"""
        if not self.playback_start_time:
            return self.position
        
        if self.is_paused:
            return self.paused_position
        
        current_position = time.time() - self.playback_start_time
        # Ensure position doesn't exceed duration
        return min(current_position, self.duration)

class TrackManager:
    def __init__(self, config, bot=None):
        # Basic configuration
        self.max_queue_size = config['max_queue_size_mb'] * 1024 * 1024  # Convert to bytes
        self.temp_folder = config['temp_folder']
        self.default_volume = config.get('default_volume', 100)
        self.bot = bot  # Store bot reference if provided

        # Resource limits from config
        self.cleanup_interval = config['resource_limits']['cleanup_interval_minutes'] * 60  # Convert to seconds
        self.file_max_age = config['resource_limits']['inactive_timeout_minutes'] * 60  # Convert to seconds
        self.max_tracks = config['resource_limits']['max_tracks_per_guild']
        self.max_duration = config['resource_limits']['max_track_duration_minutes'] * 60  # Convert to seconds
        self.rate_limit = config['resource_limits']['rate_limit_seconds']

        # Internal state
        self.last_cleanup = time.time()
        self._active_files = set()  # Track currently active files
        
        # Ensure temp folder exists
        if not os.path.exists(self.temp_folder):
            os.makedirs(self.temp_folder)
            logging.info(f"Created temp folder: {self.temp_folder}")

    def mark_file_active(self, file_path):
        """Mark a file as currently active/in-use"""
        if file_path:
            self._active_files.add(file_path)
            logging.debug(f"Marked file as active: {os.path.basename(file_path)}")

    def mark_file_inactive(self, file_path):
        """Mark a file as no longer active/in-use"""
        if file_path:
            self._active_files.discard(file_path)
            logging.debug(f"Marked file as inactive: {os.path.basename(file_path)}")

    def get_queue_size(self, queue):
        """Calculate total size of all tracks in queue"""
        return sum(track.file_size for track in queue)

    def can_add_to_queue(self, queue, file_size):
        """Check if a new file can be added to queue without exceeding limits"""
        # Check queue size limit
        if (self.get_queue_size(queue) + file_size) > self.max_queue_size:
            logging.warning(f"Queue size limit reached: {self.max_queue_size/1024/1024}MB")
            return False
            
        # Check track count limit
        if len(queue) >= self.max_tracks:
            logging.warning(f"Maximum track count reached: {self.max_tracks}")
            return False
            
        return True

    async def validate_track(self, track):
        """Validate track duration and other constraints"""
        if track.duration > self.max_duration:
            raise ValueError(
                f"Track duration ({track.duration/60:.1f}min) exceeds limit "
                f"of {self.max_duration/60:.1f} minutes"
            )
        return True

    def get_queue_stats(self, queue):
        """Get queue statistics"""
        total_size = self.get_queue_size(queue)
        return {
            'current_size_mb': total_size / (1024 * 1024),
            'max_size_mb': self.max_queue_size / (1024 * 1024),
            'available_space_mb': (self.max_queue_size - total_size) / (1024 * 1024),
            'track_count': len(queue),
            'max_tracks': self.max_tracks,
            'tracks_remaining': self.max_tracks - len(queue)
        }

    async def cleanup_temp_files(self):
        """Clean up old temporary files"""
        current_time = time.time()
        
        # Only run cleanup if enough time has passed
        if current_time - self.last_cleanup < self.cleanup_interval:
            return

        self.last_cleanup = current_time
        cleaned_count = 0
        error_count = 0

        if os.path.exists(self.temp_folder):
            for file in os.listdir(self.temp_folder):
                file_path = os.path.join(self.temp_folder, file)
                try:
                    # Skip files that are marked as active
                    if file_path in self._active_files:
                        logging.debug(f"Skipping cleanup of in-use file: {file}")
                        continue

                    # Check file age
                    file_age = current_time - os.path.getctime(file_path)
                    if file_age > self.file_max_age:
                        os.remove(file_path)
                        cleaned_count += 1
                        logging.info(f"Cleaned up old file: {file}")
                        
                except Exception as e:
                    logging.error(f"Error cleaning up file {file}: {e}")
                    error_count += 1

            if cleaned_count > 0 or error_count > 0:
                logging.info(
                    f"Cleanup completed: {cleaned_count} files removed, "
                    f"{error_count} errors encountered"
                )

    async def ensure_temp_folder(self):
        """Ensure temporary folder exists and is writable"""
        if not os.path.exists(self.temp_folder):
            try:
                os.makedirs(self.temp_folder)
                logging.info(f"Created temporary folder: {self.temp_folder}")
            except Exception as e:
                logging.error(f"Error creating temp folder: {e}")
                raise

        # Test if folder is writable
        test_file = os.path.join(self.temp_folder, 'test_write')
        try:
            with open(test_file, 'w') as f:
                f.write('test')
            os.remove(test_file)
        except Exception as e:
            logging.error(f"Temp folder is not writable: {e}")
            raise

    async def initialize(self):
        """Initialize the track manager"""
        try:
            await self.ensure_temp_folder()
            await self.cleanup_temp_files()  # Initial cleanup
            logging.info("Track manager initialized successfully")
        except Exception as e:
            logging.error(f"Failed to initialize track manager: {e}")
            raise

    def is_rate_limited(self, last_action_time):
        """Check if an action should be rate limited"""
        return (time.time() - last_action_time) < self.rate_limit