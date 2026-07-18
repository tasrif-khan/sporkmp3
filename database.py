"""
Database management for the music bot.
Handles guild settings, blacklists, whitelists, and persistent tracks.
"""
import sqlite3
import logging
import time
import threading
import os
from typing import List, Optional, Dict, Any
from contextlib import contextmanager


class Database:
    """SQLite database manager with connection pooling and retry logic"""
    
    def __init__(self, db_path: str = 'bot_settings.db', max_storage_mb: int = 100):
        self.db_path = db_path
        self.max_storage = max_storage_mb * 1024 * 1024  # Convert to bytes
        self._lock = threading.Lock()
        self._init_tables()
    
    @contextmanager
    def _connection(self, timeout: float = 20.0):
        """Context manager for database connections"""
        conn = sqlite3.connect(self.db_path, timeout=timeout)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()
    
    def _execute(self, query: str, params: tuple = (), fetch: str = None, retries: int = 5):
        """Execute query with retry logic"""
        for attempt in range(retries):
            try:
                with self._lock, self._connection() as conn:
                    cursor = conn.execute(query, params)
                    if fetch == 'one':
                        return cursor.fetchone()
                    elif fetch == 'all':
                        return cursor.fetchall()
                    return cursor.lastrowid
            except sqlite3.OperationalError as e:
                if "locked" in str(e) and attempt < retries - 1:
                    time.sleep(0.1 * (2 ** attempt))
                    continue
                raise
    
    def _init_tables(self):
        """Initialize all database tables"""
        tables = f"""
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                autoplay_enabled BOOLEAN DEFAULT 1,
                autodisconnect_enabled BOOLEAN DEFAULT 1,
                playback_speed INTEGER DEFAULT 100,
                upload_count INTEGER DEFAULT 0,
                last_rating_request INTEGER DEFAULT 0
            );
            
            CREATE TABLE IF NOT EXISTS blacklisted_users (
                guild_id INTEGER,
                user_id INTEGER,
                PRIMARY KEY (guild_id, user_id)
            );
            
            CREATE TABLE IF NOT EXISTS whitelisted_roles (
                guild_id INTEGER,
                role_id INTEGER,
                PRIMARY KEY (guild_id, role_id)
            );
            
            CREATE TABLE IF NOT EXISTS persistent_tracks (
                guild_id INTEGER,
                track_name TEXT,
                filename TEXT,
                file_path TEXT,
                uploaded_by TEXT,
                upload_date INTEGER,
                file_size INTEGER,
                PRIMARY KEY (guild_id, track_name)
            );
            
            CREATE TABLE IF NOT EXISTS guild_storage (
                guild_id INTEGER PRIMARY KEY,
                used_storage INTEGER DEFAULT 0,
                max_storage INTEGER DEFAULT {self.max_storage}
            );
            
            CREATE TABLE IF NOT EXISTS playlists (
                guild_id INTEGER,
                playlist_name TEXT,
                created_by TEXT,
                created_at INTEGER,
                description TEXT DEFAULT '',
                PRIMARY KEY (guild_id, playlist_name)
            );
            
            CREATE TABLE IF NOT EXISTS playlist_tracks (
                guild_id INTEGER,
                playlist_name TEXT,
                track_name TEXT,
                position INTEGER,
                added_by TEXT,
                added_at INTEGER,
                PRIMARY KEY (guild_id, playlist_name, track_name),
                FOREIGN KEY (guild_id, playlist_name) REFERENCES playlists(guild_id, playlist_name) ON DELETE CASCADE,
                FOREIGN KEY (guild_id, track_name) REFERENCES persistent_tracks(guild_id, track_name) ON DELETE CASCADE
            );
        """
        for statement in tables.strip().split(';'):
            if statement.strip():
                self._execute(statement)
        self._migrate()
        logging.info("Database initialized")

    def _migrate(self):
        """Add columns that were introduced after the initial schema."""
        migrations = [
            "ALTER TABLE guild_settings ADD COLUMN playback_speed INTEGER DEFAULT 100",
            "ALTER TABLE guild_settings ADD COLUMN last_rating_request INTEGER DEFAULT 0",
        ]
        for sql in migrations:
            try:
                self._execute(sql)
            except sqlite3.OperationalError:
                pass  # Column already exists
    
    # ========== Guild Settings ==========
    
    def _ensure_guild(self, guild_id: int):
        """Ensure guild exists in settings table"""
        self._execute(
            "INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)",
            (guild_id,)
        )
    
    def _get_setting(self, guild_id: int, column: str, default: Any) -> Any:
        """Get a guild setting value"""
        self._ensure_guild(guild_id)
        row = self._execute(
            f"SELECT {column} FROM guild_settings WHERE guild_id = ?",
            (guild_id,), fetch='one'
        )
        return row[0] if row else default
    
    def _set_setting(self, guild_id: int, column: str, value: Any):
        """Set a guild setting value"""
        self._ensure_guild(guild_id)
        self._execute(
            f"UPDATE guild_settings SET {column} = ? WHERE guild_id = ?",
            (value, guild_id)
        )
    
    # Autoplay
    def get_autoplay(self, guild_id: int) -> bool:
        return bool(self._get_setting(guild_id, 'autoplay_enabled', True))
    
    def set_autoplay(self, guild_id: int, enabled: bool):
        self._set_setting(guild_id, 'autoplay_enabled', enabled)
    
    # Autodisconnect
    def get_autodisconnect(self, guild_id: int) -> bool:
        return bool(self._get_setting(guild_id, 'autodisconnect_enabled', True))
    
    def set_autodisconnect(self, guild_id: int, enabled: bool):
        self._set_setting(guild_id, 'autodisconnect_enabled', enabled)
    
    # Playback speed
    def get_speed(self, guild_id: int) -> int:
        return self._get_setting(guild_id, 'playback_speed', 100)
    
    def set_speed(self, guild_id: int, speed: int):
        self._set_setting(guild_id, 'playback_speed', max(50, min(200, speed)))
    
    # Upload tracking
    def get_upload_count(self, guild_id: int) -> int:
        return self._get_setting(guild_id, 'upload_count', 0)
    
    def increment_uploads(self, guild_id: int, count: int = 1):
        self._ensure_guild(guild_id)
        self._execute(
            "UPDATE guild_settings SET upload_count = upload_count + ? WHERE guild_id = ?",
            (count, guild_id)
        )
    
    def reset_uploads(self, guild_id: int):
        self._set_setting(guild_id, 'upload_count', 0)
    
    def get_last_rating_request(self, guild_id: int) -> int:
        return self._get_setting(guild_id, 'last_rating_request', 0)
    
    def set_last_rating_request(self, guild_id: int, timestamp: int):
        self._set_setting(guild_id, 'last_rating_request', timestamp)
    
    # ========== Blacklist ==========
    
    def is_blacklisted(self, guild_id: int, user_id: int) -> bool:
        row = self._execute(
            "SELECT 1 FROM blacklisted_users WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id), fetch='one'
        )
        return row is not None
    
    def add_blacklist(self, guild_id: int, user_id: int):
        self._execute(
            "INSERT OR IGNORE INTO blacklisted_users (guild_id, user_id) VALUES (?, ?)",
            (guild_id, user_id)
        )
    
    def remove_blacklist(self, guild_id: int, user_id: int):
        self._execute(
            "DELETE FROM blacklisted_users WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id)
        )

    def get_blacklisted_users(self, guild_id: int) -> List[int]:
        rows = self._execute(
            "SELECT user_id FROM blacklisted_users WHERE guild_id = ?",
            (guild_id,), fetch='all'
        )
        return [row[0] for row in rows] if rows else []
    
    # ========== Role Whitelist ==========
    
    def get_whitelisted_roles(self, guild_id: int) -> List[int]:
        rows = self._execute(
            "SELECT role_id FROM whitelisted_roles WHERE guild_id = ?",
            (guild_id,), fetch='all'
        )
        return [row[0] for row in rows] if rows else []
    
    def add_role_whitelist(self, guild_id: int, role_id: int):
        self._execute(
            "INSERT OR IGNORE INTO whitelisted_roles (guild_id, role_id) VALUES (?, ?)",
            (guild_id, role_id)
        )
    
    def remove_role_whitelist(self, guild_id: int, role_id: int):
        self._execute(
            "DELETE FROM whitelisted_roles WHERE guild_id = ? AND role_id = ?",
            (guild_id, role_id)
        )
    
    # ========== Persistent Tracks ==========
    
    def add_track(self, guild_id: int, name: str, filename: str, path: str, uploader: str, size: int):
        """Add a persistent track"""
        self._execute(
            """INSERT OR REPLACE INTO persistent_tracks 
               (guild_id, track_name, filename, file_path, uploaded_by, upload_date, file_size)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (guild_id, name, filename, path, uploader, int(time.time()), size)
        )
        self._update_storage(guild_id, size)
    
    def remove_track(self, guild_id: int, name: str) -> Optional[str]:
        """Remove a persistent track, returns file path"""
        row = self._execute(
            "SELECT file_path, file_size FROM persistent_tracks WHERE guild_id = ? AND track_name = ?",
            (guild_id, name), fetch='one'
        )
        if not row:
            return None
        
        self._execute(
            "DELETE FROM persistent_tracks WHERE guild_id = ? AND track_name = ?",
            (guild_id, name)
        )
        self._update_storage(guild_id, -row['file_size'])
        return row['file_path']
    
    def get_track(self, guild_id: int, name: str) -> Optional[Dict]:
        """Get a persistent track by name"""
        row = self._execute(
            "SELECT * FROM persistent_tracks WHERE guild_id = ? AND track_name = ?",
            (guild_id, name), fetch='one'
        )
        return dict(row) if row else None
    
    def list_tracks(self, guild_id: int) -> List[Dict]:
        """List all persistent tracks for a guild"""
        rows = self._execute(
            """SELECT track_name, filename, uploaded_by, upload_date, file_size 
               FROM persistent_tracks WHERE guild_id = ? ORDER BY track_name""",
            (guild_id,), fetch='all'
        )
        return [dict(row) for row in rows] if rows else []
    
    # ========== Storage Management ==========
    
    def _update_storage(self, guild_id: int, delta: int):
        """Update guild storage usage"""
        self._execute(
            """INSERT INTO guild_storage (guild_id, used_storage) VALUES (?, MAX(0, ?))
               ON CONFLICT(guild_id) DO UPDATE SET used_storage = MAX(0, used_storage + ?)""",
            (guild_id, delta, delta)
        )
    
    def get_storage(self, guild_id: int) -> Dict[str, int]:
        """Get guild storage usage and ensure max_storage is up to date"""
        row = self._execute(
            "SELECT used_storage, max_storage FROM guild_storage WHERE guild_id = ?",
            (guild_id,), fetch='one'
        )
        if row:
            # Update max_storage if it differs from current config
            if row['max_storage'] != self.max_storage:
                self._execute(
                    "UPDATE guild_storage SET max_storage = ? WHERE guild_id = ?",
                    (self.max_storage, guild_id)
                )
                logging.info(f"Updated max_storage for guild {guild_id} to {self.max_storage / 1024 / 1024}MB")
                return {'used': row['used_storage'], 'max': self.max_storage}
            return {'used': row['used_storage'], 'max': row['max_storage']}
        return {'used': 0, 'max': self.max_storage}
    
    def can_store(self, guild_id: int, size: int) -> bool:
        """Check if file can be stored"""
        storage = self.get_storage(guild_id)
        return storage['used'] + size <= storage['max']
    
    def update_all_storage_limits(self) -> int:
        """Update max_storage for all guilds to match current config"""
        count = self._execute(
            "UPDATE guild_storage SET max_storage = ? WHERE max_storage != ?",
            (self.max_storage, self.max_storage)
        )
        if count and count > 0:
            logging.info(f"Updated storage limits for {count} guild(s) to {self.max_storage / 1024 / 1024}MB")
        return count if count else 0
    
    # ========== Playlists ==========
    
    def create_playlist(self, guild_id: int, name: str, created_by: str, description: str = '') -> bool:
        """Create a new playlist. Returns False if it already exists."""
        existing = self._execute(
            "SELECT 1 FROM playlists WHERE guild_id = ? AND playlist_name = ?",
            (guild_id, name), fetch='one'
        )
        if existing:
            return False
        self._execute(
            "INSERT INTO playlists (guild_id, playlist_name, created_by, created_at, description) VALUES (?, ?, ?, ?, ?)",
            (guild_id, name, created_by, int(time.time()), description)
        )
        return True
    
    def delete_playlist(self, guild_id: int, name: str) -> bool:
        """Delete a playlist and its tracks. Returns False if not found."""
        existing = self._execute(
            "SELECT 1 FROM playlists WHERE guild_id = ? AND playlist_name = ?",
            (guild_id, name), fetch='one'
        )
        if not existing:
            return False
        self._execute(
            "DELETE FROM playlist_tracks WHERE guild_id = ? AND playlist_name = ?",
            (guild_id, name)
        )
        self._execute(
            "DELETE FROM playlists WHERE guild_id = ? AND playlist_name = ?",
            (guild_id, name)
        )
        return True
    
    def get_playlist(self, guild_id: int, name: str) -> Optional[Dict]:
        """Get playlist info"""
        row = self._execute(
            "SELECT * FROM playlists WHERE guild_id = ? AND playlist_name = ?",
            (guild_id, name), fetch='one'
        )
        return dict(row) if row else None
    
    def list_playlists(self, guild_id: int) -> List[Dict]:
        """List all playlists for a guild"""
        rows = self._execute(
            "SELECT p.*, COUNT(pt.track_name) as track_count FROM playlists p "
            "LEFT JOIN playlist_tracks pt ON p.guild_id = pt.guild_id AND p.playlist_name = pt.playlist_name "
            "WHERE p.guild_id = ? GROUP BY p.playlist_name ORDER BY p.playlist_name",
            (guild_id,), fetch='all'
        )
        return [dict(row) for row in rows] if rows else []
    
    def add_to_playlist(self, guild_id: int, playlist_name: str, track_name: str, added_by: str) -> Optional[str]:
        """Add a library track to a playlist. Returns error string or None on success."""
        # Check playlist exists
        if not self.get_playlist(guild_id, playlist_name):
            return "Playlist not found"
        # Check track exists in library
        if not self.get_track(guild_id, track_name):
            return "Track not found in library"
        # Check not already in playlist
        existing = self._execute(
            "SELECT 1 FROM playlist_tracks WHERE guild_id = ? AND playlist_name = ? AND track_name = ?",
            (guild_id, playlist_name, track_name), fetch='one'
        )
        if existing:
            return "Track already in playlist"
        # Get next position
        row = self._execute(
            "SELECT COALESCE(MAX(position), 0) + 1 as next_pos FROM playlist_tracks WHERE guild_id = ? AND playlist_name = ?",
            (guild_id, playlist_name), fetch='one'
        )
        pos = row['next_pos'] if row else 1
        self._execute(
            "INSERT INTO playlist_tracks (guild_id, playlist_name, track_name, position, added_by, added_at) VALUES (?, ?, ?, ?, ?, ?)",
            (guild_id, playlist_name, track_name, pos, added_by, int(time.time()))
        )
        return None
    
    def remove_from_playlist(self, guild_id: int, playlist_name: str, track_name: str) -> bool:
        """Remove a track from a playlist. Returns False if not found."""
        existing = self._execute(
            "SELECT 1 FROM playlist_tracks WHERE guild_id = ? AND playlist_name = ? AND track_name = ?",
            (guild_id, playlist_name, track_name), fetch='one'
        )
        if not existing:
            return False
        self._execute(
            "DELETE FROM playlist_tracks WHERE guild_id = ? AND playlist_name = ? AND track_name = ?",
            (guild_id, playlist_name, track_name)
        )
        return True
    
    def get_playlist_tracks(self, guild_id: int, playlist_name: str) -> List[Dict]:
        """Get all tracks in a playlist with their library info"""
        rows = self._execute(
            "SELECT pt.track_name, pt.position, pt.added_by, "
            "p.filename, p.file_path, p.file_size "
            "FROM playlist_tracks pt "
            "JOIN persistent_tracks p ON pt.guild_id = p.guild_id AND pt.track_name = p.track_name "
            "WHERE pt.guild_id = ? AND pt.playlist_name = ? "
            "ORDER BY pt.position",
            (guild_id, playlist_name), fetch='all'
        )
        return [dict(row) for row in rows] if rows else []
    
    # ========== Maintenance ==========
    
    def validate_files(self) -> int:
        """Remove database entries for missing files"""
        rows = self._execute(
            "SELECT guild_id, track_name, file_path, file_size FROM persistent_tracks",
            fetch='all'
        )
        
        orphaned = []
        for row in rows or []:
            if not os.path.exists(row['file_path']):
                orphaned.append((row['guild_id'], row['track_name'], row['file_size']))
        
        for guild_id, name, size in orphaned:
            self._execute(
                "DELETE FROM persistent_tracks WHERE guild_id = ? AND track_name = ?",
                (guild_id, name)
            )
            self._update_storage(guild_id, -size)
        
        if orphaned:
            logging.info(f"Cleaned {len(orphaned)} orphaned database entries")
        return len(orphaned)