"""
Main music cog for SporkMP3 bot.
Combines commands, playback logic, and event handling.
"""
import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import io
import logging
import time
import os
import re
import struct
import zlib
import aiohttp
from datetime import timedelta

from state import AudioTrack, MusicState
from database import Database
from utils import (
    EMOJI, Colors, TrackManager, VoiceHandler, HealthMonitor,
    create_embed, format_duration, progress_bar,
    error_embed, warning_embed, success_embed, info_embed,
    check_permissions, admin_only, safe_defer
)


# ============================================================================
# INTERACTIVE UI COMPONENTS
# ============================================================================


class PlayerHubView(discord.ui.View):
    """Unified player hub — transport controls + Library / Playlists / Queue navigation."""

    def __init__(self, music_cog, guild: discord.Guild):
        super().__init__(timeout=3600)
        self.music_cog = music_cog
        self.guild     = guild
        self._rebuild()

    def _rebuild(self):
        self.clear_items()
        vc    = self.guild.voice_client
        state = self.music_cog.state.guild_states.get(self.guild.id)

        is_paused = bool(vc and vc.is_paused())
        is_active = bool(vc and (vc.is_playing() or vc.is_paused()))
        at_start  = not state or state.queue_position <= 0
        at_end    = not state or state.queue_position >= len(state.queue) - 1
        loop_on   = bool(state and state.loop_enabled)
        has_queue = bool(state and state.queue)

        # ── Row 0: Transport ─────────────────────────────────────────────────
        b = discord.ui.Button(label="◀◀", style=discord.ButtonStyle.secondary,
                              disabled=at_start or not is_active, row=0)
        b.callback = self._on_previous
        self.add_item(b)

        b = discord.ui.Button(
            label="▶" if (is_paused or not is_active) else "‖",
            style=discord.ButtonStyle.success if (is_paused or (has_queue and not is_active))
                  else discord.ButtonStyle.secondary,
            disabled=not has_queue, row=0
        )
        b.callback = self._on_play_pause
        self.add_item(b)

        b = discord.ui.Button(label="■", style=discord.ButtonStyle.danger,
                              disabled=not is_active, row=0)
        b.callback = self._on_stop
        self.add_item(b)

        b = discord.ui.Button(label="▶▶", style=discord.ButtonStyle.secondary,
                              disabled=at_end or not is_active, row=0)
        b.callback = self._on_skip
        self.add_item(b)

        b = discord.ui.Button(
            label="↻",
            style=discord.ButtonStyle.success if loop_on else discord.ButtonStyle.secondary,
            disabled=not is_active, row=0
        )
        b.callback = self._on_loop
        self.add_item(b)

        # ── Row 1: Navigation ────────────────────────────────────────────────
        b = discord.ui.Button(label="Library", style=discord.ButtonStyle.primary, row=1)
        b.callback = self._open_library
        self.add_item(b)

        b = discord.ui.Button(label="Playlists", style=discord.ButtonStyle.primary, row=1)
        b.callback = self._open_playlists
        self.add_item(b)

        b = discord.ui.Button(label="Queue", style=discord.ButtonStyle.secondary,
                              disabled=not has_queue, row=1)
        b.callback = self._open_queue
        self.add_item(b)

    def build_embed(self) -> discord.Embed:
        state = self.music_cog.state.guild_states.get(self.guild.id)
        if state and state.current_track:
            return self.music_cog._build_now_playing_embed(self.guild, state)

        embed = create_embed(color=Colors.PRIMARY)
        embed.title = f"{EMOJI['music']} SporkMP3"
        if state and state.queue and state.is_stopped:
            embed.description = (
                f"{EMOJI['stop']} **Stopped** — {len(state.queue)} track(s) queued.\n"
                "Press ▶ to resume, or browse below."
            )
        else:
            embed.description = (
                "Nothing playing yet.\n"
                "Open **Library** to queue tracks, or load a **Playlist** to get started.\n"
                "You can also mention me with an audio file to queue it directly."
            )
        return embed

    # ── Permission check ──────────────────────────────────────────────────────
    async def _check_perms(self, interaction: discord.Interaction) -> bool:
        mc = self.music_cog
        vc = self.guild.voice_client
        if vc and vc.channel:
            if not interaction.user.voice or interaction.user.voice.channel != vc.channel:
                await interaction.response.send_message(
                    embed=error_embed("You must be in the same voice channel to control playback."),
                    ephemeral=True)
                return False
        if interaction.user.guild_permissions.administrator:
            return True
        if mc.db.is_blacklisted(interaction.guild_id, interaction.user.id):
            await interaction.response.send_message(
                embed=error_embed("You are blacklisted from using this bot."), ephemeral=True)
            return False
        whitelisted = mc.db.get_whitelisted_roles(interaction.guild_id)
        if whitelisted:
            user_roles = {r.id for r in interaction.user.roles}
            if not user_roles & set(whitelisted):
                await interaction.response.send_message(
                    embed=error_embed("You don't have the required role."), ephemeral=True)
                return False
        return True

    async def _edit_in_place(self, interaction: discord.Interaction):
        self._rebuild()
        embed = self.build_embed()
        state = self.music_cog.state.guild_states.get(self.guild.id)
        track = state.current_track if state else None
        if track and track.cover_url:
            # File attachment is still on this message — reference it directly
            embed.set_thumbnail(url="attachment://cover.png")
        await interaction.response.edit_message(embed=embed, view=self)

    # ── Transport callbacks ───────────────────────────────────────────────────
    async def _on_play_pause(self, interaction: discord.Interaction):
        if not await self._check_perms(interaction):
            return
        vc    = self.guild.voice_client
        state = self.music_cog.state.guild_states.get(self.guild.id)

        if vc and vc.is_playing():
            if state and state.current_track:
                state.current_track.pause_playback()
            vc.pause()
            await self._edit_in_place(interaction)
        elif vc and vc.is_paused():
            if state and state.current_track:
                state.current_track.resume_playback()
            vc.resume()
            await self._edit_in_place(interaction)
        elif state and state.queue:
            if not interaction.user.voice:
                await interaction.response.send_message(
                    embed=warning_embed("Join a voice channel first!"), ephemeral=True)
                return
            if not vc:
                vc = await self.music_cog.voice.connect(interaction.user.voice.channel)
                if not vc:
                    await interaction.response.send_message(
                        embed=error_embed("Failed to connect to voice channel."), ephemeral=True)
                    return
            state.is_stopped = False
            if state.queue_position < 0:
                state.queue_position = 0
            await interaction.response.defer()
            await self.music_cog._play_next(self.guild, force=True, advance=False)
        else:
            await interaction.response.send_message(
                embed=warning_embed("Nothing in queue — browse **Library** or **Playlists** first."),
                ephemeral=True)

    async def _on_stop(self, interaction: discord.Interaction):
        if not await self._check_perms(interaction):
            return
        vc    = self.guild.voice_client
        state = self.music_cog.state.guild_states.get(self.guild.id)
        if vc and (vc.is_playing() or vc.is_paused()):
            if state:
                state.is_stopped = True
                if state.current_track:
                    self.music_cog.tracks.mark_inactive(state.current_track.downloaded_path)
                    if state.current_track.converted_path:
                        self.music_cog.tracks.mark_inactive(state.current_track.converted_path)
            vc.stop()
        await self._edit_in_place(interaction)

    async def _on_skip(self, interaction: discord.Interaction):
        if not await self._check_perms(interaction):
            return
        vc    = self.guild.voice_client
        state = self.music_cog.state.guild_states.get(self.guild.id)

        if not state or not vc or (not vc.is_playing() and not vc.is_paused()):
            await interaction.response.send_message(
                embed=warning_embed("Nothing is playing!"), ephemeral=True)
            return
        if state.queue_position >= len(state.queue) - 1:
            await interaction.response.send_message(
                embed=warning_embed("No more tracks in queue!"), ephemeral=True)
            return

        await interaction.response.defer()
        state.loop_enabled      = False
        state.loop_count        = 0
        state.max_loops         = None
        state.manual_queue_seek = True
        state.queue_position   += 1
        vc.stop()
        await asyncio.sleep(0.3)
        await self.music_cog._play_next(self.guild, force=True, advance=False)
        state.manual_queue_seek = False

    async def _on_previous(self, interaction: discord.Interaction):
        if not await self._check_perms(interaction):
            return
        vc    = self.guild.voice_client
        state = self.music_cog.state.guild_states.get(self.guild.id)

        if not state or state.queue_position <= 0:
            await interaction.response.send_message(
                embed=warning_embed("Already at the first track!"), ephemeral=True)
            return

        await interaction.response.defer()
        state.loop_enabled      = False
        state.loop_count        = 0
        state.max_loops         = None
        state.manual_queue_seek = True
        state.queue_position   -= 1
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()
        await asyncio.sleep(0.3)
        await self.music_cog._play_next(self.guild, force=True, advance=False)
        state.manual_queue_seek = False

    async def _on_loop(self, interaction: discord.Interaction):
        if not await self._check_perms(interaction):
            return
        state = self.music_cog.state.guild_states.get(self.guild.id)
        if state:
            state.loop_enabled = not state.loop_enabled
            if not state.loop_enabled:
                state.loop_count = 0
                state.max_loops  = None
        await self._edit_in_place(interaction)

    # ── Navigation callbacks ──────────────────────────────────────────────────
    async def _open_library(self, interaction: discord.Interaction):
        state = self.music_cog.state.guild_states.get(self.guild.id)
        if state:
            state.last_channel_id = interaction.channel_id
        tracks = self.music_cog.db.list_tracks(self.guild.id)
        view   = LibraryView(tracks, self.guild.id, self.music_cog, from_hub=True)
        await interaction.response.edit_message(embed=view.build_embed(), view=view, attachments=[])

    async def _open_playlists(self, interaction: discord.Interaction):
        state = self.music_cog.state.guild_states.get(self.guild.id)
        if state:
            state.last_channel_id = interaction.channel_id
        view = PlaylistManagerView(self.music_cog, self.guild, interaction.user, from_hub=True)
        await interaction.response.edit_message(embed=view.build_embed(), view=view, attachments=[])

    async def _open_queue(self, interaction: discord.Interaction):
        state = self.music_cog.state.guild_states.get(self.guild.id)
        if not state or not state.queue:
            await interaction.response.send_message(
                embed=warning_embed("Queue is empty!"), ephemeral=True)
            return
        cur_page = max(0, state.queue_position // QueueView.TRACKS_PER_PAGE)
        view     = QueueView(state, self.music_cog, self.guild, page=cur_page, from_hub=True)
        await interaction.response.edit_message(embed=view.build_embed(), view=view, attachments=[])

    async def on_timeout(self):
        self.clear_items()
        state = self.music_cog.state.guild_states.get(self.guild.id)
        if state and state.last_np_message:
            try:
                await state.last_np_message.edit(view=self)
            except Exception:
                pass


class LibraryURLUploadModal(discord.ui.Modal, title="Upload URL to Library"):
    """Admin modal for saving an audio URL directly to the library."""

    track_name = discord.ui.TextInput(label="Track Name", placeholder="e.g. My Song", max_length=50)
    track_url  = discord.ui.TextInput(label="Audio URL",  placeholder="https://...", max_length=500)

    def __init__(self, music_cog, guild: discord.Guild):
        super().__init__()
        self.music_cog = music_cog
        self.guild     = guild

    async def on_submit(self, interaction: discord.Interaction):
        name = self.track_name.value.strip()
        url  = self.track_url.value.strip()

        if not url.startswith(('http://', 'https://')):
            await interaction.response.send_message(
                embed=error_embed("URL must start with http:// or https://"), ephemeral=True)
            return
        if self.music_cog.db.get_track(self.guild.id, name):
            await interaction.response.send_message(
                embed=warning_embed(f"**{name}** already exists in library!"), ephemeral=True)
            return

        await interaction.response.send_message(
            embed=info_embed("Downloading…", f"{EMOJI['loading']} Saving **{name}** to library…"),
            ephemeral=True)

        ALLOWED_EXTS = {'.mp3', '.wav', '.ogg', '.flac', '.m4a', '.aac', '.mp4', '.webm'}
        filename = url.split('/')[-1].split('?')[0]
        ext = os.path.splitext(filename)[1].lower()
        if not ext:
            for ae in ALLOWED_EXTS:
                if ae in url.lower():
                    ext = ae; filename = f"{name}{ext}"; break
            else:
                ext = '.mp3'; filename = f"{name}.mp3"

        if ext not in ALLOWED_EXTS:
            await interaction.followup.send(
                embed=error_embed(f"Unsupported format. Supported: {', '.join(ALLOWED_EXTS)}"),
                ephemeral=True)
            return

        perm_folder = 'permanent'
        os.makedirs(perm_folder, exist_ok=True)
        safe = ''.join(c for c in filename if c.isalnum() or c in '._- ')
        path = os.path.join(perm_folder, f"{self.guild.id}_{safe}")

        try:
            file_size = 0
            async with aiohttp.ClientSession() as session:
                try:
                    async with session.head(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                        file_size = int(r.headers.get('content-length', 0))
                except Exception:
                    file_size = 5 * 1024 * 1024

            if not self.music_cog.db.can_store(self.guild.id, file_size or 5 * 1024 * 1024):
                storage = self.music_cog.db.get_storage(self.guild.id)
                await interaction.followup.send(embed=error_embed(
                    f"Storage full! {storage['used']/1024/1024:.1f}MB / {storage['max']/1024/1024:.1f}MB"),
                    ephemeral=True)
                return

            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    if resp.status != 200:
                        await interaction.followup.send(
                            embed=error_embed(f"Download failed (HTTP {resp.status})"), ephemeral=True)
                        return
                    with open(path, 'wb') as f:
                        async for chunk in resp.content.iter_chunked(8192):
                            f.write(chunk)

            actual_size = os.path.getsize(path)
            self.music_cog.db.add_track(self.guild.id, name, filename, path,
                                         interaction.user.display_name, actual_size)
            await interaction.followup.send(embed=success_embed(
                "Saved to Library",
                f"{EMOJI['music']} **{name}** saved ({actual_size/1024/1024:.1f}MB). "
                f"Open **Library** in the player to queue it."),
                ephemeral=True)
        except Exception as e:
            if os.path.exists(path):
                os.remove(path)
            await interaction.followup.send(
                embed=error_embed(f"Upload failed: {str(e)[:100]}"), ephemeral=True)


class LibraryRemoveView(discord.ui.View):
    """Admin view for removing tracks from the library."""

    TRACKS_PER_PAGE = 20

    def __init__(self, tracks: list, guild_id: int, music_cog, page: int = 0):
        super().__init__(timeout=300)
        self.all_tracks = tracks
        self.guild_id   = guild_id
        self.music_cog  = music_cog
        self.page       = page
        self.max_page   = max(0, (len(tracks) - 1) // self.TRACKS_PER_PAGE) if tracks else 0
        self._rebuild()

    def _page_tracks(self):
        s = self.page * self.TRACKS_PER_PAGE
        return self.all_tracks[s:s + self.TRACKS_PER_PAGE]

    def _rebuild(self):
        self.clear_items()
        page_tracks = self._page_tracks()

        if page_tracks:
            options = [
                discord.SelectOption(
                    label=t['track_name'][:100],
                    description=f"{t['filename'][:50]} · {t['file_size']/1024/1024:.1f}MB",
                    value=t['track_name']
                ) for t in page_tracks[:25]
            ]
            sel = discord.ui.Select(placeholder="Select tracks to remove…",
                                    options=options, min_values=1,
                                    max_values=min(len(options), 5), row=0)
            sel.callback = self._on_remove
            self.add_item(sel)

        if self.max_page > 0:
            b = discord.ui.Button(label="◀ Prev", style=discord.ButtonStyle.secondary,
                                  disabled=(self.page == 0), row=1)
            b.callback = self._prev
            self.add_item(b)
            self.add_item(discord.ui.Button(label=f"Page {self.page+1}/{self.max_page+1}",
                                             style=discord.ButtonStyle.primary, disabled=True, row=1))
            b = discord.ui.Button(label="Next ▶", style=discord.ButtonStyle.secondary,
                                  disabled=(self.page >= self.max_page), row=1)
            b.callback = self._next
            self.add_item(b)

        b = discord.ui.Button(label="← Back to Library", style=discord.ButtonStyle.secondary, row=2)
        b.callback = self._on_back
        self.add_item(b)

    def build_embed(self) -> discord.Embed:
        embed = create_embed(color=Colors.WARNING)
        embed.title = "Remove from Library"
        embed.description = (
            f"Select tracks to permanently remove.\n"
            f"**{len(self.all_tracks)}** track(s) in library."
        )
        page_tracks = self._page_tracks()
        start_idx   = self.page * self.TRACKS_PER_PAGE
        lines = [
            f"`{start_idx + i + 1}.` **{t['track_name']}** › {t['file_size']/1024/1024:.1f}MB"
            for i, t in enumerate(page_tracks)
        ]
        if lines:
            embed.add_field(name="Tracks", value="\n".join(lines), inline=False)
        return embed

    async def _on_remove(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(embed=error_embed("Admin only."), ephemeral=True)
            return
        removed = []
        for name in interaction.data['values']:
            file_path = self.music_cog.db.remove_track(interaction.guild_id, name)
            if file_path:
                if os.path.exists(file_path):
                    os.remove(file_path)
                removed.append(name)
                self.all_tracks = [t for t in self.all_tracks if t['track_name'] != name]
        self.max_page = max(0, (len(self.all_tracks) - 1) // self.TRACKS_PER_PAGE) if self.all_tracks else 0
        self.page     = min(self.page, self.max_page)
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)
        if removed:
            await interaction.followup.send(
                embed=success_embed("Removed", f"Removed {len(removed)} track(s) from library."),
                ephemeral=True)

    async def _prev(self, interaction: discord.Interaction):
        self.page = max(0, self.page - 1)
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _next(self, interaction: discord.Interaction):
        self.page = min(self.max_page, self.page + 1)
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_back(self, interaction: discord.Interaction):
        tracks = self.music_cog.db.list_tracks(interaction.guild_id)
        view   = LibraryView(tracks, interaction.guild_id, self.music_cog, from_hub=True)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    async def on_timeout(self):
        self.clear_items()


class LibrarySelect(discord.ui.Select):
    """Dropdown to pick a library track and add it to queue"""
    
    def __init__(self, tracks: list, page: int, music_cog):
        self.music_cog = music_cog
        options = []
        for t in tracks:
            size_mb = t['file_size'] / 1024 / 1024
            fname = t['filename']
            if len(fname) > 50:
                fname = fname[:47] + "..."
            options.append(discord.SelectOption(
                label=t['track_name'][:100],
                description=f"{fname} ({size_mb:.1f}MB)",
                value=t['track_name']
            ))
        super().__init__(
            placeholder=f"Select a track to add to queue...",
            min_values=1,
            max_values=min(len(options), 5),
            options=options
        )
    
    async def callback(self, interaction: discord.Interaction):
        if not interaction.user.voice:
            await interaction.response.send_message(
                embed=warning_embed("Join a voice channel first!"), ephemeral=True
            )
            return
        
        await interaction.response.defer(ephemeral=True)
        
        state = await self.music_cog.state.get_guild_state(interaction.guild_id)
        state.last_channel_id = interaction.channel_id
        added = []
        
        for name in self.values:
            track_data = self.music_cog.db.get_track(interaction.guild_id, name)
            if not track_data:
                continue
            
            track = AudioTrack(
                url=track_data['file_path'],
                filename=track_data['filename'],
                requester=interaction.user.display_name,
                file_size=track_data['file_size'],
                is_permanent=True
            )
            track.downloaded_path = track_data['file_path']
            track.duration = track.get_metadata(track_data['file_path'])
            state.queue.append(track)
            added.append(name)
        
        if not added:
            await interaction.followup.send(embed=error_embed("Failed to add tracks."), ephemeral=True)
            return
        
        # Connect and start playback if needed
        if not interaction.guild.voice_client:
            vc = await self.music_cog.voice.connect(interaction.user.voice.channel)
            if not vc:
                await interaction.followup.send(
                    embed=error_embed("Failed to connect to voice channel"), ephemeral=True
                )
                return
        
        vc = interaction.guild.voice_client
        if not vc.is_playing() and not vc.is_paused():
            if state.queue_position < 0:
                state.queue_position = 0
            await self.music_cog._play_next(interaction.guild, force=True, advance=False)

        names_str = ", ".join(f"**{n}**" for n in added)
        await interaction.followup.send(
            embed=success_embed("Added to Queue", f"{EMOJI['music']} {names_str}"),
            ephemeral=True
        )


class LibraryView(discord.ui.View):
    """Paginated library browser with track select dropdown"""

    TRACKS_PER_PAGE = 10

    def __init__(self, tracks: list, guild_id: int, music_cog, page: int = 0,
                 from_hub: bool = False):
        super().__init__(timeout=120)
        self.all_tracks = tracks
        self.guild_id   = guild_id
        self.music_cog  = music_cog
        self.page       = page
        self.from_hub   = from_hub
        self.max_page   = max(0, (len(tracks) - 1) // self.TRACKS_PER_PAGE)
        self._update_components()

    def _page_tracks(self):
        start = self.page * self.TRACKS_PER_PAGE
        return self.all_tracks[start:start + self.TRACKS_PER_PAGE]

    def _update_components(self):
        self.clear_items()
        page_tracks = self._page_tracks()

        # Row 0: queue track dropdown
        if page_tracks:
            self.add_item(LibrarySelect(page_tracks, self.page, self.music_cog))

        # Row 1: pagination
        if self.max_page > 0:
            prev_btn = discord.ui.Button(label="◀ Prev", style=discord.ButtonStyle.secondary,
                                         disabled=self.page == 0, row=1)
            prev_btn.callback = self._prev
            self.add_item(prev_btn)
            self.add_item(discord.ui.Button(label=f"{self.page + 1}/{self.max_page + 1}",
                                             style=discord.ButtonStyle.primary, disabled=True, row=1))
            next_btn = discord.ui.Button(label="Next ▶", style=discord.ButtonStyle.secondary,
                                          disabled=self.page >= self.max_page, row=1)
            next_btn.callback = self._next
            self.add_item(next_btn)

        # Row 2: navigation + admin controls (hub mode only)
        if self.from_hub:
            b = discord.ui.Button(label="← Back to Player",
                                  style=discord.ButtonStyle.secondary, row=2)
            b.callback = self._on_back
            self.add_item(b)

            b = discord.ui.Button(label="↑ Upload URL",
                                  style=discord.ButtonStyle.secondary, row=2)
            b.callback = self._on_upload_url
            self.add_item(b)

            if self.all_tracks:
                b = discord.ui.Button(label="Remove Track",
                                      style=discord.ButtonStyle.danger, row=2)
                b.callback = self._on_remove
                self.add_item(b)

    def build_embed(self):
        page_tracks = self._page_tracks()
        embed = create_embed(color=Colors.PRIMARY)
        embed.title = f"{EMOJI['cd']} Sound Library"

        lines = []
        start_idx = self.page * self.TRACKS_PER_PAGE
        for i, t in enumerate(page_tracks):
            size_mb = t['file_size'] / 1024 / 1024
            lines.append(f"`{start_idx + i + 1}.` **{t['track_name']}** › "
                         f"`{t['filename'][:30]}` ({size_mb:.1f}MB)")

        embed.description = "\n".join(lines) if lines else (
            "Library is empty.\nUse **↑ Upload URL** to add tracks (admins only)."
        )

        storage = self.music_cog.db.get_storage(self.guild_id)
        used_mb  = storage['used'] / 1024 / 1024
        max_mb   = storage['max'] / 1024 / 1024
        percent  = (used_mb / max_mb * 100) if max_mb > 0 else 0
        embed.add_field(
            name="Storage",
            value=f"**{used_mb:.1f}MB** / {max_mb:.1f}MB ({percent:.0f}%) • {len(self.all_tracks)} track(s)",
            inline=False
        )
        footer = "Select tracks from the dropdown to add to queue"
        if self.from_hub:
            footer += " • Admins: Upload URL · Remove tracks"
        embed.set_footer(text=footer)
        return embed

    async def _prev(self, interaction: discord.Interaction):
        self.page = max(0, self.page - 1)
        self._update_components()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _next(self, interaction: discord.Interaction):
        self.page = min(self.max_page, self.page + 1)
        self._update_components()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_back(self, interaction: discord.Interaction):
        view  = PlayerHubView(self.music_cog, interaction.guild)
        embed = view.build_embed()
        state = self.music_cog.state.guild_states.get(interaction.guild_id)
        track = state.current_track if state else None
        if track and track.cover_url:
            embed.set_thumbnail(url=track.cover_url)
        await interaction.response.edit_message(embed=embed, view=view, attachments=[])

    async def _on_upload_url(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(embed=error_embed("Admin only."), ephemeral=True)
            return
        await interaction.response.send_modal(
            LibraryURLUploadModal(self.music_cog, interaction.guild))

    async def _on_remove(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(embed=error_embed("Admin only."), ephemeral=True)
            return
        tracks = self.music_cog.db.list_tracks(interaction.guild_id)
        view   = LibraryRemoveView(tracks, interaction.guild_id, self.music_cog)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    async def on_timeout(self):
        self.clear_items()


# ============================================================================
# PLAYLIST UI
# ============================================================================

class PlaylistCreateModal(discord.ui.Modal, title="Create Playlist"):
    """Modal popup form for creating a new playlist"""

    pl_name = discord.ui.TextInput(
        label="Playlist Name",
        placeholder="e.g. Chill Vibes",
        max_length=50
    )
    pl_desc = discord.ui.TextInput(
        label="Description (optional)",
        placeholder="A short description...",
        required=False,
        max_length=200,
        style=discord.TextStyle.paragraph
    )

    def __init__(self, music_cog, guild: discord.Guild):
        super().__init__()
        self.music_cog = music_cog
        self.guild     = guild

    async def on_submit(self, interaction: discord.Interaction):
        name = self.pl_name.value.strip()
        desc = self.pl_desc.value.strip()
        if self.music_cog.db.create_playlist(self.guild.id, name, interaction.user.display_name, desc):
            await interaction.response.send_message(
                embed=success_embed("Playlist Created",
                    f"**{name}** is ready.\nHit **↻ Refresh** in the manager to see it."),
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                embed=warning_embed(f"**{name}** already exists!"), ephemeral=True
            )


class PlaylistManagerView(discord.ui.View):
    """Top-level playlist browser — select a playlist then act on it."""

    def __init__(self, music_cog, guild: discord.Guild, invoker: discord.Member,
                 from_hub: bool = False):
        super().__init__(timeout=300)
        self.music_cog = music_cog
        self.guild     = guild
        self.invoker   = invoker
        self.from_hub  = from_hub
        self.selected  = None
        self._rebuild()

    def _is_admin(self, user: discord.Member) -> bool:
        return user.guild_permissions.administrator

    def _rebuild(self):
        self.clear_items()
        playlists = self.music_cog.db.list_playlists(self.guild.id)
        is_admin  = self._is_admin(self.invoker)
        has_sel   = self.selected is not None and bool(playlists)

        # ── Row 0: Playlist select dropdown ───────────────────────────────
        if playlists:
            options = []
            for p in playlists[:25]:
                desc = p.get('description') or ''
                label_desc = f"{p['track_count']} track(s)"
                if desc:
                    label_desc += f" — {desc[:40]}"
                options.append(discord.SelectOption(
                    label=p['playlist_name'],
                    description=label_desc,
                    value=p['playlist_name'],
                    default=(p['playlist_name'] == self.selected)
                ))
            sel = discord.ui.Select(placeholder="Select a playlist...", options=options, row=0)
            sel.callback = self._on_select
            self.add_item(sel)

        # ── Row 1: Action buttons ──────────────────────────────────────────
        b = discord.ui.Button(label="▶ Play All", style=discord.ButtonStyle.success,
                              disabled=not has_sel, row=1)
        b.callback = self._on_play_all
        self.add_item(b)

        b = discord.ui.Button(label="View Tracks", style=discord.ButtonStyle.primary,
                              disabled=not has_sel, row=1)
        b.callback = self._on_view
        self.add_item(b)

        if is_admin:
            b = discord.ui.Button(label="Add Tracks", style=discord.ButtonStyle.secondary,
                                  disabled=not has_sel, row=1)
            b.callback = self._on_add_tracks
            self.add_item(b)

            b = discord.ui.Button(label="Delete", style=discord.ButtonStyle.danger,
                                  disabled=not has_sel, row=1)
            b.callback = self._on_delete
            self.add_item(b)

        # ── Row 2: Create + Refresh + Back ───────────────────────────────────
        if is_admin:
            b = discord.ui.Button(label="+ Create Playlist",
                                  style=discord.ButtonStyle.secondary, row=2)
            b.callback = self._on_create
            self.add_item(b)

        b = discord.ui.Button(label="↻ Refresh", style=discord.ButtonStyle.secondary, row=2)
        b.callback = self._on_refresh
        self.add_item(b)

        if self.from_hub:
            b = discord.ui.Button(label="← Back to Player",
                                  style=discord.ButtonStyle.secondary, row=2)
            b.callback = self._on_back_to_hub
            self.add_item(b)

    def build_embed(self) -> discord.Embed:
        playlists = self.music_cog.db.list_playlists(self.guild.id)
        embed = create_embed(color=Colors.PRIMARY)
        embed.title = f"{EMOJI['queue']} Playlists"

        if not playlists:
            embed.description = (
                "No playlists yet.\n"
                "Admins can create one with **+ Create Playlist**."
            )
            return embed

        lines = []
        for p in playlists:
            prefix = "▶ " if p['playlist_name'] == self.selected else "　"
            desc   = f" — *{p['description'][:40]}*" if p.get('description') else ""
            lines.append(f"{prefix}**{p['playlist_name']}** › {p['track_count']} track(s){desc}")

        embed.description = "\n".join(lines)
        embed.set_footer(
            text=(f"Selected: {self.selected} • Use the buttons below"
                  if self.selected else "Pick a playlist from the dropdown to get started")
        )
        return embed

    # ---- callbacks ----

    async def _on_select(self, interaction: discord.Interaction):
        self.selected = interaction.data['values'][0]
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_play_all(self, interaction: discord.Interaction):
        if not interaction.user.voice:
            await interaction.response.send_message(
                embed=warning_embed("Join a voice channel first!"), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        added = await self.music_cog._queue_playlist_tracks(
            self.selected, interaction.guild, interaction.user, interaction.channel_id)

        if added == 0:
            await interaction.followup.send(embed=error_embed("No valid tracks in playlist."), ephemeral=True)
            return

        if not interaction.guild.voice_client:
            vc = await self.music_cog.voice.connect(interaction.user.voice.channel)
            if not vc:
                await interaction.followup.send(embed=error_embed("Failed to connect to voice."), ephemeral=True)
                return

        state = self.music_cog.state.guild_states.get(self.guild.id)
        vc = interaction.guild.voice_client
        if state and not vc.is_playing() and not vc.is_paused():
            if state.queue_position < 0:
                state.queue_position = 0
            await self.music_cog._play_next(self.guild, force=True, advance=False)

        await interaction.followup.send(
            embed=success_embed("Playlist Queued",
                f"{EMOJI['queue']} **{self.selected}** — {added} track(s) added to queue."),
            ephemeral=True
        )

    async def _on_view(self, interaction: discord.Interaction):
        playlist = self.music_cog.db.get_playlist(self.guild.id, self.selected)
        tracks   = self.music_cog.db.get_playlist_tracks(self.guild.id, self.selected)
        view     = PlaylistDetailView(self.selected, playlist, tracks, self.music_cog,
                                      self.guild, self.invoker, from_hub=self.from_hub)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    async def _on_add_tracks(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(embed=error_embed("Admin only."), ephemeral=True)
            return
        library  = self.music_cog.db.list_tracks(self.guild.id)
        existing = {t['track_name'] for t in
                    self.music_cog.db.get_playlist_tracks(self.guild.id, self.selected)}
        available = [t for t in library if t['track_name'] not in existing]

        if not available:
            await interaction.response.send_message(
                embed=info_embed("Nothing to Add",
                    "Every library track is already in this playlist."), ephemeral=True)
            return

        view = PlaylistAddView(available, self.selected, self.music_cog, self.guild, self.invoker)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    async def _on_delete(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(embed=error_embed("Admin only."), ephemeral=True)
            return
        view  = PlaylistDeleteConfirmView(self.selected, self.music_cog, self.guild,
                                          self.invoker, from_hub=self.from_hub)
        embed = create_embed(color=Colors.ERROR)
        embed.title = "Delete Playlist"
        embed.description = (f"Are you sure you want to delete **{self.selected}**?\n"
                             "This cannot be undone.")
        await interaction.response.edit_message(embed=embed, view=view)

    async def _on_create(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(embed=error_embed("Admin only."), ephemeral=True)
            return
        await interaction.response.send_modal(PlaylistCreateModal(self.music_cog, self.guild))

    async def _on_refresh(self, interaction: discord.Interaction):
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_back_to_hub(self, interaction: discord.Interaction):
        view  = PlayerHubView(self.music_cog, self.guild)
        embed = view.build_embed()
        state = self.music_cog.state.guild_states.get(self.guild.id)
        track = state.current_track if state else None
        if track and track.cover_url:
            embed.set_thumbnail(url=track.cover_url)
        await interaction.response.edit_message(embed=embed, view=view, attachments=[])

    async def on_timeout(self):
        self.clear_items()


class PlaylistDeleteConfirmView(discord.ui.View):
    """Inline confirm/cancel for playlist deletion"""

    def __init__(self, playlist_name: str, music_cog, guild: discord.Guild,
                 invoker: discord.Member, from_hub: bool = False):
        super().__init__(timeout=60)
        self.playlist_name = playlist_name
        self.music_cog     = music_cog
        self.guild         = guild
        self.invoker       = invoker
        self.from_hub      = from_hub

        b = discord.ui.Button(label="Yes, delete it", style=discord.ButtonStyle.danger, row=0)
        b.callback = self._confirm
        self.add_item(b)

        b = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary, row=0)
        b.callback = self._cancel
        self.add_item(b)

    async def _confirm(self, interaction: discord.Interaction):
        self.music_cog.db.delete_playlist(self.guild.id, self.playlist_name)
        view = PlaylistManagerView(self.music_cog, self.guild, self.invoker,
                                   from_hub=self.from_hub)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    async def _cancel(self, interaction: discord.Interaction):
        view = PlaylistManagerView(self.music_cog, self.guild, self.invoker,
                                   from_hub=self.from_hub)
        view.selected = self.playlist_name
        view._rebuild()
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class PlaylistDetailView(discord.ui.View):
    """Paginated playlist track viewer with queue controls and nav back"""

    TRACKS_PER_PAGE = 10

    def __init__(self, name: str, playlist: dict, tracks: list,
                 music_cog, guild: discord.Guild, invoker: discord.Member,
                 page: int = 0, from_hub: bool = False):
        super().__init__(timeout=300)
        self.name       = name
        self.playlist   = playlist or {}
        self.all_tracks = tracks
        self.music_cog  = music_cog
        self.guild      = guild
        self.invoker    = invoker
        self.page       = page
        self.from_hub   = from_hub
        self.max_page   = max(0, (len(tracks) - 1) // self.TRACKS_PER_PAGE) if tracks else 0
        self._rebuild()

    def _page_tracks(self):
        s = self.page * self.TRACKS_PER_PAGE
        return self.all_tracks[s:s + self.TRACKS_PER_PAGE]

    def _rebuild(self):
        self.clear_items()
        page_tracks = self._page_tracks()
        is_admin    = self.invoker.guild_permissions.administrator

        # ── Row 0: Queue selected dropdown ────────────────────────────────
        if page_tracks:
            options = []
            for t in page_tracks:
                size_mb = t['file_size'] / 1024 / 1024
                options.append(discord.SelectOption(
                    label=t['track_name'][:100],
                    description=f"#{t['position']} · {size_mb:.1f}MB",
                    value=t['track_name']
                ))
            sel = discord.ui.Select(
                placeholder="Queue individual tracks...",
                options=options,
                min_values=1,
                max_values=min(len(options), 10),
                row=0
            )
            sel.callback = self._on_queue_selected
            self.add_item(sel)

        # ── Row 1: Action buttons ─────────────────────────────────────────
        b = discord.ui.Button(label="▶ Play All", style=discord.ButtonStyle.success,
                              disabled=not self.all_tracks, row=1)
        b.callback = self._on_play_all
        self.add_item(b)

        if is_admin:
            b = discord.ui.Button(label="Add Tracks", style=discord.ButtonStyle.secondary, row=1)
            b.callback = self._on_add_tracks
            self.add_item(b)

        # ── Row 2: Pagination ─────────────────────────────────────────────
        if self.max_page > 0:
            b = discord.ui.Button(label="◀ Prev", style=discord.ButtonStyle.secondary,
                                  disabled=(self.page == 0), row=2)
            b.callback = self._prev
            self.add_item(b)

            self.add_item(discord.ui.Button(
                label=f"Page {self.page+1}/{self.max_page+1}",
                style=discord.ButtonStyle.primary, disabled=True, row=2
            ))

            b = discord.ui.Button(label="Next ▶", style=discord.ButtonStyle.secondary,
                                  disabled=(self.page >= self.max_page), row=2)
            b.callback = self._next
            self.add_item(b)

        # ── Row 3: Back ───────────────────────────────────────────────────
        b = discord.ui.Button(label="← Back to Playlists",
                              style=discord.ButtonStyle.secondary, row=3)
        b.callback = self._on_back
        self.add_item(b)

    def build_embed(self) -> discord.Embed:
        embed = create_embed(color=Colors.PRIMARY)
        embed.title = f"{EMOJI['queue']} {self.name}"

        desc = self.playlist.get('description', '')
        embed.description = f"*{desc}*\n\n" if desc else ""

        if not self.all_tracks:
            embed.description += "This playlist is empty. Use **Add Tracks** to populate it."
            return embed

        page_tracks = self._page_tracks()
        start_idx   = self.page * self.TRACKS_PER_PAGE
        lines = []
        for i, t in enumerate(page_tracks):
            size_mb = t['file_size'] / 1024 / 1024
            lines.append(f"`{start_idx + i + 1}.` **{t['track_name']}** › {size_mb:.1f}MB")
        embed.description += "\n".join(lines)

        embed.add_field(
            name="Info",
            value=(
                f"**Tracks** › {len(self.all_tracks)}\n"
                f"**Created by** › {self.playlist.get('created_by', '?')}"
            ),
            inline=False
        )
        embed.set_footer(
            text=f"Page {self.page+1}/{self.max_page+1} · Select from the dropdown to queue individually"
        )
        return embed

    async def _on_queue_selected(self, interaction: discord.Interaction):
        if not interaction.user.voice:
            await interaction.response.send_message(
                embed=warning_embed("Join a voice channel first!"), ephemeral=True)
            return

        selected_names = interaction.data['values']
        await interaction.response.defer(ephemeral=True)

        track_lookup = {t['track_name']: t for t in self.all_tracks}
        state = await self.music_cog.state.get_guild_state(self.guild.id)
        state.last_channel_id = interaction.channel_id
        added = []

        for name in selected_names:
            t = track_lookup.get(name)
            if not t:
                continue
            track = AudioTrack(
                url=t['file_path'], filename=t['filename'],
                requester=interaction.user.display_name,
                file_size=t['file_size'], is_permanent=True
            )
            track.downloaded_path = t['file_path']
            try:
                track.duration = track.get_metadata(t['file_path'])
            except Exception:
                continue
            state.queue.append(track)
            added.append(name)

        if not added:
            await interaction.followup.send(embed=error_embed("No tracks added."), ephemeral=True)
            return

        if not self.guild.voice_client:
            vc = await self.music_cog.voice.connect(interaction.user.voice.channel)
            if not vc:
                await interaction.followup.send(embed=error_embed("Failed to connect."), ephemeral=True)
                return

        vc = self.guild.voice_client
        if not vc.is_playing() and not vc.is_paused():
            if state.queue_position < 0:
                state.queue_position = 0
            await self.music_cog._play_next(self.guild, force=True, advance=False)

        await interaction.followup.send(
            embed=success_embed("Queued",
                f"{EMOJI['music']} Added **{len(added)}** track(s) to queue."),
            ephemeral=True
        )

    async def _on_play_all(self, interaction: discord.Interaction):
        if not interaction.user.voice:
            await interaction.response.send_message(
                embed=warning_embed("Join a voice channel first!"), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        added = await self.music_cog._queue_playlist_tracks(
            self.name, self.guild, interaction.user, interaction.channel_id)

        if not added:
            await interaction.followup.send(embed=error_embed("No valid tracks found."), ephemeral=True)
            return

        if not self.guild.voice_client:
            vc = await self.music_cog.voice.connect(interaction.user.voice.channel)
            if not vc:
                await interaction.followup.send(embed=error_embed("Failed to connect."), ephemeral=True)
                return

        state = self.music_cog.state.guild_states.get(self.guild.id)
        vc = self.guild.voice_client
        if state and not vc.is_playing() and not vc.is_paused():
            if state.queue_position < 0:
                state.queue_position = 0
            await self.music_cog._play_next(self.guild, force=True, advance=False)

        await interaction.followup.send(
            embed=success_embed("Playlist Queued",
                f"{EMOJI['queue']} **{self.name}** — {added} track(s) added."),
            ephemeral=True
        )

    async def _on_add_tracks(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(embed=error_embed("Admin only."), ephemeral=True)
            return
        library  = self.music_cog.db.list_tracks(self.guild.id)
        existing = {t['track_name'] for t in self.all_tracks}
        available = [t for t in library if t['track_name'] not in existing]

        if not available:
            await interaction.response.send_message(
                embed=info_embed("Nothing to Add",
                    "Every library track is already in this playlist."), ephemeral=True)
            return

        view = PlaylistAddView(available, self.name, self.music_cog, self.guild,
                               self.invoker, from_hub=self.from_hub)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    async def _prev(self, interaction: discord.Interaction):
        self.page = max(0, self.page - 1)
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _next(self, interaction: discord.Interaction):
        self.page = min(self.max_page, self.page + 1)
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_back(self, interaction: discord.Interaction):
        view          = PlaylistManagerView(self.music_cog, self.guild, self.invoker,
                                            from_hub=self.from_hub)
        view.selected = self.name
        view._rebuild()
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    async def on_timeout(self):
        self.clear_items()


class PlaylistAddView(discord.ui.View):
    """Paginated library browser for adding tracks to a playlist (admin)"""

    TRACKS_PER_PAGE = 10

    def __init__(self, available: list, playlist_name: str,
                 music_cog, guild: discord.Guild, invoker: discord.Member,
                 page: int = 0, from_hub: bool = False):
        super().__init__(timeout=300)
        self.available     = available
        self.playlist_name = playlist_name
        self.music_cog     = music_cog
        self.guild         = guild
        self.invoker       = invoker
        self.page          = page
        self.from_hub      = from_hub
        self.max_page      = max(0, (len(available) - 1) // self.TRACKS_PER_PAGE)
        self._rebuild()

    def _page_tracks(self):
        s = self.page * self.TRACKS_PER_PAGE
        return self.available[s:s + self.TRACKS_PER_PAGE]

    def _rebuild(self):
        self.clear_items()
        page_tracks = self._page_tracks()

        # ── Row 0: Track select dropdown ──────────────────────────────────
        if page_tracks:
            options = []
            for t in page_tracks:
                size_mb = t['file_size'] / 1024 / 1024
                options.append(discord.SelectOption(
                    label=t['track_name'][:100],
                    description=f"{t['filename'][:50]} · {size_mb:.1f}MB",
                    value=t['track_name']
                ))
            sel = discord.ui.Select(
                placeholder="Select tracks to add...",
                options=options,
                min_values=1,
                max_values=min(len(options), 10),
                row=0
            )
            sel.callback = self._on_add
            self.add_item(sel)

        # ── Row 1: Pagination ─────────────────────────────────────────────
        if self.max_page > 0:
            b = discord.ui.Button(label="◀ Prev", style=discord.ButtonStyle.secondary,
                                  disabled=(self.page == 0), row=1)
            b.callback = self._prev
            self.add_item(b)

            self.add_item(discord.ui.Button(
                label=f"Page {self.page+1}/{self.max_page+1}",
                style=discord.ButtonStyle.primary, disabled=True, row=1
            ))

            b = discord.ui.Button(label="Next ▶", style=discord.ButtonStyle.secondary,
                                  disabled=(self.page >= self.max_page), row=1)
            b.callback = self._next
            self.add_item(b)

        # ── Row 2: Back ───────────────────────────────────────────────────
        b = discord.ui.Button(label=f"← Back to {self.playlist_name}",
                              style=discord.ButtonStyle.secondary, row=2)
        b.callback = self._on_back
        self.add_item(b)

    def build_embed(self) -> discord.Embed:
        embed = create_embed(color=Colors.PRIMARY)
        embed.title = f"Add Tracks to {self.playlist_name}"

        page_tracks = self._page_tracks()
        start_idx   = self.page * self.TRACKS_PER_PAGE
        lines = []
        for i, t in enumerate(page_tracks):
            size_mb = t['file_size'] / 1024 / 1024
            lines.append(f"`{start_idx + i + 1}.` **{t['track_name']}** › {size_mb:.1f}MB")

        embed.description = "\n".join(lines) if lines else "No tracks available."
        embed.add_field(
            name="Available",
            value=f"**{len(self.available)}** track(s) not yet in this playlist",
            inline=False
        )
        embed.set_footer(
            text=f"Page {self.page+1}/{self.max_page+1} · Select up to 10 tracks per page"
        )
        return embed

    async def _on_add(self, interaction: discord.Interaction):
        selected_names = interaction.data['values']

        added, failed = [], []
        for name in selected_names:
            err = self.music_cog.db.add_to_playlist(
                self.guild.id, self.playlist_name, name, interaction.user.display_name)
            if err:
                failed.append(name)
            else:
                added.append(name)
                self.available = [t for t in self.available if t['track_name'] != name]

        self.max_page = max(0, (len(self.available) - 1) // self.TRACKS_PER_PAGE) if self.available else 0
        self.page     = min(self.page, self.max_page)
        self._rebuild()

        # Show result in the embed footer area by updating the view in place
        # then send an ephemeral followup for the result
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

        parts = []
        if added:
            parts.append(f"Added **{len(added)}** track(s) to **{self.playlist_name}**.")
        if failed:
            parts.append(f"Skipped {len(failed)} (already in playlist).")

        await interaction.followup.send(
            embed=success_embed("Tracks Added", "\n".join(parts)) if added
                  else warning_embed("\n".join(parts)),
            ephemeral=True
        )

    async def _prev(self, interaction: discord.Interaction):
        self.page = max(0, self.page - 1)
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _next(self, interaction: discord.Interaction):
        self.page = min(self.max_page, self.page + 1)
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_back(self, interaction: discord.Interaction):
        playlist = self.music_cog.db.get_playlist(self.guild.id, self.playlist_name)
        tracks   = self.music_cog.db.get_playlist_tracks(self.guild.id, self.playlist_name)
        view     = PlaylistDetailView(self.playlist_name, playlist, tracks,
                                      self.music_cog, self.guild, self.invoker,
                                      from_hub=self.from_hub)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    async def on_timeout(self):
        self.clear_items()


# ============================================================================
# SETTINGS VIEW
# ============================================================================

class AccessConfigView(discord.ui.View):
    """Sub-panel for managing role whitelist and user blacklist"""

    def __init__(self, music_cog, guild: discord.Guild, invoker: discord.Member):
        super().__init__(timeout=300)
        self.music_cog = music_cog
        self.guild     = guild
        self.invoker   = invoker
        self._rebuild()

    def _rebuild(self):
        self.clear_items()
        db    = self.music_cog.db
        gid   = self.guild.id
        roles = db.get_whitelisted_roles(gid)

        # ── Row 0: Add roles to whitelist ─────────────────────────────────
        role_sel = discord.ui.RoleSelect(
            placeholder="Add role to whitelist...",
            min_values=1,
            max_values=5,
            row=0
        )
        role_sel.callback = self._on_add_role
        self.add_item(role_sel)

        # ── Row 1: Remove individual roles (if any exist) ─────────────────
        if roles:
            options = []
            for rid in roles[:25]:
                r = self.guild.get_role(rid)
                options.append(discord.SelectOption(
                    label=r.name if r else f"Deleted role ({rid})",
                    value=str(rid)
                ))
            rm_sel = discord.ui.Select(
                placeholder="Remove role from whitelist...",
                options=options,
                min_values=1,
                max_values=min(len(options), 5),
                row=1
            )
            rm_sel.callback = self._on_remove_role
            self.add_item(rm_sel)

        # ── Row 2: Add user to blacklist ───────────────────────────────────
        user_sel = discord.ui.UserSelect(
            placeholder="Blacklist a user...",
            min_values=1,
            max_values=5,
            row=2
        )
        user_sel.callback = self._on_blacklist_user
        self.add_item(user_sel)

        # ── Row 3: Remove user from blacklist ──────────────────────────────
        # We can't show a UserSelect pre-populated, so offer a text summary
        # and a clear button only if blacklisted users exist
        blacklisted = db.get_blacklisted_users(gid) if hasattr(db, 'get_blacklisted_users') else []
        if blacklisted:
            opts = []
            for uid in blacklisted[:25]:
                member = self.guild.get_member(uid)
                label  = member.display_name if member else f"User {uid}"
                opts.append(discord.SelectOption(label=label, value=str(uid)))
            unban_sel = discord.ui.Select(
                placeholder="Remove user from blacklist...",
                options=opts,
                min_values=1,
                max_values=min(len(opts), 5),
                row=3
            )
            unban_sel.callback = self._on_unblacklist_user
            self.add_item(unban_sel)

        # ── Row 4: Back ────────────────────────────────────────────────────
        back = discord.ui.Button(
            label="← Back to Settings",
            style=discord.ButtonStyle.secondary,
            row=4
        )
        back.callback = self._on_back
        self.add_item(back)

    def build_embed(self) -> discord.Embed:
        db    = self.music_cog.db
        gid   = self.guild.id
        roles = db.get_whitelisted_roles(gid)
        blacklisted = db.get_blacklisted_users(gid) if hasattr(db, 'get_blacklisted_users') else []

        embed = create_embed(color=Colors.PRIMARY)
        embed.title = "Access Control"
        embed.description = f"Manage who can use SporkMP3 in **{self.guild.name}**"

        # Role whitelist
        if roles:
            role_lines = []
            for rid in roles:
                r = self.guild.get_role(rid)
                role_lines.append(r.mention if r else f"~~<deleted {rid}>~~")
            roles_val = "\n".join(role_lines[:10])
            if len(roles) > 10:
                roles_val += f"\n*+{len(roles)-10} more*"
        else:
            roles_val = "*No restrictions — everyone can use the bot*"

        embed.add_field(name="Role Whitelist", value=roles_val, inline=False)

        # Blacklist
        if blacklisted:
            bl_lines = []
            for uid in blacklisted:
                member = self.guild.get_member(uid)
                bl_lines.append(f"• {member.mention if member else f'<@{uid}>'}")
            bl_val = "\n".join(bl_lines[:10])
            if len(blacklisted) > 10:
                bl_val += f"\n*+{len(blacklisted)-10} more*"
        else:
            bl_val = "*No users blacklisted*"

        embed.add_field(name="Blacklisted Users", value=bl_val, inline=False)

        embed.set_footer(text="Role whitelist: if set, only those roles can use the bot • Admins are always exempt")
        return embed

    async def _check_admin(self, interaction: discord.Interaction) -> bool:
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                embed=error_embed("Only administrators can change access settings."), ephemeral=True)
            return False
        return True

    async def _on_add_role(self, interaction: discord.Interaction):
        if not await self._check_admin(interaction): return
        added = []
        for role in interaction.data.get('resolved', {}).get('roles', {}).values():
            self.music_cog.db.add_role_whitelist(self.guild.id, int(role['id']))
            r = self.guild.get_role(int(role['id']))
            added.append(r.name if r else role['id'])
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)
        if added:
            await interaction.followup.send(
                embed=success_embed("Whitelist Updated", f"Added: {', '.join(added)}"),
                ephemeral=True)

    async def _on_remove_role(self, interaction: discord.Interaction):
        if not await self._check_admin(interaction): return
        removed = []
        for rid_str in interaction.data['values']:
            rid = int(rid_str)
            self.music_cog.db.remove_role_whitelist(self.guild.id, rid)
            r = self.guild.get_role(rid)
            removed.append(r.name if r else str(rid))
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)
        if removed:
            await interaction.followup.send(
                embed=success_embed("Whitelist Updated", f"Removed: {', '.join(removed)}"),
                ephemeral=True)

    async def _on_blacklist_user(self, interaction: discord.Interaction):
        if not await self._check_admin(interaction): return
        added = []
        for uid_str, user_data in interaction.data.get('resolved', {}).get('users', {}).items():
            uid = int(uid_str)
            if uid == self.guild.me.id:
                continue  # don't blacklist the bot itself
            self.music_cog.db.add_blacklist(self.guild.id, uid)
            added.append(user_data.get('username', str(uid)))
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)
        if added:
            await interaction.followup.send(
                embed=success_embed("Blacklist Updated", f"Blacklisted: {', '.join(added)}"),
                ephemeral=True)

    async def _on_unblacklist_user(self, interaction: discord.Interaction):
        if not await self._check_admin(interaction): return
        removed = []
        for uid_str in interaction.data['values']:
            uid    = int(uid_str)
            member = self.guild.get_member(uid)
            self.music_cog.db.remove_blacklist(self.guild.id, uid)
            removed.append(member.display_name if member else str(uid))
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)
        if removed:
            await interaction.followup.send(
                embed=success_embed("Blacklist Updated", f"Removed: {', '.join(removed)}"),
                ephemeral=True)

    async def _on_back(self, interaction: discord.Interaction):
        view  = SettingsView(self.music_cog, self.guild, self.invoker)
        embed = view.build_embed()
        await interaction.response.edit_message(embed=embed, view=view)

    async def on_timeout(self):
        self.clear_items()


class SettingsView(discord.ui.View):
    """Interactive settings panel — admin only"""

    def __init__(self, music_cog, guild: discord.Guild, invoker: discord.Member):
        super().__init__(timeout=180)
        self.music_cog = music_cog
        self.guild     = guild
        self.invoker   = invoker
        self._rebuild()

    def _rebuild(self):
        self.clear_items()
        db   = self.music_cog.db
        gid  = self.guild.id
        ap   = db.get_autoplay(gid)
        ad   = db.get_autodisconnect(gid)
        spd  = db.get_speed(gid)

        # ── Row 0: Toggle buttons ──────────────────────────────────────────
        b = discord.ui.Button(
            label=f"Autoplay: {'ON' if ap else 'OFF'}",
            style=discord.ButtonStyle.success if ap else discord.ButtonStyle.secondary,
            row=0
        )
        b.callback = self._toggle_autoplay
        self.add_item(b)

        b = discord.ui.Button(
            label=f"Auto-disconnect: {'ON' if ad else 'OFF'}",
            style=discord.ButtonStyle.success if ad else discord.ButtonStyle.secondary,
            row=0
        )
        b.callback = self._toggle_autodisconnect
        self.add_item(b)

        # ── Row 1: Speed controls ──────────────────────────────────────────
        spd_down = discord.ui.Button(
            label="◀ −25%",
            style=discord.ButtonStyle.secondary,
            disabled=(spd <= 50),
            row=1
        )
        spd_down.callback = self._speed_down
        self.add_item(spd_down)

        self.add_item(discord.ui.Button(
            label=f"{spd}% Speed",
            style=discord.ButtonStyle.primary,
            disabled=True,
            row=1
        ))

        spd_up = discord.ui.Button(
            label="+25% ▶",
            style=discord.ButtonStyle.secondary,
            disabled=(spd >= 200),
            row=1
        )
        spd_up.callback = self._speed_up
        self.add_item(spd_up)

        # ── Row 2: Access config + Refresh ────────────────────────────────
        b = discord.ui.Button(
            label="Manage Access",
            style=discord.ButtonStyle.primary,
            row=2
        )
        b.callback = self._open_access
        self.add_item(b)

        b = discord.ui.Button(
            label="↻ Refresh",
            style=discord.ButtonStyle.secondary,
            row=2
        )
        b.callback = self._refresh
        self.add_item(b)

    def build_embed(self) -> discord.Embed:
        db      = self.music_cog.db
        gid     = self.guild.id
        ap      = db.get_autoplay(gid)
        ad      = db.get_autodisconnect(gid)
        spd     = db.get_speed(gid)
        storage = db.get_storage(gid)
        roles   = db.get_whitelisted_roles(gid)

        used_mb = storage['used'] / 1024 / 1024
        max_mb  = storage['max']  / 1024 / 1024
        pct     = (used_mb / max_mb * 100) if max_mb > 0 else 0

        embed = create_embed(color=Colors.PRIMARY)
        embed.title = f"{EMOJI['settings']} Server Settings"
        embed.description = f"Settings for **{self.guild.name}**"

        # Playback
        embed.add_field(
            name="Playback",
            value=(
                f"**Autoplay** › {'On' if ap else 'Off'}\n"
                f"**Auto-disconnect** › {'On' if ad else 'Off'}\n"
                f"**Speed** › {spd}%"
            ),
            inline=True
        )

        # Permissions
        if roles:
            role_mentions = []
            for rid in roles:
                r = self.guild.get_role(rid)
                role_mentions.append(r.mention if r else f"<deleted>")
            roles_val = "\n".join(role_mentions[:8])
            if len(roles) > 8:
                roles_val += f"\n*+{len(roles)-8} more*"
        else:
            roles_val = "*Everyone (no restrictions)*"

        embed.add_field(
            name="Access",
            value=f"**Whitelisted Roles**\n{roles_val}",
            inline=True
        )

        # Storage
        bar_len  = 12
        filled   = int((used_mb / max_mb) * bar_len) if max_mb > 0 else 0
        bar      = "█" * filled + "░" * (bar_len - filled)
        embed.add_field(
            name="Storage",
            value=f"`{bar}` {pct:.0f}%\n{used_mb:.1f} MB / {max_mb:.0f} MB used",
            inline=False
        )

        embed.set_footer(text="Changes apply immediately • Admin only")
        return embed

    async def _check_admin(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker.id and \
           not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                embed=error_embed("Only administrators can change settings."), ephemeral=True)
            return False
        return True

    async def _toggle_autoplay(self, interaction: discord.Interaction):
        if not await self._check_admin(interaction): return
        cur = self.music_cog.db.get_autoplay(self.guild.id)
        self.music_cog.db.set_autoplay(self.guild.id, not cur)
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _toggle_autodisconnect(self, interaction: discord.Interaction):
        if not await self._check_admin(interaction): return
        cur = self.music_cog.db.get_autodisconnect(self.guild.id)
        self.music_cog.db.set_autodisconnect(self.guild.id, not cur)
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _speed_down(self, interaction: discord.Interaction):
        if not await self._check_admin(interaction): return
        cur = self.music_cog.db.get_speed(self.guild.id)
        self.music_cog.db.set_speed(self.guild.id, max(50, cur - 25))
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _speed_up(self, interaction: discord.Interaction):
        if not await self._check_admin(interaction): return
        cur = self.music_cog.db.get_speed(self.guild.id)
        self.music_cog.db.set_speed(self.guild.id, min(200, cur + 25))
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _open_access(self, interaction: discord.Interaction):
        if not await self._check_admin(interaction): return
        view  = AccessConfigView(self.music_cog, self.guild, self.invoker)
        embed = view.build_embed()
        await interaction.response.edit_message(embed=embed, view=view)

    async def _refresh(self, interaction: discord.Interaction):
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def on_timeout(self):
        self.clear_items()


# ============================================================================
# QUEUE VIEW
# ============================================================================

class QueueView(discord.ui.View):
    """Paginated queue browser with jump-to, clear, and disconnect controls."""

    TRACKS_PER_PAGE = 10

    def __init__(self, state, music_cog, guild: discord.Guild, page: int = 0,
                 from_hub: bool = False):
        super().__init__(timeout=120)
        self.state      = state
        self.music_cog  = music_cog
        self.guild      = guild
        self.page       = page
        self.from_hub   = from_hub
        self.max_page   = max(0, (len(state.queue) - 1) // self.TRACKS_PER_PAGE)
        self._rebuild()

    def _page_tracks(self):
        s = self.page * self.TRACKS_PER_PAGE
        return self.state.queue[s:s + self.TRACKS_PER_PAGE]

    async def _check_perms(self, interaction: discord.Interaction) -> bool:
        mc = self.music_cog
        if interaction.user.guild_permissions.administrator:
            return True
        if mc.db.is_blacklisted(interaction.guild_id, interaction.user.id):
            await interaction.response.send_message(
                embed=error_embed("You are blacklisted."), ephemeral=True)
            return False
        whitelisted = mc.db.get_whitelisted_roles(interaction.guild_id)
        if whitelisted:
            user_roles = {r.id for r in interaction.user.roles}
            if not user_roles & set(whitelisted):
                await interaction.response.send_message(
                    embed=error_embed("You don't have the required role."), ephemeral=True)
                return False
        return True

    def _rebuild(self):
        self.clear_items()
        page_tracks = self._page_tracks()
        start_idx   = self.page * self.TRACKS_PER_PAGE

        # ── Row 0: Jump-to select ─────────────────────────────────────────
        if page_tracks:
            options = []
            for i, t in enumerate(page_tracks):
                abs_i = start_idx + i
                dur   = format_duration(t.duration) if t.duration else "?"
                name  = t.get_display_name()
                if len(name) > 80:
                    name = name[:77] + "..."
                is_cur = abs_i == self.state.queue_position
                options.append(discord.SelectOption(
                    label=f"{abs_i + 1}. {name}",
                    description=f"[{dur}] {'▶ Playing' if is_cur else ''}",
                    value=str(abs_i),
                    emoji="▶" if is_cur else None
                ))
            sel = discord.ui.Select(placeholder="Jump to track…", options=options, row=0)
            sel.callback = self._on_jump
            self.add_item(sel)

        # ── Row 1: Pagination ─────────────────────────────────────────────
        if self.max_page > 0:
            b = discord.ui.Button(label="◀ Prev", style=discord.ButtonStyle.secondary,
                                  disabled=(self.page == 0), row=1)
            b.callback = self._prev
            self.add_item(b)

            self.add_item(discord.ui.Button(
                label=f"Page {self.page+1}/{self.max_page+1}",
                style=discord.ButtonStyle.primary, disabled=True, row=1
            ))

            b = discord.ui.Button(label="Next ▶", style=discord.ButtonStyle.secondary,
                                  disabled=(self.page >= self.max_page), row=1)
            b.callback = self._next
            self.add_item(b)

        # ── Row 2: Actions ────────────────────────────────────────────────
        b = discord.ui.Button(label="Clear Queue", style=discord.ButtonStyle.danger, row=2)
        b.callback = self._on_clear
        self.add_item(b)

        b = discord.ui.Button(label="Disconnect", style=discord.ButtonStyle.danger, row=2)
        b.callback = self._on_disconnect
        self.add_item(b)

        # ── Row 3: Back ───────────────────────────────────────────────────
        if self.from_hub:
            b = discord.ui.Button(label="← Back to Player",
                                  style=discord.ButtonStyle.secondary, row=3)
            b.callback = self._on_back
            self.add_item(b)

    def build_embed(self) -> discord.Embed:
        embed = create_embed(color=Colors.PRIMARY)
        embed.title = f"{EMOJI['queue']} Queue"

        page_tracks = self._page_tracks()
        start_idx   = self.page * self.TRACKS_PER_PAGE
        lines = []
        for i, track in enumerate(page_tracks):
            abs_i = start_idx + i
            if abs_i == self.state.queue_position:
                icon = "▶"
            elif abs_i < self.state.queue_position:
                icon = "✓"
            else:
                icon = "▸"
            dur  = format_duration(track.duration) if track.duration else "?"
            name = track.get_display_name()
            if len(name) > 38:
                name = name[:35] + "..."
            lines.append(f"{icon} `{abs_i+1}.` **{name}** `[{dur}]`")

        embed.description = "\n".join(lines) if lines else "Queue is empty."

        total_dur  = sum(t.duration for t in self.state.queue if t.duration)
        total_size = sum(t.file_size for t in self.state.queue) / (1024 * 1024)
        remaining  = len(self.state.queue) - self.state.queue_position - 1

        embed.add_field(
            name="Stats",
            value=(
                f"**Tracks** › {len(self.state.queue)}\n"
                f"**Duration** › {format_duration(total_dur)}\n"
                f"**Size** › {total_size:.1f}MB"
            ),
            inline=True
        )
        pos_text = f"{self.state.queue_position + 1}/{len(self.state.queue)}"
        if self.state.loop_enabled:
            loop_text = "∞" if self.state.max_loops is None else f"{self.state.loop_count}/{self.state.max_loops}"
            pos_text += f"\n{EMOJI['loop']} {loop_text}"
        embed.add_field(name="Position", value=pos_text, inline=True)
        if remaining > 0:
            embed.add_field(name="Remaining", value=f"{remaining} track(s)", inline=True)

        embed.set_footer(text=f"Select a track to jump • Page {self.page+1}/{self.max_page+1}")
        return embed

    async def _on_jump(self, interaction: discord.Interaction):
        if not interaction.user.voice:
            await interaction.response.send_message(
                embed=warning_embed("Join a voice channel first!"), ephemeral=True)
            return
        await interaction.response.defer()
        target = int(interaction.data['values'][0])
        vc = self.guild.voice_client
        self.state.manual_queue_seek = True
        self.state.queue_position    = target
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()
        await asyncio.sleep(0.3)
        await self.music_cog._play_next(self.guild, force=True, advance=False)
        self.state.manual_queue_seek = False
        self._rebuild()
        try:
            await interaction.edit_original_response(embed=self.build_embed(), view=self)
        except discord.NotFound:
            pass  # NP message was deleted and re-sent by _send_now_playing; nothing to update here

    async def _prev(self, interaction: discord.Interaction):
        self.page = max(0, self.page - 1)
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _next(self, interaction: discord.Interaction):
        self.page = min(self.max_page, self.page + 1)
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_clear(self, interaction: discord.Interaction):
        if not await self._check_perms(interaction):
            return
        vc = self.guild.voice_client
        if vc and vc.is_playing():
            self.state.is_stopped = True
            vc.stop()
        count = len(self.state.queue)
        for track in self.state.queue:
            if not track.is_permanent:
                track.cleanup()
        self.state.reset()
        if self.from_hub:
            view = PlayerHubView(self.music_cog, self.guild)
            await interaction.response.edit_message(embed=view.build_embed(), view=view, attachments=[])
        else:
            await interaction.response.send_message(
                embed=success_embed("Queue Cleared", f"Removed {count} track(s)."), ephemeral=True)

    async def _on_disconnect(self, interaction: discord.Interaction):
        if not await self._check_perms(interaction):
            return
        vc = self.guild.voice_client
        if not vc:
            await interaction.response.send_message(
                embed=warning_embed("Not connected to voice!"), ephemeral=True)
            return
        if vc.is_playing() or vc.is_paused():
            self.state.is_stopped = True
            vc.stop()
        for track in self.state.queue:
            if track.downloaded_path:
                self.music_cog.tracks.mark_inactive(track.downloaded_path)
            if track.converted_path:
                self.music_cog.tracks.mark_inactive(track.converted_path)
            if not track.is_permanent:
                track.cleanup()
        self.state.reset()
        await asyncio.sleep(0.3)
        await vc.disconnect()
        if self.from_hub:
            view = PlayerHubView(self.music_cog, self.guild)
            await interaction.response.edit_message(embed=view.build_embed(), view=view, attachments=[])
        else:
            await interaction.response.send_message(
                embed=success_embed("Disconnected", "Cleared queue and left voice channel."))

    async def _on_back(self, interaction: discord.Interaction):
        view  = PlayerHubView(self.music_cog, self.guild)
        embed = view.build_embed()
        track = self.state.current_track
        if track and track.cover_url:
            embed.set_thumbnail(url=track.cover_url)
        await interaction.response.edit_message(embed=embed, view=view, attachments=[])

    async def on_timeout(self):
        self.clear_items()


class Music(commands.Cog):
    """Discord music bot cog"""
    
    def __init__(self, bot):
        self.bot = bot
        max_storage = bot.config.get('max_permanent_storage_mb', 100)
        self.db = Database(max_storage_mb=max_storage)
        self.state = MusicState()
        self.tracks = TrackManager(bot.config)
        self.voice = VoiceHandler()
        self.health = HealthMonitor(bot)
        logging.info("Music cog loaded")
    
    async def cog_load(self):
        """Initialize on cog load"""
        await self.tracks.ensure_temp_folder()
        await self.tracks.cleanup_temp_files()
        orphaned = self.db.validate_files()
        if orphaned:
            logging.info(f"Startup: cleaned {orphaned} orphaned entries")

        updated = self.db.update_all_storage_limits()
        if updated:
            logging.info(f"Startup: updated storage limits for {updated} guild(s)")

        asyncio.create_task(self._cleanup_loop())
        asyncio.create_task(self._activity_loop())
        asyncio.create_task(self.health.monitor_loop())
    
    async def cog_unload(self):
        """Cleanup on cog unload"""
        for guild in self.bot.guilds:
            if guild.voice_client:
                await guild.voice_client.disconnect()
        
        for state in self.state.guild_states.values():
            for track in state.queue:
                if not track.is_permanent:
                    track.cleanup()
        
        await self.tracks.cleanup_temp_files()
    
    # ========== Helper Methods ==========

    @staticmethod
    def _make_default_cover() -> bytes:
        """Generate a 300×300 solid-color PNG (no external deps) for tracks without artwork."""
        w, h, r, g, b = 300, 300, 0x3e, 0x45, 0x66  # Colors.PRIMARY

        def chunk(tag: bytes, data: bytes) -> bytes:
            c = tag + data
            return struct.pack('>I', len(data)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)

        ihdr = chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
        raw  = b''.join(b'\x00' + bytes([r, g, b] * w) for _ in range(h))
        idat = chunk(b'IDAT', zlib.compress(raw, 9))
        iend = chunk(b'IEND', b'')
        return b'\x89PNG\r\n\x1a\n' + ihdr + idat + iend

    def _cover_file(self, track) -> discord.File:
        """Return a discord.File containing the track's artwork or the default cover."""
        data = (track.get_artwork() if track else None) or self._make_default_cover()
        return discord.File(io.BytesIO(data), filename="cover.png")

    async def _get_state(self, interaction: discord.Interaction):
        """Get guild state and update last channel"""
        state = await self.state.get_guild_state(interaction.guild_id)
        state.last_channel_id = interaction.channel_id
        return state
    
    def _get_audio_source(self, track: AudioTrack, start: float = 0, speed: int = 100):
        """Create FFmpeg audio source"""
        track.last_accessed = time.time()
        duration = track.duration or 0
        start = max(0, min(start, duration)) if duration else max(0, start)
        
        timestamp = str(timedelta(seconds=int(start)))
        before_opts = f'-ss {timestamp}'
        
        # Build FFmpeg options
        buffer = max(2048, track.bitrate * 4)
        opts = f'-vn -af aformat=sample_fmts=s16:channel_layouts=stereo:sample_rates=48000 -bufsize {buffer}k -thread_queue_size 1024'
        
        if speed != 100 and 50 <= speed <= 200:
            opts = f'-vn -af "aformat=sample_fmts=s16:channel_layouts=stereo:sample_rates=48000,atempo={speed / 100.0}"'
        elif speed == 100:
            opts = f'-vn -af aformat=sample_fmts=s16:channel_layouts=stereo:sample_rates=48000'
        
        # Use converted file if codec conversion was needed, else original
        audio_path = track.playback_path
        
        source = discord.FFmpegPCMAudio(
            audio_path,
            before_options=before_opts,
            options=opts
        )
        
        track.start_playback(start)
        return discord.PCMVolumeTransformer(source, volume=track.volume / 100)
    
    async def _play_next(self, guild: discord.Guild, force: bool = False, advance: bool = True):
        """Play the next track in queue"""
        state = await self.state.get_guild_state(guild.id)
        
        if state.is_seeking:
            return
        
        # Empty queue check
        if not state.queue:
            if self.db.get_autodisconnect(guild.id) and guild.voice_client:
                await guild.voice_client.disconnect()
            return
        
        # Check autoplay before advancing — otherwise we'd advance position then bail,
        # leaving the queue pointer on the wrong track for next /play
        if not force and not self.db.get_autoplay(guild.id):
            return

        current = state.current_track

        # Handle looping
        if state.loop_enabled and current:
            if state.max_loops is not None:
                state.loop_count += 1
                if state.loop_count >= state.max_loops:
                    state.loop_enabled = False
                    state.loop_count = 0
                    state.max_loops = None
                    if advance:
                        state.queue_position += 1
            # Infinite loop: stay at current position
        elif advance:
            state.queue_position += 1
        
        # End of queue
        if state.queue_position >= len(state.queue):
            if current and current.downloaded_path:
                self.tracks.mark_inactive(current.downloaded_path)
                if current.converted_path:
                    self.tracks.mark_inactive(current.converted_path)
            if self.db.get_autodisconnect(guild.id) and guild.voice_client:
                await guild.voice_client.disconnect()
            return
        
        voice = guild.voice_client
        if not voice or not voice.is_connected():
            return
        
        # Prepare next track
        track = state.current_track
        if not track:
            return
        
        # File management
        if current and current != track and current.downloaded_path:
            self.tracks.mark_inactive(current.downloaded_path)
            if current.converted_path:
                self.tracks.mark_inactive(current.converted_path)
        if track.downloaded_path:
            self.tracks.mark_active(track.downloaded_path)
        if track.converted_path:
            self.tracks.mark_active(track.converted_path)
        
        # Download if needed
        if not track.downloaded_path:
            await self.tracks.ensure_temp_folder()
            await track.download(self.bot.config['temp_folder'])
        
        # Ensure codec compatibility (covers library tracks that skip download)
        if track.downloaded_path and not track.converted_path:
            try:
                await track._ensure_compatible(self.bot.config['temp_folder'])
            except RuntimeError as e:
                logging.error(f"Codec compatibility check failed: {e}")
                # Skip this track — try the next one
                if not state.is_seeking and not state.manual_queue_seek:
                    state.queue_position += 1
                    asyncio.ensure_future(self._play_next(guild, force=True, advance=False))
                return
        
        state.last_activity = time.time()
        
        # Create audio source
        speed = self.db.get_speed(guild.id)
        source = self._get_audio_source(track, track.position, speed)
        source.volume = state.volume / 100
        
        # Playback callback
        def after(error):
            if error:
                logging.error(f'Playback error in guild {guild.id}: {error}')
            
            # Check if bot is alone before clearing alone_since
            if voice and voice.channel and len(voice.channel.members) > 1:
                self.state.alone_since.pop(guild.id, None)
            
            state.last_activity = time.time()
            
            if not state.is_seeking and not state.manual_queue_seek and not state.is_stopped:
                asyncio.run_coroutine_threadsafe(self._play_next(guild), self.bot.loop)
        
        voice.play(source, after=after)
        state.is_stopped = False
        
        # Only clear alone_since if there are other members in the channel
        if voice.channel and len(voice.channel.members) > 1:
            self.state.alone_since.pop(guild.id, None)
        
        logging.info(f"Playing '{track.filename}' ({state.queue_position + 1}/{len(state.queue)}) in guild {guild.id}")
        
        # Send now playing message
        if state.last_channel_id:
            await self._send_now_playing(guild, state)
    
    async def _send_now_playing(self, guild: discord.Guild, state):
        """Send or edit-in-place the Now Playing embed with artwork."""
        channel = guild.get_channel(state.last_channel_id)
        if not channel:
            return
        track = state.current_track
        if not track:
            return

        view = PlayerHubView(self, guild)

        # When the track changes we must resend — can't add file attachments via edit
        if state.last_np_track is not track and state.last_np_message:
            try:
                await state.last_np_message.delete()
            except Exception:
                pass
            state.last_np_message = None

        embed = self._build_now_playing_embed(guild, state)

        # ── Edit in place (same track) ────────────────────────────────────
        if state.last_np_message:
            if track.cover_url:
                embed.set_thumbnail(url=track.cover_url)
            try:
                await state.last_np_message.edit(embed=embed, view=view)
                return
            except discord.NotFound:
                state.last_np_message = None
            except Exception as e:
                logging.warning(f"Failed to edit NP message: {type(e).__name__}: {e}")
                state.last_np_message = None

        # ── Fresh send with artwork ───────────────────────────────────────
        embed.set_thumbnail(url="attachment://cover.png")
        try:
            msg = await channel.send(embed=embed, view=view, file=self._cover_file(track))
            if msg.attachments:
                track.cover_url = msg.attachments[0].url
            state.last_np_message = msg
            state.last_np_track   = track
        except Exception as e:
            logging.error(f"Failed to send now playing: {type(e).__name__}: {e}")

    def _build_now_playing_embed(self, guild: discord.Guild, state) -> discord.Embed:
        """Build the Now Playing embed (shared by command + button updates)"""
        track = state.current_track
        pos   = track.get_current_position()
        speed = self.db.get_speed(guild.id)

        embed = create_embed(color=Colors.PRIMARY)

        # Status line
        if state.is_stopped:
            embed.title = f"{EMOJI['stop']} Stopped"
        elif guild.voice_client and guild.voice_client.is_paused():
            embed.title = f"{EMOJI['pause']} Paused"
        else:
            embed.title = f"{EMOJI['play']} Now Playing"

        display_name = track.get_display_name()
        embed.description = f"### {display_name}"

        # Progress bar
        bar  = progress_bar(pos, track.duration)
        embed.add_field(
            name="Progress",
            value=f"{format_duration(pos)} {bar} {format_duration(track.duration)}",
            inline=False
        )

        # Details
        details = [f"**Requester** › {track.requester}", f"**Bitrate** › {track.bitrate}kbps"]
        if speed != 100:
            details.append(f"**Speed** › {EMOJI['slow'] if speed < 100 else EMOJI['fast']} {speed}%")
        embed.add_field(name="Details", value="\n".join(details), inline=True)

        # Queue position + loop
        remaining  = len(state.queue) - state.queue_position - 1
        queue_info = [f"**Position** › {state.queue_position + 1}/{len(state.queue)}"]
        if state.loop_enabled:
            loop_text = "∞" if state.max_loops is None else f"{state.loop_count}/{state.max_loops}"
            queue_info.append(f"**Loop** › {EMOJI['loop']} {loop_text}")
        embed.add_field(name="Queue", value="\n".join(queue_info), inline=True)

        # Coming up — next 3 tracks
        next_tracks = state.queue[state.queue_position + 1:state.queue_position + 4]
        if next_tracks:
            lines = [
                f"`{state.queue_position + i + 2}.` {t.get_display_name()[:45]}"
                for i, t in enumerate(next_tracks)
            ]
            if remaining > 3:
                lines.append(f"*…+{remaining - 3} more*")
            embed.add_field(name="Coming Up", value="\n".join(lines), inline=False)

        return embed

    async def _update_np_embed(self, guild: discord.Guild):
        """Silently refresh the NP embed (volume, loop, seek) without touching the cover."""
        state = self.state.guild_states.get(guild.id)
        if not state or not state.last_np_message:
            return
        track = state.current_track
        if not track:
            return
        try:
            embed = self._build_now_playing_embed(guild, state)
            view  = PlayerHubView(self, guild)
            if track.cover_url:
                embed.set_thumbnail(url=track.cover_url)
            await state.last_np_message.edit(embed=embed, view=view)
        except Exception as e:
            logging.debug(f"Could not refresh NP embed: {e}")
            state.last_np_message = None

    async def _queue_playlist_tracks(self, playlist_name: str, guild: discord.Guild,
                                      user: discord.Member, channel_id: int) -> int:
        """Queue all tracks from a playlist. Returns number of tracks added."""
        tracks = self.db.get_playlist_tracks(guild.id, playlist_name)
        if not tracks:
            return 0
        state = await self.state.get_guild_state(guild.id)
        state.last_channel_id = channel_id
        added = 0
        for t in tracks:
            track = AudioTrack(
                url=t['file_path'], filename=t['filename'],
                requester=user.display_name,
                file_size=t['file_size'], is_permanent=True
            )
            track.downloaded_path = t['file_path']
            try:
                track.duration = track.get_metadata(t['file_path'])
            except Exception as e:
                logging.warning(f"Skipping {t['track_name']} in playlist {playlist_name}: {e}")
                continue
            state.queue.append(track)
            added += 1
        return added
    
    # ========== Background Tasks ==========
    
    async def _cleanup_loop(self):
        """Periodic cleanup of inactive resources"""
        while True:
            try:
                now = time.time()
                
                # Cleanup alone bots (5 min)
                for guild_id in list(self.state.alone_since.keys()):
                    if now - self.state.alone_since[guild_id] > 300:
                        guild = self.bot.get_guild(guild_id)
                        if guild and guild.voice_client:
                            vc = guild.voice_client.channel
                            if len(vc.members) == 1:
                                await guild.voice_client.disconnect()
                        self.state.alone_since.pop(guild_id, None)
                
                # Cleanup inactive guilds (3 hours)
                for guild_id, gs in list(self.state.guild_states.items()):
                    if now - gs.last_activity > 10800:
                        guild = self.bot.get_guild(guild_id)
                        if guild and guild.voice_client:
                            await guild.voice_client.disconnect()
                        for track in gs.queue:
                            track.cleanup()
                        del self.state.guild_states[guild_id]
                
                # Cleanup rate limits
                self.state.rate_limits = {
                    k: v for k, v in self.state.rate_limits.items()
                    if now - v < 60
                }
                
                # Cleanup temp files
                await self.tracks.cleanup_temp_files()
                
                await asyncio.sleep(300)
            except Exception as e:
                logging.error(f"Cleanup error: {e}")
                await asyncio.sleep(60)
    
    async def _activity_loop(self):
        """Update activity timestamps and monitor voice channel occupancy"""
        while True:
            try:
                for guild in self.bot.guilds:
                    vc = guild.voice_client
                    if vc and vc.is_playing():
                        # Update activity timestamp
                        state = await self.state.get_guild_state(guild.id)
                        state.last_activity = time.time()
                        
                        # Check if bot is alone and autodisconnect is enabled
                        if self.db.get_autodisconnect(guild.id):
                            if vc.channel and len(vc.channel.members) == 1:
                                # Start tracking if not already tracked
                                if guild.id not in self.state.alone_since:
                                    self.state.alone_since[guild.id] = time.time()
                                    logging.info(f"Bot is alone in voice channel in guild {guild.id}")
                            else:
                                # Clear tracking if users are present
                                self.state.alone_since.pop(guild.id, None)
                
                await asyncio.sleep(60)  # Check every minute instead of 15 minutes
            except Exception as e:
                logging.error(f"Activity loop error: {e}")
                await asyncio.sleep(60)
    
    # ========== Event Listeners ==========
    
    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        """Handle voice state changes"""
        if member.bot or not before.channel:
            return
        
        voice = before.channel.guild.voice_client
        if voice and voice.channel == before.channel:
            if len(before.channel.members) == 1:
                self.state.alone_since[before.channel.guild.id] = time.time()
            else:
                self.state.alone_since.pop(before.channel.guild.id, None)
    
    @commands.Cog.listener()
    async def on_message(self, message):
        """Handle file uploads and CDN links via mention"""
        if message.author.bot or not message.guild:
            return
        if f'<@{self.bot.user.id}>' not in message.content and f'<@!{self.bot.user.id}>' not in message.content:
            return
        
        # Check permissions
        if not message.author.voice:
            await message.channel.send(embed=warning_embed("Join a voice channel first!"))
            return
        
        if self.db.is_blacklisted(message.guild.id, message.author.id):
            return

        whitelisted = self.db.get_whitelisted_roles(message.guild.id)
        if whitelisted:
            user_roles = {r.id for r in message.author.roles}
            if not user_roles & set(whitelisted):
                return
        
        state = await self.state.get_guild_state(message.guild.id)
        state.last_channel_id = message.channel.id
        
        ALLOWED_EXTS = {'.mp3', '.wav', '.ogg', '.flac', '.m4a', '.aac', '.mp4', '.webm'}
        added, skipped = [], []
        
        # Process attachments first
        for att in message.attachments[:10]:
            ext = os.path.splitext(att.filename)[1].lower()
            if ext not in ALLOWED_EXTS:
                skipped.append(f"{att.filename} (unsupported)")
                continue
            
            if not self.tracks.can_add(state.queue, att.size):
                skipped.append(f"{att.filename} (queue full)")
                continue
            
            track = AudioTrack(
                url=att.url,
                filename=att.filename,
                requester=message.author.display_name,
                file_size=att.size
            )
            state.queue.append(track)
            added.append(att.filename)
        
        # Process CDN links in message content
        # Enhanced URL pattern to match Discord CDN and other audio file URLs
        url_patterns = [
            r'(https://cdn\.discordapp\.com/attachments/[^\s]+)',  # Discord CDN
            r'(https://media\.discordapp\.net/attachments/[^\s]+)',  # Discord media
            r'(https?://[^\s]+\.(?:mp3|wav|ogg|flac|m4a|aac|mp4|webm)(?:\?[^\s]*)?)'  # Direct audio URLs
        ]
        
        urls = []
        for pattern in url_patterns:
            urls.extend(re.findall(pattern, message.content, re.IGNORECASE))
        
        # Remove duplicates while preserving order
        seen = set()
        unique_urls = []
        for url in urls:
            if url not in seen:
                seen.add(url)
                unique_urls.append(url)
        
        for url in unique_urls[:10 - len(added)]:  # Limit total to 10 tracks
            try:
                # Extract filename from URL
                filename = url.split('/')[-1].split('?')[0]
                
                # If no extension in filename, try to detect from URL or use default
                if not os.path.splitext(filename)[1]:
                    # Check if URL has audio extension anywhere
                    for ext in ALLOWED_EXTS:
                        if ext in url.lower():
                            filename = f"audio{ext}"
                            break
                    else:
                        filename = "audio.mp3"  # Default
                
                ext = os.path.splitext(filename)[1].lower()
                
                if ext not in ALLOWED_EXTS:
                    skipped.append(f"{filename} (unsupported format)")
                    continue
                
                # Try to get file size with HEAD request
                file_size = 0
                async with aiohttp.ClientSession() as session:
                    try:
                        async with session.head(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                            file_size = int(resp.headers.get('content-length', 0))
                    except:
                        try:
                            # If HEAD fails, try GET with range
                            async with session.get(url, headers={'Range': 'bytes=0-0'}, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                                content_range = resp.headers.get('content-range', '')
                                if '/' in content_range:
                                    file_size = int(content_range.split('/')[-1])
                                else:
                                    file_size = 5 * 1024 * 1024  # Default 5MB estimate
                        except:
                            file_size = 5 * 1024 * 1024  # Default 5MB estimate
                
                # Check if can add
                if not self.tracks.can_add(state.queue, file_size):
                    skipped.append(f"{filename} (queue full)")
                    continue
                
                track = AudioTrack(
                    url=url,
                    filename=filename,
                    requester=message.author.display_name,
                    file_size=file_size
                )
                state.queue.append(track)
                added.append(filename)
                logging.info(f"Added CDN link: {url}")
                
            except Exception as e:
                logging.warning(f"Failed to process URL {url}: {e}")
                skipped.append(f"{url.split('/')[-1][:30]} (failed)")
                continue
        
        # Send response
        if added or skipped:
            msg = []
            if added:
                msg.append(f"✓ Added {len(added)} track(s)")
            if skipped:
                msg.append(f"✕ Skipped: {', '.join(skipped[:5])}")
            
            size_mb = self.tracks.get_queue_size(state.queue) / (1024 * 1024)
            msg.append(f"\n{EMOJI['cd']} Queue: {size_mb:.1f}MB / {self.bot.config['max_queue_size_mb']}MB")
            
            await message.channel.send(embed=success_embed("Tracks Added", '\n'.join(msg)))
        elif not message.attachments:
            # No valid content found
            await message.channel.send(embed=warning_embed(
                "No audio files or CDN links found!\n"
                "Attach audio files or paste CDN links."
            ))
        
        # Connect and play
        if not message.guild.voice_client and added:
            vc = await self.voice.connect(message.author.voice.channel)
            if vc and self.db.get_autoplay(message.guild.id):
                await self._play_next(message.guild)
        elif message.guild.voice_client and not message.guild.voice_client.is_playing():
            if self.db.get_autoplay(message.guild.id) and added:
                await self._play_next(message.guild)
    
    # ========== Admin Commands ==========

    @app_commands.command(name="health", description="Check bot health")
    @admin_only()
    @safe_defer
    async def health(self, interaction: discord.Interaction):
        stats = await self.health.get_stats()
        orphaned = self.db.validate_files()
        
        embed = create_embed(color=Colors.PRIMARY)
        embed.title = "Bot Health"
        
        # Status indicator
        status = "Healthy" if stats['recent_failures'] < 5 else "Degraded" if stats['recent_failures'] < 10 else "Issues"
        embed.description = f"**Status** › {status}"
        
        # Stats
        stats_text = (
            f"**Guilds** › {stats['guilds']}\n"
            f"**Voice Connections** › {stats['voice_connections']}\n"
            f"**Uptime** › {stats['uptime_hours']:.1f}h"
        )
        embed.add_field(name="Statistics", value=stats_text, inline=True)
        
        # Issues
        issues_text = (
            f"**Recent Failures** › {stats['recent_failures']}\n"
            f"**Orphaned Files** › {orphaned}"
        )
        embed.add_field(name="Maintenance", value=issues_text, inline=True)
        
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="settings", description="View and change server settings")
    @admin_only()
    @safe_defer
    async def settings(self, interaction: discord.Interaction):
        view  = SettingsView(self, interaction.guild, interaction.user)
        embed = view.build_embed()
        await interaction.followup.send(embed=embed, view=view)
    
    # ========== Playback Commands ==========

    @app_commands.command(name="play", description="Open the player, or queue a library track by name")
    @app_commands.describe(name="Library track to queue (leave empty to open the player hub)")
    @check_permissions()
    @safe_defer
    async def play(self, interaction: discord.Interaction, name: str = None):
        state = await self._get_state(interaction)

        # Queue a named library track
        if name:
            track_data = self.db.get_track(interaction.guild_id, name)
            if not track_data:
                await interaction.followup.send(
                    embed=error_embed(f"Sound **{name}** not found in library."), ephemeral=True)
                return
            track = AudioTrack(
                url=track_data['file_path'],
                filename=track_data['filename'],
                requester=interaction.user.display_name,
                file_size=track_data['file_size'],
                is_permanent=True
            )
            track.downloaded_path = track_data['file_path']
            track.duration = track.get_metadata(track_data['file_path'])
            state.queue.append(track)

        # Connect to VC if user is in one
        if interaction.user.voice and not interaction.guild.voice_client:
            vc = await self.voice.connect(interaction.user.voice.channel)
            if not vc:
                await interaction.followup.send(
                    embed=error_embed("Failed to connect to voice channel."), ephemeral=True)
                return

        vc = interaction.guild.voice_client

        # Resume if paused and no specific track was queued
        if vc and vc.is_paused() and not name:
            if state.current_track:
                state.current_track.resume_playback()
            vc.resume()

        # Start playback if connected and queue has something
        start_playback = (vc and not vc.is_playing() and not vc.is_paused() and state.queue)
        if start_playback:
            if state.queue_position < 0:
                state.queue_position = 0
            state.is_stopped = False

        # Send PlayerHub with artwork, then start playback so it edits in place
        hub   = PlayerHubView(self, interaction.guild)
        embed = hub.build_embed()
        track = state.current_track
        if track and track.cover_url:
            embed.set_thumbnail(url=track.cover_url)
            msg = await interaction.followup.send(embed=embed, view=hub)
        else:
            embed.set_thumbnail(url="attachment://cover.png")
            msg = await interaction.followup.send(embed=embed, view=hub,
                                                   file=self._cover_file(track))
            if msg.attachments and track:
                track.cover_url     = msg.attachments[0].url
                state.last_np_track = track

        state.last_np_message = msg

        if start_playback:
            await self._play_next(interaction.guild, force=True, advance=False)

    @app_commands.command(name="volume", description="Set volume (0-120)")
    @check_permissions()
    @safe_defer
    async def volume(self, interaction: discord.Interaction, volume: int):
        if not 0 <= volume <= 120:
            await interaction.followup.send(embed=warning_embed("Volume must be 0-120!"), ephemeral=True)
            return
        
        state = await self._get_state(interaction)
        state.volume = volume
        
        vc = interaction.guild.voice_client
        if vc and vc.source:
            vc.source.volume = volume / 100
        
        emoji = EMOJI['mute'] if volume == 0 else EMOJI['volume']
        await interaction.followup.send(embed=success_embed("Volume", f"{emoji} Volume set to **{volume}%**"))
    
    @app_commands.command(name="speed", description="Set playback speed")
    @app_commands.choices(speed=[
        app_commands.Choice(name="0.5×  Slow",     value=50),
        app_commands.Choice(name="0.75×",           value=75),
        app_commands.Choice(name="1×  Normal",      value=100),
        app_commands.Choice(name="1.25×",           value=125),
        app_commands.Choice(name="1.5×  Fast",      value=150),
        app_commands.Choice(name="2×  Very Fast",   value=200),
    ])
    @check_permissions()
    @safe_defer
    async def speed(self, interaction: discord.Interaction, speed: app_commands.Choice[int]):
        await self._get_state(interaction)
        self.db.set_speed(interaction.guild_id, speed.value)
        await interaction.followup.send(embed=success_embed(
            "Speed Changed",
            f"{speed.name} — applies to next track."
        ), ephemeral=True)
    
    @app_commands.command(name="loop", description="Toggle infinite loop, or pass a number to loop that many times")
    @app_commands.describe(times="Number of times to loop (0 = disable, omit = toggle infinite)")
    @check_permissions()
    @safe_defer
    async def loop(self, interaction: discord.Interaction, times: int = None):
        state = await self._get_state(interaction)

        if not state.current_track:
            await interaction.followup.send(embed=warning_embed("Nothing is playing!"), ephemeral=True)
            return

        if times is None:
            # Toggle: disable any active loop, or enable infinite if off
            if state.loop_enabled:
                state.loop_enabled = False
                state.loop_count   = 0
                state.max_loops    = None
                msg = "Loop off."
            else:
                state.loop_enabled = True
                state.loop_count   = 0
                state.max_loops    = None
                msg = f"{EMOJI['loop']} Looping infinitely."
            await interaction.followup.send(embed=success_embed("Loop", msg), ephemeral=True)
        elif times <= 0:
            state.loop_enabled = False
            state.loop_count   = 0
            state.max_loops    = None
            await interaction.followup.send(embed=success_embed("Loop", "Loop off."), ephemeral=True)
        else:
            state.loop_enabled = True
            state.loop_count   = 0
            state.max_loops    = times
            await interaction.followup.send(embed=success_embed(
                "Loop", f"{EMOJI['loop']} Looping **{times}×**."), ephemeral=True)

        await self._update_np_embed(interaction.guild)
    
    @app_commands.command(name="forward", description="Skip forward by seconds")
    @check_permissions()
    @safe_defer
    async def forward(self, interaction: discord.Interaction, seconds: int):
        await self._seek_relative(interaction, seconds)
    
    @app_commands.command(name="backward", description="Skip backward by seconds")
    @check_permissions()
    @safe_defer
    async def backward(self, interaction: discord.Interaction, seconds: int):
        await self._seek_relative(interaction, -seconds)
    
    async def _seek_relative(self, interaction: discord.Interaction, delta: int):
        """Seek forward/backward by delta seconds"""
        vc = interaction.guild.voice_client
        if not vc or (not vc.is_playing() and not vc.is_paused()):
            await interaction.followup.send(embed=warning_embed("Nothing is playing!"), ephemeral=True)
            return

        state = await self._get_state(interaction)
        track = state.current_track
        if not track:
            return

        if not track.duration:
            await interaction.followup.send(embed=warning_embed("Track duration unknown — can't seek."), ephemeral=True)
            return

        new_pos = max(0, min(track.get_current_position() + delta, track.duration - 1))

        state.is_seeking = True
        vc.stop()
        await asyncio.sleep(0.3)

        track.position = new_pos
        speed = self.db.get_speed(interaction.guild_id)
        source = self._get_audio_source(track, new_pos, speed)
        source.volume = state.volume / 100

        vc.play(source, after=lambda e: self._seek_done(state, interaction.guild))
        state.is_seeking = False

        direction = "forward" if delta > 0 else "backward"
        await interaction.followup.send(embed=success_embed(
            f"Seeked {direction.title()}",
            f"Now at {format_duration(new_pos)} / {format_duration(track.duration)}"
        ), ephemeral=True)
    
    def _seek_done(self, state, guild):
        """Callback after seek playback ends"""
        state.last_activity = time.time()
        if not state.is_seeking and not state.manual_queue_seek and not state.is_stopped:
            asyncio.run_coroutine_threadsafe(self._play_next(guild), self.bot.loop)
    
    @app_commands.command(name="seek", description="Jump to position (h:m:s or seconds)")
    @app_commands.describe(
        hours="Hours (optional, default 0)",
        minutes="Minutes (optional, default 0)", 
        seconds="Seconds"
    )
    @check_permissions()
    @safe_defer
    async def seek(self, interaction: discord.Interaction, seconds: int, minutes: int = 0, hours: int = 0):
        target = hours * 3600 + minutes * 60 + seconds

        vc = interaction.guild.voice_client
        if not vc or (not vc.is_playing() and not vc.is_paused()):
            await interaction.followup.send(embed=warning_embed("Nothing is playing!"), ephemeral=True)
            return

        state = await self._get_state(interaction)
        track = state.current_track

        if not track.duration:
            await interaction.followup.send(embed=warning_embed("Track duration unknown — can't seek."), ephemeral=True)
            return

        if target < 0:
            await interaction.followup.send(embed=warning_embed("Position must be positive!"), ephemeral=True)
            return

        if target >= track.duration:
            await interaction.followup.send(embed=warning_embed(
                f"Position exceeds track duration ({format_duration(track.duration)})"
            ), ephemeral=True)
            return
        
        state.is_seeking = True
        vc.stop()
        await asyncio.sleep(0.3)
        
        track.position = target
        speed = self.db.get_speed(interaction.guild_id)
        source = self._get_audio_source(track, target, speed)
        source.volume = state.volume / 100
        
        vc.play(source, after=lambda e: self._seek_done(state, interaction.guild))
        state.is_seeking = False
        
        await interaction.followup.send(embed=success_embed(
            "Position Set",
            f"Jumped to {format_duration(target)}"
        ))
    
    # ========== Library Commands ==========
    
    @app_commands.command(name="upload", description="Upload file or URL to sound library")
    @app_commands.describe(name="Name for the sound", file="Audio file to upload", url="Or provide a CDN link/URL")
    @check_permissions()
    @safe_defer
    async def upload(self, interaction: discord.Interaction, name: str, file: discord.Attachment = None, url: str = None):
        # Must provide either file or URL
        if not file and not url:
            await interaction.followup.send(embed=warning_embed(
                "Please provide either a file attachment or a URL!"
            ))
            return
        
        if file and url:
            await interaction.followup.send(embed=warning_embed(
                "Please provide only a file OR a URL, not both!"
            ))
            return
        
        # Check if name exists
        if self.db.get_track(interaction.guild_id, name):
            await interaction.followup.send(embed=warning_embed(f"'{name}' already exists in library!"))
            return
        
        ALLOWED_EXTS = {'.mp3', '.wav', '.ogg', '.flac', '.m4a', '.aac', '.mp4', '.webm'}
        
        # Handle file upload
        if file:
            # Validate extension
            ext = os.path.splitext(file.filename)[1].lower()
            if ext not in ALLOWED_EXTS:
                await interaction.followup.send(embed=error_embed(
                    f"Unsupported format! Supported: {', '.join(ALLOWED_EXTS)}"
                ))
                return
            
            # Check storage
            if not self.db.can_store(interaction.guild_id, file.size):
                storage = self.db.get_storage(interaction.guild_id)
                await interaction.followup.send(embed=error_embed(
                    f"Storage full! {storage['used'] / 1024 / 1024:.1f}MB / {storage['max'] / 1024 / 1024:.1f}MB"
                ))
                return
            
            # Download and save
            perm_folder = 'permanent'
            os.makedirs(perm_folder, exist_ok=True)
            
            safe_name = ''.join(c for c in file.filename if c.isalnum() or c in '._- ')
            file_path = os.path.join(perm_folder, f"{interaction.guild_id}_{safe_name}")
            
            await file.save(file_path)
            
            self.db.add_track(
                interaction.guild_id, name, file.filename, file_path,
                interaction.user.display_name, file.size
            )
            
            await interaction.followup.send(embed=success_embed(
                "Uploaded to Library",
                f"{EMOJI['music']} **{name}** saved to library ({file.size / 1024 / 1024:.1f}MB)\n"
                f"Use `/play {name}` to play"
            ))
            
            logging.info(f"Uploaded file to library: {name} ({file.filename})")
        
        # Handle URL
        else:
            # Validate URL format
            if not url.startswith('http://') and not url.startswith('https://'):
                await interaction.followup.send(embed=error_embed("Invalid URL! Must start with http:// or https://"))
                return
            
            # Extract filename and validate extension
            filename = url.split('/')[-1].split('?')[0]
            
            # Try to detect extension
            ext = os.path.splitext(filename)[1].lower()
            if not ext:
                for allowed_ext in ALLOWED_EXTS:
                    if allowed_ext in url.lower():
                        ext = allowed_ext
                        filename = f"{name}{ext}"
                        break
                else:
                    ext = '.mp3'  # Default
                    filename = f"{name}.mp3"
            
            if ext not in ALLOWED_EXTS:
                await interaction.followup.send(embed=error_embed(
                    f"Unsupported format! Supported: {', '.join(ALLOWED_EXTS)}"
                ))
                return
            
            # Try to get file size
            file_size = 0
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.head(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status != 200:
                            await interaction.followup.send(embed=error_embed(f"Failed to access URL (Status {resp.status})"))
                            return
                        file_size = int(resp.headers.get('content-length', 0))
                        if file_size == 0:
                            file_size = 5 * 1024 * 1024  # 5MB estimate
            except Exception as e:
                logging.warning(f"Failed to get file size for {url}: {e}")
                file_size = 5 * 1024 * 1024  # 5MB estimate
            
            # Check storage
            if not self.db.can_store(interaction.guild_id, file_size):
                storage = self.db.get_storage(interaction.guild_id)
                await interaction.followup.send(embed=error_embed(
                    f"Storage full! {storage['used'] / 1024 / 1024:.1f}MB / {storage['max'] / 1024 / 1024:.1f}MB"
                ))
                return
            
            # Download the file
            perm_folder = 'permanent'
            os.makedirs(perm_folder, exist_ok=True)
            
            safe_name = ''.join(c for c in filename if c.isalnum() or c in '._- ')
            file_path = os.path.join(perm_folder, f"{interaction.guild_id}_{safe_name}")
            
            try:
                await interaction.followup.send(embed=info_embed(
                    "Downloading...",
                    f"{EMOJI['loading']} Downloading audio file to library..."
                ))
                
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                        if resp.status != 200:
                            await interaction.followup.send(embed=error_embed(f"Download failed (Status {resp.status})"))
                            return
                        
                        with open(file_path, 'wb') as f:
                            async for chunk in resp.content.iter_chunked(8192):
                                f.write(chunk)
                
                # Get actual file size
                actual_size = os.path.getsize(file_path)
                
                # Add to database
                self.db.add_track(
                    interaction.guild_id, name, filename, file_path,
                    interaction.user.display_name, actual_size
                )
                
                await interaction.followup.send(embed=success_embed(
                    "Saved to Library",
                    f"{EMOJI['music']} **{name}** saved to library ({actual_size / 1024 / 1024:.1f}MB)\n"
                    f"Use `/play {name}` to play"
                ))
                
                logging.info(f"Saved URL to library: {name} ({url})")
                
            except asyncio.TimeoutError:
                if os.path.exists(file_path):
                    os.remove(file_path)
                await interaction.followup.send(embed=error_embed("Download timed out! Try a smaller file."))
            except Exception as e:
                if os.path.exists(file_path):
                    os.remove(file_path)
                await interaction.followup.send(embed=error_embed(f"Download failed: {str(e)[:100]}"))
                logging.error(f"Failed to download URL to library: {e}")
    
    # ========== Queue / Hub Shortcut Commands ==========

    @app_commands.command(name="playing", description="Open the player hub")
    @check_permissions()
    @safe_defer
    async def playing(self, interaction: discord.Interaction):
        state = await self._get_state(interaction)
        hub   = PlayerHubView(self, interaction.guild)
        embed = hub.build_embed()
        track = state.current_track
        if track and track.cover_url:
            embed.set_thumbnail(url=track.cover_url)
            msg = await interaction.followup.send(embed=embed, view=hub)
        else:
            embed.set_thumbnail(url="attachment://cover.png")
            msg = await interaction.followup.send(embed=embed, view=hub,
                                                   file=self._cover_file(track))
            if msg.attachments and track:
                track.cover_url     = msg.attachments[0].url
                state.last_np_track = track
        state.last_np_message = msg

    @app_commands.command(name="library", description="Open the library browser")
    @check_permissions()
    @safe_defer
    async def library(self, interaction: discord.Interaction):
        state = await self._get_state(interaction)
        tracks = self.db.list_tracks(interaction.guild_id)
        view   = LibraryView(tracks, interaction.guild_id, self, from_hub=True)
        msg    = await interaction.followup.send(embed=view.build_embed(), view=view)
        state.last_np_message = msg

    @app_commands.command(name="playlist", description="Open the playlist manager")
    @check_permissions()
    @safe_defer
    async def playlist(self, interaction: discord.Interaction):
        state = await self._get_state(interaction)
        view  = PlaylistManagerView(self, interaction.guild, interaction.user, from_hub=True)
        msg   = await interaction.followup.send(embed=view.build_embed(), view=view)
        state.last_np_message = msg

    @app_commands.command(name="clear", description="Clear queue and stop")
    @check_permissions()
    @safe_defer
    async def clear(self, interaction: discord.Interaction):
        state = await self._get_state(interaction)
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            state.is_stopped = True
            vc.stop()
        count = len(state.queue)
        for track in state.queue:
            if not track.is_permanent:
                track.cleanup()
        state.reset()
        await interaction.followup.send(embed=success_embed(
            "Queue Cleared", f"Removed {count} track(s) from queue"))

    @app_commands.command(name="remove", description="Remove a track from the queue by position")
    @app_commands.describe(position="Queue position (1 = first)")
    @check_permissions()
    @safe_defer
    async def remove(self, interaction: discord.Interaction, position: int):
        state = await self._get_state(interaction)
        if not state.queue:
            await interaction.followup.send(embed=warning_embed("Queue is empty!"), ephemeral=True)
            return
        if not 1 <= position <= len(state.queue):
            await interaction.followup.send(embed=warning_embed(
                f"Position must be 1–{len(state.queue)}"), ephemeral=True)
            return
        if position - 1 == state.queue_position:
            await interaction.followup.send(embed=warning_embed(
                "Can't remove the currently playing track."), ephemeral=True)
            return
        track = state.queue.pop(position - 1)
        if position - 1 < state.queue_position:
            state.queue_position -= 1
        track.cleanup()
        await interaction.followup.send(embed=success_embed(
            "Track Removed", f"Removed: **{track.get_display_name()}**"))

    @app_commands.command(name="disconnect", description="Disconnect from voice and clear queue")
    @check_permissions()
    @safe_defer
    async def disconnect(self, interaction: discord.Interaction):
        state = await self._get_state(interaction)
        vc = interaction.guild.voice_client
        if not vc:
            await interaction.followup.send(embed=warning_embed("Not connected to voice!"), ephemeral=True)
            return
        if vc.is_playing():
            state.is_stopped = True
            vc.stop()
        await asyncio.sleep(0.3)
        for track in state.queue:
            if track.downloaded_path:
                self.tracks.mark_inactive(track.downloaded_path)
            if track.converted_path:
                self.tracks.mark_inactive(track.converted_path)
            if not track.is_permanent:
                track.cleanup()
        count = len(state.queue)
        state.reset()
        await vc.disconnect()
        await interaction.followup.send(embed=success_embed(
            "Disconnected", f"Left voice and cleared {count} track(s)."))

    # ---- Autocomplete ----

    async def _library_track_autocomplete(self, interaction: discord.Interaction, current: str):
        tracks = self.db.list_tracks(interaction.guild_id)
        return [
            app_commands.Choice(name=t['track_name'], value=t['track_name'])
            for t in tracks if current.lower() in t['track_name'].lower()
        ][:25]

    @play.autocomplete('name')
    async def _play_name(self, interaction: discord.Interaction, current: str):
        return await self._library_track_autocomplete(interaction, current)

    # ========== Radio Commands ==========
    
    
    # ========== Help Command ==========
    
    @app_commands.command(name="help", description="Show all commands")
    async def help(self, interaction: discord.Interaction):
        embed = create_embed(color=Colors.PRIMARY)
        embed.title = f"{EMOJI['music']} SporkMP3"
        embed.description = (
            "Use `/play`, `/library`, or `/playlist` to open the hub panels.\n"
            "Power users can also use the queue commands directly."
        )

        embed.add_field(
            name="Player",
            value=(
                "`/play [name]` › Open the Player Hub (or queue a named library track)\n"
                "`/playing` › Re-open the Player Hub\n"
                "`/library` › Open the Library browser\n"
                "`/playlist` › Open the Playlist manager\n"
                "**Hub buttons** › ◀◀ ▶/‖ ■ ▶▶ ↻ transport · Library · Playlists · Queue"
            ),
            inline=False
        )

        embed.add_field(
            name="Playback",
            value=(
                "`/volume <0-120>` › Set volume\n"
                "`/speed` › Adjust playback speed\n"
                "`/loop` › Toggle infinite loop · `/loop 3` › Loop 3× · `/loop 0` › Off\n"
                "`/forward <s>` `/backward <s>` › Seek by seconds\n"
                "`/seek` › Jump to specific timestamp"
            ),
            inline=False
        )

        embed.add_field(
            name="Queue",
            value=(
                "`/clear` › Clear queue and stop\n"
                "`/remove <pos>` › Remove a track by position\n"
                "`/disconnect` › Leave voice and clear queue"
            ),
            inline=False
        )

        embed.add_field(
            name="Library",
            value=(
                "`/upload <name>` › Save a file or URL to the library\n"
                "**In Library panel** › ↑ Upload URL · Remove Track *(admin)*"
            ),
            inline=False
        )

        embed.add_field(
            name="Admin",
            value=(
                "`/settings` › Autoplay, auto-disconnect, speed, access control\n"
                "`/health` › Bot health and diagnostic stats"
            ),
            inline=False
        )

        embed.add_field(
            name="Tips",
            value=(
                "**Mention the bot** with audio files or CDN links to queue them instantly\n"
                "**Playlists** group library tracks — manage them inside the Playlists panel"
            ),
            inline=False
        )

        await interaction.response.send_message(embed=embed)


async def setup(bot):
    await bot.add_cog(Music(bot))