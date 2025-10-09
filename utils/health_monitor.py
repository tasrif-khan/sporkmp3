import logging
import time
import asyncio
from datetime import datetime, timedelta

class HealthMonitor:
    def __init__(self, bot):
        self.bot = bot
        self.voice_connection_failures = {}
        self.last_health_check = time.time()
        
    async def monitor_voice_connections(self):
        """Monitor and report on voice connection health"""
        while True:
            try:
                unhealthy_connections = []
                
                for guild in self.bot.guilds:
                    voice_client = guild.voice_client
                    if voice_client:
                        # Check if connection is healthy
                        if not voice_client.is_connected():
                            unhealthy_connections.append(guild.id)
                            logging.warning(f"Unhealthy voice connection in guild {guild.id}")
                
                if unhealthy_connections:
                    logging.info(f"Found {len(unhealthy_connections)} unhealthy voice connections")
                
                # Wait 5 minutes before next check
                await asyncio.sleep(300)
                
            except Exception as e:
                logging.error(f"Error in voice connection monitor: {e}")
                await asyncio.sleep(60)
    
    def log_voice_failure(self, guild_id, error_code):
        """Log voice connection failures for analysis"""
        if guild_id not in self.voice_connection_failures:
            self.voice_connection_failures[guild_id] = []
        
        self.voice_connection_failures[guild_id].append({
            'timestamp': time.time(),
            'error_code': error_code
        })
        
        # Keep only last 10 failures per guild
        if len(self.voice_connection_failures[guild_id]) > 10:
            self.voice_connection_failures[guild_id] = self.voice_connection_failures[guild_id][-10:]
    
    def get_failure_stats(self, guild_id):
        """Get failure statistics for a guild"""
        if guild_id not in self.voice_connection_failures:
            return None
        
        failures = self.voice_connection_failures[guild_id]
        recent_failures = [f for f in failures if time.time() - f['timestamp'] < 3600]  # Last hour
        
        return {
            'total_failures': len(failures),
            'recent_failures': len(recent_failures),
            'last_failure': failures[-1]['timestamp'] if failures else None
        }
    
    async def get_bot_health_stats(self):
        """Get comprehensive bot health statistics"""
        try:
            # Voice connections
            connected_guilds = len([g for g in self.bot.guilds if g.voice_client])
            total_guilds = len(self.bot.guilds)
            
            # Get recent failures
            recent_failures = 0
            for guild_id in self.voice_connection_failures:
                stats = self.get_failure_stats(guild_id)
                if stats:
                    recent_failures += stats['recent_failures']
            
            return {
                'total_guilds': total_guilds,
                'voice_connections': connected_guilds,
                'recent_voice_failures': recent_failures,
                'uptime_hours': (time.time() - self.bot.start_time) / 3600 if hasattr(self.bot, 'start_time') else 0
            }
        except Exception as e:
            logging.error(f"Error getting health stats: {e}")
            return None
