from functools import wraps
import discord
from discord import app_commands
import logging

def check_permissions():
    """Check if user has permission to use the bot"""
    async def predicate(interaction: discord.Interaction):
        # Get the Music cog instance
        music_cog = interaction.client.get_cog('Music')
        if not music_cog:
            return False

        # Skip checks for administrators
        if interaction.user.guild_permissions.administrator:
            return True

        # Check if user is blacklisted
        if music_cog.db.is_user_blacklisted(interaction.guild_id, interaction.user.id):
            await interaction.response.send_message(
                embed=music_cog.music_ui.create_embed(
                    f"{music_cog.emoji['error']} Access Denied",
                    "You are blacklisted from using this bot.",
                    discord.Color.red()
                ),
                ephemeral=True
            )
            return False

        # Check role whitelist
        whitelisted_roles = music_cog.db.get_whitelisted_roles(interaction.guild_id)
        if whitelisted_roles:
            user_roles = [role.id for role in interaction.user.roles]
            if not any(role_id in user_roles for role_id in whitelisted_roles):
                await interaction.response.send_message(
                    embed=music_cog.music_ui.create_embed(
                        f"{music_cog.emoji['error']} Access Denied",
                        "You don't have the required role to use this bot.",
                        discord.Color.red()
                    ),
                    ephemeral=True
                )
                return False

        return True

    return app_commands.check(predicate)

def admin_only():
    """Check if user is an administrator"""
    async def predicate(interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            music_cog = interaction.client.get_cog('Music')
            await interaction.response.send_message(
                embed=music_cog.music_ui.create_embed(
                    f"{music_cog.emoji['error']} Access Denied",
                    "This command requires administrator privileges.",
                    discord.Color.red()
                ),
                ephemeral=True
            )
            return False
        return True

    return app_commands.check(predicate)