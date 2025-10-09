import sqlite3
import logging
import time
import threading
import os
from typing import List, Optional

class DatabaseManager:
    def __init__(self, db_path: str = 'bot_settings.db'):
        self.db_path = db_path
        self.connection_lock = threading.Lock()
        self.init_database()

    def get_connection(self, timeout=20.0):
        """Get a database connection with proper timeout and settings"""
        connection = sqlite3.connect(self.db_path, timeout=timeout)
        connection.execute("PRAGMA journal_mode=WAL")  # Use Write-Ahead Logging for better concurrency
        connection.execute("PRAGMA busy_timeout=10000")  # Set busy timeout to 10 seconds
        return connection

    def init_database(self):
        """Initialize database tables"""
        try:
            with self.connection_lock:
                with self.get_connection() as conn:
                    cursor = conn.cursor()
                    
                    # Create guild settings table
                    cursor.execute('''
                        CREATE TABLE IF NOT EXISTS guild_settings (
                            guild_id INTEGER PRIMARY KEY,
                            autoplay_enabled BOOLEAN DEFAULT 1,
                            autodisconnect_enabled BOOLEAN DEFAULT 1
                        )
                    ''')
                    
                    # Check if columns exist, add them if they don't
                    cursor.execute("PRAGMA table_info(guild_settings)")
                    columns = [column[1] for column in cursor.fetchall()]
                    
                    if 'playback_speed' not in columns:
                        cursor.execute('''
                            ALTER TABLE guild_settings
                            ADD COLUMN playback_speed INTEGER DEFAULT 100
                        ''')
                        logging.info("Added playback_speed column to guild_settings table")
                    
                    if 'upload_count' not in columns:
                        cursor.execute('''
                            ALTER TABLE guild_settings
                            ADD COLUMN upload_count INTEGER DEFAULT 0
                        ''')
                        logging.info("Added upload_count column to guild_settings table")
                    
                    if 'last_rating_request' not in columns:
                        cursor.execute('''
                            ALTER TABLE guild_settings
                            ADD COLUMN last_rating_request INTEGER DEFAULT 0
                        ''')
                        logging.info("Added last_rating_request column to guild_settings table")
                    
                    # Create blacklisted users table
                    cursor.execute('''
                        CREATE TABLE IF NOT EXISTS blacklisted_users (
                            guild_id INTEGER,
                            user_id INTEGER,
                            PRIMARY KEY (guild_id, user_id)
                        )
                    ''')
                    
                    # Create whitelisted roles table
                    cursor.execute('''
                        CREATE TABLE IF NOT EXISTS whitelisted_roles (
                            guild_id INTEGER,
                            role_id INTEGER,
                            PRIMARY KEY (guild_id, role_id)
                        )
                    ''')
                    
                    # Create persistent audio tracks table
                    cursor.execute('''
                        CREATE TABLE IF NOT EXISTS persistent_tracks (
                            guild_id INTEGER,
                            track_name TEXT,
                            filename TEXT,
                            file_path TEXT,
                            uploaded_by TEXT,
                            upload_date INTEGER,
                            file_size INTEGER,
                            PRIMARY KEY (guild_id, track_name)
                        )
                    ''')
                    
                    # Create table to track guild storage usage
                    cursor.execute('''
                        CREATE TABLE IF NOT EXISTS guild_storage (
                            guild_id INTEGER PRIMARY KEY,
                            used_storage INTEGER DEFAULT 0,
                            max_storage INTEGER DEFAULT 104857600
                        )
                    ''')
                    
                    conn.commit()
                    logging.info("Database initialized successfully")
        except Exception as e:
            logging.error(f"Error initializing database: {e}")
            raise

    # Database operation with retry logic
    def execute_with_retry(self, operation, params=None, max_retries=5, initial_delay=0.1):
        """Execute a database operation with retry logic"""
        retry_count = 0
        last_error = None
        
        while retry_count < max_retries:
            try:
                with self.connection_lock:
                    with self.get_connection() as conn:
                        cursor = conn.cursor()
                        if params:
                            result = operation(conn, cursor, params)
                        else:
                            result = operation(conn, cursor)
                        conn.commit()
                        return result
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e):
                    retry_count += 1
                    last_error = e
                    # Exponential backoff
                    sleep_time = initial_delay * (2 ** retry_count)
                    logging.warning(f"Database locked, retrying in {sleep_time:.2f}s (attempt {retry_count}/{max_retries})")
                    time.sleep(sleep_time)
                else:
                    # Other operational error
                    logging.error(f"Database error: {e}")
                    raise
            except Exception as e:
                logging.error(f"Error in database operation: {e}")
                raise
        
        # If we get here, we've exceeded our retries
        logging.error(f"Failed to execute database operation after {max_retries} retries: {last_error}")
        raise last_error

    # NEW FILE VALIDATION METHODS
    def validate_persistent_files(self):
        """Validate and clean up persistent file entries"""
        try:
            with self.connection_lock:
                with self.get_connection() as conn:
                    cursor = conn.cursor()
                    
                    # Get all persistent tracks
                    cursor.execute("SELECT guild_id, track_name, file_path, file_size FROM persistent_tracks")
                    tracks = cursor.fetchall()
                    
                    orphaned_entries = []
                    total_freed_space = 0
                    
                    for guild_id, track_name, file_path, file_size in tracks:
                        if not os.path.exists(file_path):
                            logging.warning(f"Orphaned database entry found: {track_name} -> {file_path}")
                            orphaned_entries.append((guild_id, track_name, file_size))
                            total_freed_space += file_size
                    
                    # Remove orphaned entries
                    for guild_id, track_name, file_size in orphaned_entries:
                        cursor.execute(
                            "DELETE FROM persistent_tracks WHERE guild_id = ? AND track_name = ?",
                            (guild_id, track_name)
                        )
                        
                        # Update storage usage
                        cursor.execute('''
                            UPDATE guild_storage 
                            SET used_storage = MAX(0, used_storage - ?) 
                            WHERE guild_id = ?
                        ''', (file_size, guild_id))
                    
                    conn.commit()
                    
                    if orphaned_entries:
                        logging.info(f"Cleaned up {len(orphaned_entries)} orphaned file entries, freed {total_freed_space / (1024*1024):.2f}MB")
                    
                    return len(orphaned_entries)
                    
        except Exception as e:
            logging.error(f"Error validating persistent files: {e}")
            return 0

    def verify_file_before_play(self, guild_id: int, track_name: str):
        """Verify file exists before attempting to play"""
        try:
            track_info = self.get_persistent_track(guild_id, track_name)
            if not track_info:
                return None, "Track not found in database"
            
            file_path = track_info["file_path"]
            if not os.path.exists(file_path):
                # File missing - remove from database
                logging.warning(f"File missing for track '{track_name}': {file_path}")
                self.remove_persistent_track(guild_id, track_name)
                return None, f"File missing for track '{track_name}'. Entry removed from library."
            
            # Check if file is readable
            try:
                with open(file_path, 'rb') as f:
                    f.read(1)  # Try to read first byte
            except Exception as e:
                logging.error(f"File not readable for track '{track_name}': {e}")
                return None, f"File corrupted for track '{track_name}'. Please re-upload."
            
            return track_info, None
            
        except Exception as e:
            logging.error(f"Error verifying file: {e}")
            return None, "Error verifying file"

    # Autoplay settings
    def get_autoplay_setting(self, guild_id: int) -> bool:
        """Get autoplay setting for a guild"""
        try:
            def operation(conn, cursor, params):
                cursor.execute(
                    "SELECT autoplay_enabled FROM guild_settings WHERE guild_id = ?",
                    (params,)
                )
                result = cursor.fetchone()
                return bool(result[0]) if result else True
            
            return self.execute_with_retry(operation, guild_id)
        except Exception as e:
            logging.error(f"Error getting autoplay setting: {e}")
            return True

    def set_autoplay_setting(self, guild_id: int, enabled: bool):
        """Set autoplay setting for a guild"""
        try:
            def operation(conn, cursor, params):
                guild_id, enabled = params
                cursor.execute('''
                    INSERT INTO guild_settings (guild_id, autoplay_enabled)
                    VALUES (?, ?)
                    ON CONFLICT(guild_id) DO UPDATE SET autoplay_enabled = ?
                ''', (guild_id, enabled, enabled))
            
            self.execute_with_retry(operation, (guild_id, enabled))
        except Exception as e:
            logging.error(f"Error setting autoplay: {e}")
            raise

    # Playback Speed settings
    def get_playback_speed(self, guild_id: int) -> int:
        """Get playback speed setting for a guild (percentage: 100 = normal speed)"""
        try:
            def operation(conn, cursor, params):
                cursor.execute(
                    "SELECT playback_speed FROM guild_settings WHERE guild_id = ?",
                    (params,)
                )
                result = cursor.fetchone()
                return result[0] if result and result[0] is not None else 100
            
            return self.execute_with_retry(operation, guild_id)
        except Exception as e:
            logging.error(f"Error getting playback speed setting: {e}")
            return 100

    def set_playback_speed(self, guild_id: int, speed: int):
        """Set playback speed setting for a guild (percentage: 100 = normal speed)"""
        try:
            def operation(conn, cursor, params):
                guild_id, speed = params
                cursor.execute('''
                    INSERT INTO guild_settings (guild_id, playback_speed)
                    VALUES (?, ?)
                    ON CONFLICT(guild_id) DO UPDATE SET playback_speed = ?
                ''', (guild_id, speed, speed))
            
            self.execute_with_retry(operation, (guild_id, speed))
        except Exception as e:
            logging.error(f"Error setting playback speed: {e}")
            raise
    
    # Upload count and rating request tracking
    def get_upload_count(self, guild_id: int) -> int:
        """Get upload count for a guild"""
        try:
            def operation(conn, cursor, params):
                cursor.execute(
                    "SELECT upload_count FROM guild_settings WHERE guild_id = ?",
                    (params,)
                )
                result = cursor.fetchone()
                return result[0] if result and result[0] is not None else 0
            
            return self.execute_with_retry(operation, guild_id)
        except Exception as e:
            logging.error(f"Error getting upload count: {e}")
            return 0

    def increment_upload_count(self, guild_id: int, amount: int = 1):
        """Increment upload count for a guild"""
        try:
            current_count = self.get_upload_count(guild_id)
            new_count = current_count + amount
            
            def operation(conn, cursor, params):
                guild_id, new_count = params
                cursor.execute('''
                    INSERT INTO guild_settings (guild_id, upload_count)
                    VALUES (?, ?)
                    ON CONFLICT(guild_id) DO UPDATE SET upload_count = ?
                ''', (guild_id, new_count, new_count))
            
            self.execute_with_retry(operation, (guild_id, new_count))
        except Exception as e:
            logging.error(f"Error incrementing upload count: {e}")

    def reset_upload_count(self, guild_id: int):
        """Reset upload count for a guild"""
        try:
            def operation(conn, cursor, params):
                cursor.execute('''
                    INSERT INTO guild_settings (guild_id, upload_count)
                    VALUES (?, 0)
                    ON CONFLICT(guild_id) DO UPDATE SET upload_count = 0
                ''', (params,))
            
            self.execute_with_retry(operation, guild_id)
        except Exception as e:
            logging.error(f"Error resetting upload count: {e}")

    def get_last_rating_request(self, guild_id: int) -> int:
        """Get timestamp of last rating request for a guild"""
        try:
            def operation(conn, cursor, params):
                cursor.execute(
                    "SELECT last_rating_request FROM guild_settings WHERE guild_id = ?",
                    (params,)
                )
                result = cursor.fetchone()
                return result[0] if result and result[0] is not None else 0
            
            return self.execute_with_retry(operation, guild_id)
        except Exception as e:
            logging.error(f"Error getting last rating request timestamp: {e}")
            return 0

    def update_last_rating_request(self, guild_id: int, timestamp: int):
        """Update timestamp of last rating request for a guild"""
        try:
            def operation(conn, cursor, params):
                guild_id, timestamp = params
                cursor.execute('''
                    INSERT INTO guild_settings (guild_id, last_rating_request)
                    VALUES (?, ?)
                    ON CONFLICT(guild_id) DO UPDATE SET last_rating_request = ?
                ''', (guild_id, timestamp, timestamp))
            
            self.execute_with_retry(operation, (guild_id, timestamp))
        except Exception as e:
            logging.error(f"Error updating last rating request timestamp: {e}")

    # Blacklist management
    def add_to_blacklist(self, guild_id: int, user_id: int):
        """Add a user to the blacklist"""
        try:
            def operation(conn, cursor, params):
                guild_id, user_id = params
                cursor.execute(
                    "INSERT OR IGNORE INTO blacklisted_users (guild_id, user_id) VALUES (?, ?)",
                    (guild_id, user_id)
                )
            
            self.execute_with_retry(operation, (guild_id, user_id))
        except Exception as e:
            logging.error(f"Error adding user to blacklist: {e}")
            raise

    def remove_from_blacklist(self, guild_id: int, user_id: int):
        """Remove a user from the blacklist"""
        try:
            def operation(conn, cursor, params):
                guild_id, user_id = params
                cursor.execute(
                    "DELETE FROM blacklisted_users WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id)
                )
            
            self.execute_with_retry(operation, (guild_id, user_id))
        except Exception as e:
            logging.error(f"Error removing user from blacklist: {e}")
            raise

    def is_user_blacklisted(self, guild_id: int, user_id: int) -> bool:
        """Check if a user is blacklisted"""
        try:
            def operation(conn, cursor, params):
                guild_id, user_id = params
                cursor.execute(
                    "SELECT 1 FROM blacklisted_users WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id)
                )
                return cursor.fetchone() is not None
            
            return self.execute_with_retry(operation, (guild_id, user_id))
        except Exception as e:
            logging.error(f"Error checking blacklist: {e}")
            return False

    def get_blacklisted_users(self, guild_id: int) -> List[int]:
        """Get all blacklisted users for a guild"""
        try:
            def operation(conn, cursor, params):
                cursor.execute(
                    "SELECT user_id FROM blacklisted_users WHERE guild_id = ?",
                    (params,)
                )
                return [row[0] for row in cursor.fetchall()]
            
            return self.execute_with_retry(operation, guild_id)
        except Exception as e:
            logging.error(f"Error getting blacklisted users: {e}")
            return []

    # Role whitelist management
    def add_to_role_whitelist(self, guild_id: int, role_id: int):
        """Add a role to the whitelist"""
        try:
            def operation(conn, cursor, params):
                guild_id, role_id = params
                cursor.execute(
                    "INSERT OR IGNORE INTO whitelisted_roles (guild_id, role_id) VALUES (?, ?)",
                    (guild_id, role_id)
                )
            
            self.execute_with_retry(operation, (guild_id, role_id))
        except Exception as e:
            logging.error(f"Error adding role to whitelist: {e}")
            raise

    def remove_from_role_whitelist(self, guild_id: int, role_id: int):
        """Remove a role from the whitelist"""
        try:
            def operation(conn, cursor, params):
                guild_id, role_id = params
                cursor.execute(
                    "DELETE FROM whitelisted_roles WHERE guild_id = ? AND role_id = ?",
                    (guild_id, role_id)
                )
            
            self.execute_with_retry(operation, (guild_id, role_id))
        except Exception as e:
            logging.error(f"Error removing role from whitelist: {e}")
            raise

    def get_whitelisted_roles(self, guild_id: int) -> List[int]:
        """Get all whitelisted roles for a guild"""
        try:
            def operation(conn, cursor, params):
                cursor.execute(
                    "SELECT role_id FROM whitelisted_roles WHERE guild_id = ?",
                    (params,)
                )
                return [row[0] for row in cursor.fetchall()]
            
            return self.execute_with_retry(operation, guild_id)
        except Exception as e:
            logging.error(f"Error getting whitelisted roles: {e}")
            return []

    def has_whitelisted_roles(self, guild_id: int) -> bool:
        """Check if a guild has any whitelisted roles"""
        try:
            def operation(conn, cursor, params):
                cursor.execute(
                    "SELECT 1 FROM whitelisted_roles WHERE guild_id = ? LIMIT 1",
                    (params,)
                )
                return cursor.fetchone() is not None
            
            return self.execute_with_retry(operation, guild_id)
        except Exception as e:
            logging.error(f"Error checking whitelisted roles: {e}")
            return False

    def get_autodisconnect_setting(self, guild_id: int) -> bool:
        """Get autodisconnect setting for a guild"""
        try:
            def operation(conn, cursor, params):
                cursor.execute(
                    "SELECT autodisconnect_enabled FROM guild_settings WHERE guild_id = ?",
                    (params,)
                )
                result = cursor.fetchone()
                return bool(result[0]) if result else True
            
            return self.execute_with_retry(operation, guild_id)
        except Exception as e:
            logging.error(f"Error getting autodisconnect setting: {e}")
            return True

    def set_autodisconnect_setting(self, guild_id: int, enabled: bool):
        """Set autodisconnect setting for a guild"""
        try:
            def operation(conn, cursor, params):
                guild_id, enabled = params
                cursor.execute('''
                    INSERT INTO guild_settings (guild_id, autodisconnect_enabled)
                    VALUES (?, ?)
                    ON CONFLICT(guild_id) DO UPDATE SET autodisconnect_enabled = ?
                ''', (guild_id, enabled, enabled))
            
            self.execute_with_retry(operation, (guild_id, enabled))
        except Exception as e:
            logging.error(f"Error setting autodisconnect: {e}")
            raise
            
    # Persistent track management
    def add_persistent_track(self, guild_id: int, track_name: str, filename: str, 
                          file_path: str, uploaded_by: str, file_size: int):
        """Add a persistent track to the database"""
        try:
            upload_date = int(time.time())
            
            def operation(conn, cursor, params):
                guild_id, track_name, filename, file_path, uploaded_by, upload_date, file_size = params
                cursor.execute('''
                    INSERT OR REPLACE INTO persistent_tracks 
                    (guild_id, track_name, filename, file_path, uploaded_by, upload_date, file_size)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (guild_id, track_name, filename, file_path, uploaded_by, upload_date, file_size))
            
            self.execute_with_retry(operation, (guild_id, track_name, filename, file_path, uploaded_by, upload_date, file_size))
        except Exception as e:
            logging.error(f"Error adding persistent track: {e}")
            raise

    def remove_persistent_track(self, guild_id: int, track_name: str):
        """Remove a persistent track from the database"""
        try:
            # This operation needs to be atomic - get file info and update storage in one transaction
            def operation(conn, cursor, params):
                guild_id, track_name = params
                
                # First get the file path to delete the actual file
                cursor.execute(
                    "SELECT file_path, file_size FROM persistent_tracks WHERE guild_id = ? AND track_name = ?",
                    (guild_id, track_name)
                )
                result = cursor.fetchone()
                
                if not result:
                    return None
                    
                file_path, file_size = result
                
                # Remove from database
                cursor.execute(
                    "DELETE FROM persistent_tracks WHERE guild_id = ? AND track_name = ?",
                    (guild_id, track_name)
                )
                
                # Update storage usage
                cursor.execute('''
                    UPDATE guild_storage 
                    SET used_storage = MAX(0, used_storage - ?) 
                    WHERE guild_id = ?
                ''', (file_size, guild_id))
                
                return file_path
            
            return self.execute_with_retry(operation, (guild_id, track_name))
        except Exception as e:
            logging.error(f"Error removing persistent track: {e}")
            raise

    def get_persistent_track(self, guild_id: int, track_name: str):
        """Get a persistent track from the database"""
        try:
            def operation(conn, cursor, params):
                guild_id, track_name = params
                cursor.execute(
                    "SELECT * FROM persistent_tracks WHERE guild_id = ? AND track_name = ?",
                    (guild_id, track_name)
                )
                result = cursor.fetchone()
                if result:
                    return {
                        "guild_id": result[0],
                        "track_name": result[1],
                        "filename": result[2],
                        "file_path": result[3],
                        "uploaded_by": result[4],
                        "upload_date": result[5],
                        "file_size": result[6]
                    }
                return None
            
            return self.execute_with_retry(operation, (guild_id, track_name))
        except Exception as e:
            logging.error(f"Error getting persistent track: {e}")
            return None

    def list_persistent_tracks(self, guild_id: int):
        """List all persistent tracks for a guild"""
        try:
            def operation(conn, cursor, params):
                cursor.execute(
                    "SELECT track_name, filename, uploaded_by, upload_date, file_size FROM persistent_tracks WHERE guild_id = ? ORDER BY track_name",
                    (params,)
                )
                tracks = cursor.fetchall()
                return [
                    {
                        "track_name": track[0],
                        "filename": track[1],
                        "uploaded_by": track[2],
                        "upload_date": track[3],
                        "file_size": track[4]
                    }
                    for track in tracks
                ]
            
            return self.execute_with_retry(operation, guild_id)
        except Exception as e:
            logging.error(f"Error listing persistent tracks: {e}")
            return []

    # Guild storage management
    def get_guild_storage(self, guild_id: int):
        """Get current storage usage for a guild"""
        try:
            def operation(conn, cursor, params):
                cursor.execute(
                    "SELECT used_storage, max_storage FROM guild_storage WHERE guild_id = ?",
                    (params,)
                )
                result = cursor.fetchone()
                if result:
                    return {"used": result[0], "max": result[1]}
                else:
                    # Default: 0 used, 100MB max (104857600 bytes)
                    return {"used": 0, "max": 104857600}
            
            return self.execute_with_retry(operation, guild_id)
        except Exception as e:
            logging.error(f"Error getting guild storage: {e}")
            return {"used": 0, "max": 104857600}

    def increase_guild_storage(self, guild_id: int, size: int):
        """Increase storage usage for a guild"""
        try:
            def operation(conn, cursor, params):
                guild_id, size = params
                cursor.execute('''
                    INSERT INTO guild_storage (guild_id, used_storage)
                    VALUES (?, ?)
                    ON CONFLICT(guild_id) DO UPDATE SET used_storage = used_storage + ?
                ''', (guild_id, size, size))
            
            self.execute_with_retry(operation, (guild_id, size))
        except Exception as e:
            logging.error(f"Error updating guild storage: {e}")
            raise

    def decrease_guild_storage(self, guild_id: int, size: int):
        """Decrease storage usage for a guild"""
        try:
            def operation(conn, cursor, params):
                guild_id, size = params
                cursor.execute('''
                    UPDATE guild_storage 
                    SET used_storage = MAX(0, used_storage - ?) 
                    WHERE guild_id = ?
                ''', (size, guild_id))
                
                # If no rows were updated, insert a new record with 0 usage
                if cursor.rowcount == 0:
                    cursor.execute('''
                        INSERT OR IGNORE INTO guild_storage (guild_id, used_storage)
                        VALUES (?, 0)
                    ''', (guild_id,))
            
            self.execute_with_retry(operation, (guild_id, size))
        except Exception as e:
            logging.error(f"Error updating guild storage: {e}")
            raise

    def can_add_to_storage(self, guild_id: int, size: int):
        """Check if a file can be added to guild storage"""
        storage = self.get_guild_storage(guild_id)
        return (storage["used"] + size) <= storage["max"]
