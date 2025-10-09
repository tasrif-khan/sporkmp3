import discord
import logging
import asyncio
from functools import wraps

class PermissionChecker:
    """Comprehensive permission checking for Discord operations"""
    
    @staticmethod
    def check_voice_permissions(voice_channel, bot_member):
        """Check if bot has required voice permissions"""
        permissions = voice_channel.permissions_for(bot_member)
        
        missing_perms = []
        if not permissions.connect:
            missing_perms.append("Connect")
        if not permissions.speak:
            missing_perms.append("Speak")
        if not permissions.use_voice_activation:
            missing_perms.append("Use Voice Activity")
        
        return missing_perms
    
    @staticmethod
    def check_text_permissions(text_channel, bot_member):
        """Check if bot has required text permissions"""
        permissions = text_channel.permissions_for(bot_member)
        
        missing_perms = []
        if not permissions.send_messages:
            missing_perms.append("Send Messages")
        if not permissions.embed_links:
            missing_perms.append("Embed Links")
        if not permissions.attach_files:
            missing_perms.append("Attach Files")
        
        return missing_perms
    
    @staticmethod
    def get_permission_error_embed(missing_perms, permission_type="voice"):
        """Create an embed for permission errors"""
        perms_list = ", ".join(missing_perms)
        
        if permission_type == "voice":
            title = "🔒 Missing Voice Permissions"
            description = f"I need the following permissions in the voice channel:\n`{perms_list}`"
        else:
            title = "🔒 Missing Text Permissions" 
            description = f"I need the following permissions in this channel:\n`{perms_list}`"
        
        embed = discord.Embed(
            title=title,
            description=description,
            color=discord.Color.red()
        )
        embed.add_field(
            name="How to fix:",
            value="Ask a server administrator to grant these permissions to my role.",
            inline=False
        )
        return embed

def safe_interaction(func):
    """Decorator to handle interaction timeouts and errors safely"""
    @wraps(func)
    async def wrapper(self, interaction, *args, **kwargs):
        try:
            # Check if interaction is still valid
            if interaction.response.is_done():
                # Already responded, call function directly
                return await func(self, interaction, *args, **kwargs)
            
            # Defer if we haven't responded yet and this might take time
            await interaction.response.defer()
            
            # Call the original function
            result = await func(self, interaction, *args, **kwargs)
            return result
            
        except discord.NotFound as e:
            if e.code == 10008:  # Unknown Message (interaction expired)
                logging.warning(f"Interaction expired for command {func.__name__}")
                # Try to send a message to the channel instead
                try:
                    channel = interaction.channel
                    if channel:
                        embed = self.music_ui.create_embed(
                            f"{self.music_ui.emoji['warning']} Command Timeout",
                            "The command took too long to process. Please try again.",
                            discord.Color.yellow()
                        )
                        await channel.send(embed=embed, delete_after=10)
                except:
                    pass  # If we can't send to channel either, give up gracefully
            else:
                logging.error(f"NotFound error in {func.__name__}: {e}")
        except discord.HTTPException as e:
            logging.error(f"HTTP exception in {func.__name__}: {e}")
            try:
                embed = self.music_ui.create_embed(
                    f"{self.music_ui.emoji['error']} Error",
                    "A Discord API error occurred. Please try again.",
                    discord.Color.red()
                )
                if not interaction.response.is_done():
                    await interaction.response.send_message(embed=embed, ephemeral=True)
                else:
                    await interaction.followup.send(embed=embed, ephemeral=True)
            except:
                pass
        except Exception as e:
            logging.error(f"Unexpected error in {func.__name__}: {e}")
            try:
                embed = self.music_ui.create_embed(
                    f"{self.music_ui.emoji['error']} Unexpected Error",
                    "An unexpected error occurred. Please try again.",
                    discord.Color.red()
                )
                if not interaction.response.is_done():
                    await interaction.response.send_message(embed=embed, ephemeral=True)
                else:
                    await interaction.followup.send(embed=embed, ephemeral=True)
            except:
                pass
    
    return wrapper
