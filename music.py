"""
Main music cog for SporkMP3 bot.
Combines commands, playback logic, and event handling.
"""
import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import logging
import time
import os
import re
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


class NowPlayingView(discord.ui.View):
    """Persistent Now Playing embed with playback control buttons"""

    def __init__(self, music_cog, guild: discord.Guild):
        super().__init__(timeout=3600)
        self.music_cog = music_cog
        self.guild = guild
        self._refresh_buttons()

    def _refresh_buttons(self):
        self.clear_items()
        vc = self.guild.voice_client
        state = self.music_cog.state.guild_states.get(self.guild.id)

        is_paused  = bool(vc and vc.is_paused())
        is_active  = bool(vc and (vc.is_playing() or vc.is_paused()))
        at_start   = not state or state.queue_position <= 0
        at_end     = not state or state.queue_position >= len(state.queue) - 1
        loop_on    = bool(state and state.loop_enabled)

        # ── Transport controls ─────────────────────────────────────────────
        b = discord.ui.Button(emoji="⏮️", style=discord.ButtonStyle.secondary,
                              disabled=at_start or not is_active, row=0)
        b.callback = self._on_previous
        self.add_item(b)

        b = discord.ui.Button(
            emoji="▶️" if is_paused else "⏸️",
            style=discord.ButtonStyle.success if is_paused else discord.ButtonStyle.secondary,
            disabled=not is_active, row=0
        )
        b.callback = self._on_pause_resume
        self.add_item(b)

        b = discord.ui.Button(emoji="⏹️", style=discord.ButtonStyle.danger,
                              disabled=not is_active, row=0)
        b.callback = self._on_stop
        self.add_item(b)

        b = discord.ui.Button(emoji="⏭️", style=discord.ButtonStyle.secondary,
                              disabled=at_end or not is_active, row=0)
        b.callback = self._on_skip
        self.add_item(b)

        b = discord.ui.Button(
            emoji="🔄",
            style=discord.ButtonStyle.success if loop_on else discord.ButtonStyle.secondary,
            row=0
        )
        b.callback = self._on_loop
        self.add_item(b)

    # ---- helpers ----

    async def _check_perms(self, interaction: discord.Interaction) -> bool:
        mc = self.music_cog

        # Must be in the same voice channel as the bot
        vc = self.guild.voice_client
        if vc and vc.channel:
            if not interaction.user.voice or interaction.user.voice.channel != vc.channel:
                await interaction.response.send_message(
                    embed=error_embed("You must be in the voice channel to control playback."),
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
        """Rebuild buttons + embed and edit this message"""
        state = self.music_cog.state.guild_states.get(self.guild.id)
        self._refresh_buttons()
        if state and state.current_track:
            embed = self.music_cog._build_now_playing_embed(self.guild, state)
        else:
            embed = create_embed(color=Colors.PRIMARY)
            embed.title = f"{EMOJI['stop']} Stopped"
            embed.description = "Playback stopped. Queue is intact — use `/play` to resume."
        await interaction.response.edit_message(embed=embed, view=self)

    # ---- button callbacks ----

    async def _on_pause_resume(self, interaction: discord.Interaction):
        if not await self._check_perms(interaction):
            return
        vc = self.guild.voice_client
        state = self.music_cog.state.guild_states.get(self.guild.id)
        if vc and vc.is_playing():
            if state and state.current_track:
                state.current_track.pause_playback()
            vc.pause()
        elif vc and vc.is_paused():
            if state and state.current_track:
                state.current_track.resume_playback()
            vc.resume()
        await self._edit_in_place(interaction)

    async def _on_stop(self, interaction: discord.Interaction):
        if not await self._check_perms(interaction):
            return
        vc = self.guild.voice_client
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

        # Defer immediately so Discord doesn't time out while we do async work
        await interaction.response.defer()

        # Take manual control — prevents after-callback from also advancing
        state.loop_enabled     = False
        state.loop_count       = 0
        state.max_loops        = None
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
                state.max_loops = None
        await self._edit_in_place(interaction)

    async def on_timeout(self):
        """Disable all buttons when the view expires"""
        self.clear_items()
        state = self.music_cog.state.guild_states.get(self.guild.id)
        if state and state.last_np_message:
            try:
                await state.last_np_message.edit(view=self)
            except Exception:
                pass


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
        
        if not interaction.guild.voice_client.is_playing():
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
    
    def __init__(self, tracks: list, guild_id: int, music_cog, page: int = 0):
        super().__init__(timeout=120)
        self.all_tracks = tracks
        self.guild_id = guild_id
        self.music_cog = music_cog
        self.page = page
        self.max_page = max(0, (len(tracks) - 1) // self.TRACKS_PER_PAGE)
        self._update_components()
    
    def _page_tracks(self):
        start = self.page * self.TRACKS_PER_PAGE
        return self.all_tracks[start:start + self.TRACKS_PER_PAGE]
    
    def _update_components(self):
        self.clear_items()
        page_tracks = self._page_tracks()
        if page_tracks:
            self.add_item(LibrarySelect(page_tracks, self.page, self.music_cog))
        if self.max_page > 0:
            prev_btn = discord.ui.Button(label="◀ Prev", style=discord.ButtonStyle.secondary, disabled=self.page == 0)
            prev_btn.callback = self._prev
            self.add_item(prev_btn)
            
            page_btn = discord.ui.Button(label=f"{self.page + 1}/{self.max_page + 1}", style=discord.ButtonStyle.primary, disabled=True)
            self.add_item(page_btn)
            
            next_btn = discord.ui.Button(label="Next ▶", style=discord.ButtonStyle.secondary, disabled=self.page >= self.max_page)
            next_btn.callback = self._next
            self.add_item(next_btn)
    
    def _build_embed(self):
        page_tracks = self._page_tracks()
        embed = create_embed(color=Colors.PRIMARY)
        embed.title = f"{EMOJI['cd']} Sound Library"
        
        lines = []
        start_idx = self.page * self.TRACKS_PER_PAGE
        for i, t in enumerate(page_tracks):
            size_mb = t['file_size'] / 1024 / 1024
            lines.append(f"`{start_idx + i + 1}.` **{t['track_name']}** › `{t['filename'][:30]}` ({size_mb:.1f}MB)")
        
        embed.description = "\n".join(lines) if lines else "No tracks found."
        
        storage = self.music_cog.db.get_storage(self.guild_id)
        used_mb = storage['used'] / 1024 / 1024
        max_mb = storage['max'] / 1024 / 1024
        percent = (used_mb / max_mb * 100) if max_mb > 0 else 0
        embed.add_field(
            name="Storage",
            value=f"**{used_mb:.1f}MB** / {max_mb:.1f}MB ({percent:.0f}%) • {len(self.all_tracks)} track(s)",
            inline=False
        )
        embed.set_footer(text="Select tracks from the dropdown to add to queue")
        return embed
    
    async def _prev(self, interaction: discord.Interaction):
        self.page = max(0, self.page - 1)
        self._update_components()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)
    
    async def _next(self, interaction: discord.Interaction):
        self.page = min(self.max_page, self.page + 1)
        self._update_components()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)
    
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
                    f"**{name}** is ready.\nHit **🔄 Refresh** in the manager to see it."),
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                embed=warning_embed(f"**{name}** already exists!"), ephemeral=True
            )


class PlaylistManagerView(discord.ui.View):
    """Top-level playlist browser — select a playlist then act on it."""

    def __init__(self, music_cog, guild: discord.Guild, invoker: discord.Member):
        super().__init__(timeout=300)
        self.music_cog = music_cog
        self.guild     = guild
        self.invoker   = invoker
        self.selected  = None   # currently selected playlist name
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
                    default=(p['playlist_name'] == self.selected),
                    emoji="🎵"
                ))
            sel = discord.ui.Select(placeholder="Select a playlist...", options=options, row=0)
            sel.callback = self._on_select
            self.add_item(sel)

        # ── Row 1: Action buttons ──────────────────────────────────────────
        b = discord.ui.Button(label="▶ Play All", style=discord.ButtonStyle.success,
                              disabled=not has_sel, row=1)
        b.callback = self._on_play_all
        self.add_item(b)

        b = discord.ui.Button(label="👁 View Tracks", style=discord.ButtonStyle.primary,
                              disabled=not has_sel, row=1)
        b.callback = self._on_view
        self.add_item(b)

        if is_admin:
            b = discord.ui.Button(label="➕ Add Tracks", style=discord.ButtonStyle.secondary,
                                  disabled=not has_sel, row=1)
            b.callback = self._on_add_tracks
            self.add_item(b)

            b = discord.ui.Button(label="🗑 Delete", style=discord.ButtonStyle.danger,
                                  disabled=not has_sel, row=1)
            b.callback = self._on_delete
            self.add_item(b)

        # ── Row 2: Create + Refresh ────────────────────────────────────────
        if is_admin:
            b = discord.ui.Button(label="➕ Create New Playlist",
                                  style=discord.ButtonStyle.secondary, row=2)
            b.callback = self._on_create
            self.add_item(b)

        b = discord.ui.Button(label="🔄 Refresh", style=discord.ButtonStyle.secondary, row=2)
        b.callback = self._on_refresh
        self.add_item(b)

    def build_embed(self) -> discord.Embed:
        playlists = self.music_cog.db.list_playlists(self.guild.id)
        embed = create_embed(color=Colors.PRIMARY)
        embed.title = f"{EMOJI['queue']} Playlists"

        if not playlists:
            embed.description = (
                "No playlists yet.\n"
                "Admins can create one with **➕ Create New Playlist**."
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
        if not interaction.guild.voice_client.is_playing() and state:
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
                                      self.guild, self.invoker)
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
        view  = PlaylistDeleteConfirmView(self.selected, self.music_cog, self.guild, self.invoker)
        embed = create_embed(color=Colors.ERROR)
        embed.title = "🗑 Delete Playlist"
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

    async def on_timeout(self):
        self.clear_items()


class PlaylistDeleteConfirmView(discord.ui.View):
    """Inline confirm/cancel for playlist deletion"""

    def __init__(self, playlist_name: str, music_cog, guild: discord.Guild, invoker: discord.Member):
        super().__init__(timeout=60)
        self.playlist_name = playlist_name
        self.music_cog     = music_cog
        self.guild         = guild
        self.invoker       = invoker

        b = discord.ui.Button(label="Yes, delete it", style=discord.ButtonStyle.danger, row=0)
        b.callback = self._confirm
        self.add_item(b)

        b = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary, row=0)
        b.callback = self._cancel
        self.add_item(b)

    async def _confirm(self, interaction: discord.Interaction):
        self.music_cog.db.delete_playlist(self.guild.id, self.playlist_name)
        view = PlaylistManagerView(self.music_cog, self.guild, self.invoker)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    async def _cancel(self, interaction: discord.Interaction):
        view = PlaylistManagerView(self.music_cog, self.guild, self.invoker)
        view.selected = self.playlist_name
        view._rebuild()
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class PlaylistDetailView(discord.ui.View):
    """Paginated playlist track viewer with queue controls and nav back"""

    TRACKS_PER_PAGE = 10

    def __init__(self, name: str, playlist: dict, tracks: list,
                 music_cog, guild: discord.Guild, invoker: discord.Member, page: int = 0):
        super().__init__(timeout=300)
        self.name       = name
        self.playlist   = playlist or {}
        self.all_tracks = tracks
        self.music_cog  = music_cog
        self.guild      = guild
        self.invoker    = invoker
        self.page       = page
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
                    value=t['track_name'],
                    emoji="🎵"
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
            b = discord.ui.Button(label="➕ Add Tracks", style=discord.ButtonStyle.secondary, row=1)
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
            embed.description += "This playlist is empty. Use **➕ Add Tracks** to populate it."
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

        if not self.guild.voice_client.is_playing():
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
        if not self.guild.voice_client.is_playing() and state:
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

        view = PlaylistAddView(available, self.name, self.music_cog, self.guild, self.invoker)
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
        view          = PlaylistManagerView(self.music_cog, self.guild, self.invoker)
        view.selected = self.name
        view._rebuild()
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    async def on_timeout(self):
        self.clear_items()


class PlaylistAddView(discord.ui.View):
    """Paginated library browser for adding tracks to a playlist (admin)"""

    TRACKS_PER_PAGE = 10

    def __init__(self, available: list, playlist_name: str,
                 music_cog, guild: discord.Guild, invoker: discord.Member, page: int = 0):
        super().__init__(timeout=300)
        self.available     = available
        self.playlist_name = playlist_name
        self.music_cog     = music_cog
        self.guild         = guild
        self.invoker       = invoker
        self.page          = page
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
                    value=t['track_name'],
                    emoji="🎵"
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
        embed.title = f"➕ Add Tracks to {self.playlist_name}"

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
                                      self.music_cog, self.guild, self.invoker)
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
            placeholder="➕ Add role to whitelist...",
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
                    value=str(rid),
                    emoji="🔐"
                ))
            rm_sel = discord.ui.Select(
                placeholder="🗑 Remove role from whitelist...",
                options=options,
                min_values=1,
                max_values=min(len(options), 5),
                row=1
            )
            rm_sel.callback = self._on_remove_role
            self.add_item(rm_sel)

        # ── Row 2: Add user to blacklist ───────────────────────────────────
        user_sel = discord.ui.UserSelect(
            placeholder="🚫 Blacklist a user...",
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
                opts.append(discord.SelectOption(label=label, value=str(uid), emoji="🚫"))
            unban_sel = discord.ui.Select(
                placeholder="✅ Remove user from blacklist...",
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
        embed.title = "🔐 Access Control"
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

        embed.add_field(name="🔐 Role Whitelist", value=roles_val, inline=False)

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

        embed.add_field(name="🚫 Blacklisted Users", value=bl_val, inline=False)

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
            label=f"Autoplay: {'ON ✅' if ap else 'OFF ❌'}",
            style=discord.ButtonStyle.success if ap else discord.ButtonStyle.secondary,
            row=0
        )
        b.callback = self._toggle_autoplay
        self.add_item(b)

        b = discord.ui.Button(
            label=f"Auto-disconnect: {'ON ✅' if ad else 'OFF ❌'}",
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
            label=f"⚡ {spd}% Speed",
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
            label="🔐 Manage Access",
            style=discord.ButtonStyle.primary,
            row=2
        )
        b.callback = self._open_access
        self.add_item(b)

        b = discord.ui.Button(
            label="🔄 Refresh",
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
            name="▶️ Playback",
            value=(
                f"**Autoplay** › {'✅ On' if ap else '❌ Off'}\n"
                f"**Auto-disconnect** › {'✅ On' if ad else '❌ Off'}\n"
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
            name="🔐 Access",
            value=f"**Whitelisted Roles**\n{roles_val}",
            inline=True
        )

        # Storage
        bar_len  = 12
        filled   = int((used_mb / max_mb) * bar_len) if max_mb > 0 else 0
        bar      = "█" * filled + "░" * (bar_len - filled)
        embed.add_field(
            name="💾 Library Storage",
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
    """Paginated queue browser with jump-to select"""

    TRACKS_PER_PAGE = 10

    def __init__(self, state, music_cog, guild: discord.Guild, page: int = 0):
        super().__init__(timeout=120)
        self.state      = state
        self.music_cog  = music_cog
        self.guild      = guild
        self.page       = page
        self.max_page   = max(0, (len(state.queue) - 1) // self.TRACKS_PER_PAGE)
        self._rebuild()

    def _page_tracks(self):
        s = self.page * self.TRACKS_PER_PAGE
        return self.state.queue[s:s + self.TRACKS_PER_PAGE]

    def _rebuild(self):
        self.clear_items()
        page_tracks = self._page_tracks()
        start_idx   = self.page * self.TRACKS_PER_PAGE

        # ── Row 0: Jump-to select ──────────────────────────────────────────
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
                    emoji="▶️" if is_cur else None
                ))
            sel = discord.ui.Select(placeholder="Jump to track...", options=options, row=0)
            sel.callback = self._on_jump
            self.add_item(sel)

        # ── Row 1: Pagination ──────────────────────────────────────────────
        if self.max_page > 0:
            b = discord.ui.Button(label="◀ Prev", style=discord.ButtonStyle.secondary,
                                  disabled=(self.page == 0), row=1)
            b.callback = self._prev
            self.add_item(b)

            self.add_item(discord.ui.Button(
                label=f"Page {self.page+1}/{self.max_page+1}",
                style=discord.ButtonStyle.primary,
                disabled=True, row=1
            ))

            b = discord.ui.Button(label="Next ▶", style=discord.ButtonStyle.secondary,
                                  disabled=(self.page >= self.max_page), row=1)
            b.callback = self._next
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
                icon = "▶️"
            elif abs_i < self.state.queue_position:
                icon = "✅"
            else:
                icon = "⏸️"
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
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _prev(self, interaction: discord.Interaction):
        self.page = max(0, self.page - 1)
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _next(self, interaction: discord.Interaction):
        self.page = min(self.max_page, self.page + 1)
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

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
        
        # Start background tasks
        bot.loop.create_task(self._cleanup_loop())
        bot.loop.create_task(self._activity_loop())
        bot.loop.create_task(self.health.monitor_loop())
        
        logging.info("Music cog loaded")
    
    async def cog_load(self):
        """Initialize on cog load"""
        await self.tracks.ensure_temp_folder()
        await self.tracks.cleanup_temp_files()
        orphaned = self.db.validate_files()
        if orphaned:
            logging.info(f"Startup: cleaned {orphaned} orphaned entries")
        
        # Update all guilds to use current storage limit from config
        updated = self.db.update_all_storage_limits()
        if updated:
            logging.info(f"Startup: updated storage limits for {updated} guild(s)")
    
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
        """Send or edit-in-place the Now Playing embed, plus a fresh track notification."""
        channel = guild.get_channel(state.last_channel_id)
        if not channel:
            return
        track = state.current_track
        if not track:
            return

        embed = self._build_now_playing_embed(guild, state)
        view  = NowPlayingView(self, guild)

        # ── 1. Track-change notification (always a new message) ───────────
        name     = track.get_display_name()
        pos      = state.queue_position + 1
        total    = len(state.queue)
        notif    = create_embed(color=Colors.PRIMARY)
        notif.description = (
            f"{EMOJI['play']} **Now Playing** — **{name}**\n"
            f"Track {pos}/{total}"
            + (f" · requested by {track.requester}" if track.requester else "")
        )
        try:
            await channel.send(embed=notif)
        except Exception as e:
            logging.warning(f"Failed to send track notification: {e}")

        # ── 2. NP embed — edit in place or create fresh ───────────────────
        if state.last_np_message:
            try:
                await state.last_np_message.edit(embed=embed, view=view)
                return
            except discord.NotFound:
                logging.debug("NP message deleted, will send fresh.")
                state.last_np_message = None
            except Exception as e:
                logging.warning(f"Failed to edit NP message: {type(e).__name__}: {e}")
                state.last_np_message = None

        try:
            state.last_np_message = await channel.send(embed=embed, view=view)
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

        # Queue
        remaining  = len(state.queue) - state.queue_position - 1
        queue_info = [f"**Position** › {state.queue_position + 1}/{len(state.queue)}"]
        if remaining > 0:
            queue_info.append(f"**Up next** › {remaining} track(s)")
        if state.loop_enabled:
            loop_text = "∞" if state.max_loops is None else f"{state.loop_count}/{state.max_loops}"
            queue_info.append(f"**Loop** › {EMOJI['loop']} {loop_text}")
        embed.add_field(name="Queue", value="\n".join(queue_info), inline=True)

        return embed

    async def _update_np_embed(self, guild: discord.Guild):
        """Silently refresh the NP embed from a slash command (no interaction needed)"""
        state = self.state.guild_states.get(guild.id)
        if not state or not state.last_np_message:
            return
        try:
            embed = self._build_now_playing_embed(guild, state)
            view  = NowPlayingView(self, guild)
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
                msg.append(f"✅ Added {len(added)} track(s)")
            if skipped:
                msg.append(f"❌ Skipped: {', '.join(skipped[:5])}")  # Limit skipped display
            
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
    
    @app_commands.command(name="blacklist", description="Add/remove user from blacklist")
    @app_commands.choices(action=[
        app_commands.Choice(name="Add to blacklist",      value="add"),
        app_commands.Choice(name="Remove from blacklist", value="remove"),
    ])
    @admin_only()
    @safe_defer
    async def blacklist(self, interaction: discord.Interaction,
                        action: app_commands.Choice[str], user: discord.Member):
        if action.value == 'add':
            self.db.add_blacklist(interaction.guild_id, user.id)
            await interaction.followup.send(embed=success_embed(
                "Blacklist Updated", f"{user.mention} has been **blacklisted**."), ephemeral=True)
        else:
            self.db.remove_blacklist(interaction.guild_id, user.id)
            await interaction.followup.send(embed=success_embed(
                "Blacklist Updated", f"{user.mention} has been **removed** from the blacklist."), ephemeral=True)
    
    @app_commands.command(name="role_config", description="Add/remove role from whitelist")
    @app_commands.choices(action=[
        app_commands.Choice(name="Add role to whitelist",      value="add"),
        app_commands.Choice(name="Remove role from whitelist", value="remove"),
    ])
    @admin_only()
    @safe_defer
    async def role_config(self, interaction: discord.Interaction,
                          action: app_commands.Choice[str], role: discord.Role):
        if action.value == 'add':
            self.db.add_role_whitelist(interaction.guild_id, role.id)
            await interaction.followup.send(embed=success_embed(
                "Role Whitelist Updated", f"{role.mention} **added** to whitelist."), ephemeral=True)
        else:
            self.db.remove_role_whitelist(interaction.guild_id, role.id)
            await interaction.followup.send(embed=success_embed(
                "Role Whitelist Updated", f"{role.mention} **removed** from whitelist."), ephemeral=True)
    
    @app_commands.command(name="autodisconnect", description="Toggle auto-disconnect when queue empty")
    @admin_only()
    @safe_defer
    async def autodisconnect(self, interaction: discord.Interaction, enabled: bool):
        self.db.set_autodisconnect(interaction.guild_id, enabled)
        status = "enabled" if enabled else "disabled"
        await interaction.followup.send(embed=success_embed("Auto-Disconnect", f"Auto-disconnect {status}"))
    
    @app_commands.command(name="autoplay", description="Toggle autoplay")
    @admin_only()
    @safe_defer
    async def autoplay(self, interaction: discord.Interaction, enabled: bool):
        self.db.set_autoplay(interaction.guild_id, enabled)
        self.state.alone_since.pop(interaction.guild_id, None)
        status = "enabled" if enabled else "disabled"
        await interaction.followup.send(embed=success_embed("Autoplay", f"Autoplay {status}"))
    
    @app_commands.command(name="health", description="Check bot health")
    @admin_only()
    @safe_defer
    async def health(self, interaction: discord.Interaction):
        stats = await self.health.get_stats()
        orphaned = self.db.validate_files()
        
        embed = create_embed(color=Colors.PRIMARY)
        embed.title = "🏥 Bot Health"
        
        # Status indicator
        status = "🟢 Healthy" if stats['recent_failures'] < 5 else "🟡 Degraded" if stats['recent_failures'] < 10 else "🔴 Issues"
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
    
    @app_commands.command(name="play", description="Play from queue or library")
    @app_commands.describe(name="Optional: library sound name to queue and play")
    @check_permissions()
    @safe_defer
    async def play(self, interaction: discord.Interaction, name: str = None):
        state = await self._get_state(interaction)

        if not interaction.user.voice:
            await interaction.followup.send(embed=warning_embed("Join a voice channel first!"), ephemeral=True)
            return

        # Queue a library track if name given
        if name:
            track_data = self.db.get_track(interaction.guild_id, name)
            if not track_data:
                await interaction.followup.send(embed=error_embed(f"Sound **{name}** not found in library."), ephemeral=True)
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
            await interaction.followup.send(
                embed=success_embed("Queued", f"{EMOJI['music']} **{name}** added to queue."),
                ephemeral=True
            )

        # Connect if needed
        if not interaction.guild.voice_client:
            vc = await self.voice.connect(interaction.user.voice.channel)
            if not vc:
                await interaction.followup.send(embed=error_embed("Failed to connect to voice channel."), ephemeral=True)
                return

        # Start or resume playback
        vc = interaction.guild.voice_client
        if vc.is_paused():
            if state.current_track:
                state.current_track.resume_playback()
            vc.resume()
            await self._update_np_embed(interaction.guild)
            if not name:
                await interaction.followup.send(embed=success_embed("Resumed", "Playback resumed!"), ephemeral=True)
        elif not vc.is_playing():
            if state.queue:
                if state.queue_position < 0:
                    state.queue_position = 0
                state.is_stopped = False
                await self._play_next(interaction.guild, force=True, advance=False)
                if not name:
                    await interaction.followup.send(embed=success_embed("Starting", "Starting playback…"), ephemeral=True)
            else:
                await interaction.followup.send(embed=warning_embed("Queue is empty! Upload some files first."), ephemeral=True)
        elif not name:
            await interaction.followup.send(embed=warning_embed("Already playing! Use `/skip`, `/stop`, or the player buttons."), ephemeral=True)
    
    @app_commands.command(name="pause", description="Pause playback")
    @check_permissions()
    @safe_defer
    async def pause(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if not vc or not vc.is_playing():
            await interaction.followup.send(embed=warning_embed("Nothing is playing!"), ephemeral=True)
            return

        state = await self._get_state(interaction)
        if state.current_track:
            state.current_track.pause_playback()
        vc.pause()

        await self._update_np_embed(interaction.guild)
        await interaction.followup.send(
            embed=success_embed("Paused", "Use `/resume`, or press ▶️ on the player to continue."),
            ephemeral=True
        )
    
    @app_commands.command(name="resume", description="Resume playback")
    @check_permissions()
    @safe_defer
    async def resume(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        state = await self._get_state(interaction)

        # Allow resume from stopped state if queue has tracks
        if vc and vc.is_paused():
            if state.current_track:
                state.current_track.resume_playback()
            vc.resume()
            await self._update_np_embed(interaction.guild)
            await interaction.followup.send(embed=success_embed("Resumed", "Playback resumed!"), ephemeral=True)
        elif not vc or (not vc.is_playing() and not vc.is_paused()):
            # Restart from stopped
            if state.queue and state.queue_position >= 0:
                state.is_stopped = False
                await self._play_next(interaction.guild, force=True, advance=False)
                await interaction.followup.send(embed=success_embed("Resumed", "Restarting playback!"), ephemeral=True)
            else:
                await interaction.followup.send(embed=warning_embed("Nothing to resume!"), ephemeral=True)
        else:
            await interaction.followup.send(embed=warning_embed("Already playing!"), ephemeral=True)
    
    @app_commands.command(name="stop", description="Stop playback (keeps queue)")
    @check_permissions()
    @safe_defer
    async def stop(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if not vc or (not vc.is_playing() and not vc.is_paused()):
            await interaction.followup.send(embed=warning_embed("Nothing is playing!"), ephemeral=True)
            return

        state = await self._get_state(interaction)
        if state.current_track:
            self.tracks.mark_inactive(state.current_track.downloaded_path)
            if state.current_track.converted_path:
                self.tracks.mark_inactive(state.current_track.converted_path)
        state.is_stopped = True
        vc.stop()

        await self._update_np_embed(interaction.guild)
        await interaction.followup.send(
            embed=success_embed("Stopped", "Queue is intact — use `/resume` or `/play` to continue."),
            ephemeral=True
        )
    
    @app_commands.command(name="skip", description="Skip to next track")
    @check_permissions()
    @safe_defer
    async def skip(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if not vc or (not vc.is_playing() and not vc.is_paused()):
            await interaction.followup.send(embed=warning_embed("Nothing is playing!"), ephemeral=True)
            return

        state = await self._get_state(interaction)
        if state.queue_position >= len(state.queue) - 1:
            await interaction.followup.send(embed=warning_embed("Already at the last track!"), ephemeral=True)
            return

        state.loop_enabled      = False
        state.loop_count        = 0
        state.max_loops         = None
        state.manual_queue_seek = True
        state.queue_position   += 1

        vc.stop()
        await asyncio.sleep(0.3)
        await self._play_next(interaction.guild, force=True, advance=False)
        state.manual_queue_seek = False

        await interaction.followup.send(
            embed=success_embed("Skipped", f"{EMOJI['skip']} Playing next track..."),
            ephemeral=True
        )

    @app_commands.command(name="previous", description="Go back to previous track")
    @check_permissions()
    @safe_defer
    async def previous(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if not vc or (not vc.is_playing() and not vc.is_paused()):
            await interaction.followup.send(embed=warning_embed("Nothing is playing!"), ephemeral=True)
            return

        state = await self._get_state(interaction)
        if state.queue_position <= 0:
            await interaction.followup.send(embed=warning_embed("Already at the first track!"), ephemeral=True)
            return

        state.loop_enabled      = False
        state.loop_count        = 0
        state.max_loops         = None
        state.manual_queue_seek = True
        state.queue_position   -= 1

        vc.stop()
        await asyncio.sleep(0.3)
        await self._play_next(interaction.guild, force=True, advance=False)
        state.manual_queue_seek = False

        await interaction.followup.send(
            embed=success_embed("Going Back", f"{EMOJI['play']} Playing previous track..."),
            ephemeral=True
        )
    
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
        app_commands.Choice(name="🐌 0.5× Slow",   value=50),
        app_commands.Choice(name="🐢 0.75×",        value=75),
        app_commands.Choice(name="⏱️ 1× Normal",    value=100),
        app_commands.Choice(name="⚡ 1.25×",        value=125),
        app_commands.Choice(name="⚡ 1.5× Fast",    value=150),
        app_commands.Choice(name="🚀 2× Very Fast", value=200),
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
    
    @app_commands.command(name="loop", description="Set loop mode for current track")
    @app_commands.choices(mode=[
        app_commands.Choice(name="🔄 Infinite loop", value=0),
        app_commands.Choice(name="1× then stop",     value=1),
        app_commands.Choice(name="2×",               value=2),
        app_commands.Choice(name="3×",               value=3),
        app_commands.Choice(name="5×",               value=5),
        app_commands.Choice(name="10×",              value=10),
        app_commands.Choice(name="❌ Disable loop",   value=-1),
    ])
    @check_permissions()
    @safe_defer
    async def loop(self, interaction: discord.Interaction, mode: app_commands.Choice[int] = None):
        state = await self._get_state(interaction)

        if not state.current_track:
            await interaction.followup.send(embed=warning_embed("Nothing is playing!"), ephemeral=True)
            return

        # No argument = toggle
        if mode is None:
            if state.loop_enabled:
                state.loop_enabled = False
                state.loop_count   = 0
                state.max_loops    = None
                await interaction.followup.send(embed=success_embed("Loop Disabled", "Loop off."), ephemeral=True)
            else:
                state.loop_enabled = True
                state.loop_count   = 0
                state.max_loops    = None
                await interaction.followup.send(embed=success_embed(
                    "Loop Enabled", f"{EMOJI['loop']} Looping current track (infinite)"), ephemeral=True)
            await self._update_np_embed(interaction.guild)
            return

        if mode.value == -1:
            state.loop_enabled = False
            state.loop_count   = 0
            state.max_loops    = None
            await interaction.followup.send(embed=success_embed("Loop Disabled", "Loop off."), ephemeral=True)
        else:
            state.loop_enabled = True
            state.loop_count   = 0
            state.max_loops    = None if mode.value == 0 else mode.value
            loop_text = "infinite" if mode.value == 0 else f"{mode.value}×"
            await interaction.followup.send(embed=success_embed(
                "Loop Set", f"{EMOJI['loop']} Looping current track ({loop_text})"), ephemeral=True)

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
    
    # ========== Queue Commands ==========
    
    @app_commands.command(name="queue", description="Show and navigate the queue")
    @check_permissions()
    @safe_defer
    async def queue(self, interaction: discord.Interaction):
        state = await self._get_state(interaction)

        if not state.queue:
            await interaction.followup.send(embed=warning_embed("Queue is empty!"), ephemeral=True)
            return

        # Start on the page containing the current track
        cur_page = max(0, state.queue_position // QueueView.TRACKS_PER_PAGE)
        view  = QueueView(state, self, interaction.guild, page=cur_page)
        embed = view.build_embed()
        await interaction.followup.send(embed=embed, view=view)
    
    @app_commands.command(name="seekqueue", description="Jump to queue position")
    @app_commands.describe(position="Queue position (1 = first)")
    @check_permissions()
    @safe_defer
    async def seekqueue(self, interaction: discord.Interaction, position: int):
        if not interaction.user.voice:
            await interaction.followup.send(embed=warning_embed("Join a voice channel first!"), ephemeral=True)
            return

        state = await self._get_state(interaction)

        if not state.queue:
            await interaction.followup.send(embed=warning_embed("Queue is empty!"), ephemeral=True)
            return

        if not 1 <= position <= len(state.queue):
            await interaction.followup.send(embed=warning_embed(
                f"Position must be 1–{len(state.queue)}"), ephemeral=True)
            return

        state.manual_queue_seek = True
        state.queue_position = position - 1

        vc = interaction.guild.voice_client
        if not vc:
            vc = await self.voice.connect(interaction.user.voice.channel)
            if not vc:
                await interaction.followup.send(embed=error_embed("Failed to connect to voice channel."), ephemeral=True)
                state.manual_queue_seek = False
                return
        elif vc.is_playing():
            vc.stop()

        await asyncio.sleep(0.5)
        await self._play_next(interaction.guild, force=True, advance=False)
        state.manual_queue_seek = False

        track = state.current_track
        name  = track.get_display_name() if track else f"#{position}"
        await interaction.followup.send(embed=success_embed(
            f"Jumped to #{position}",
            f"{EMOJI['music']} **{name}**"
        ), ephemeral=True)
    
    @app_commands.command(name="playing", description="Show current track")
    @check_permissions()
    @safe_defer
    async def playing(self, interaction: discord.Interaction):
        state = await self._get_state(interaction)
        track = state.current_track

        if not track:
            await interaction.followup.send(embed=warning_embed("Nothing is playing!"), ephemeral=True)
            return

        embed = self._build_now_playing_embed(interaction.guild, state)
        view  = NowPlayingView(self, interaction.guild)

        # Promote this as the new NP message
        msg = await interaction.followup.send(embed=embed, view=view)
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
            "Queue Cleared",
            f"Removed {count} track(s) from queue"
        ))
    
    @app_commands.command(name="remove", description="Remove track from queue")
    @check_permissions()
    @safe_defer
    async def remove(self, interaction: discord.Interaction, position: int):
        state = await self._get_state(interaction)
        
        if not state.queue:
            await interaction.followup.send(embed=warning_embed("Queue is empty!"))
            return
        
        if not 1 <= position <= len(state.queue):
            await interaction.followup.send(embed=warning_embed(
                f"Position must be 1-{len(state.queue)}"
            ))
            return
        
        if position - 1 == state.queue_position:
            await interaction.followup.send(embed=warning_embed(
                "Can't remove currently playing track! Use /skip instead."
            ))
            return
        
        track = state.queue.pop(position - 1)
        track.cleanup()
        
        # Adjust queue position if needed
        if position - 1 < state.queue_position:
            state.queue_position -= 1
        
        await interaction.followup.send(embed=success_embed(
            "Track Removed",
            f"Removed: **{track.get_display_name()}**"
        ))
    
    @app_commands.command(name="disconnect", description="Disconnect from voice")
    @check_permissions()
    @safe_defer
    async def disconnect(self, interaction: discord.Interaction):
        state = await self._get_state(interaction)
        vc = interaction.guild.voice_client
        
        if not vc:
            await interaction.followup.send(embed=warning_embed("Not connected to voice!"))
            return
        
        if vc.is_playing():
            state.is_stopped = True
            vc.stop()
        
        await asyncio.sleep(0.5)
        
        # Cleanup
        for track in state.queue:
            if track.downloaded_path:
                self.tracks.mark_inactive(track.downloaded_path)
            if track.converted_path:
                self.tracks.mark_inactive(track.converted_path)
            if not track.is_permanent:
                track.cleanup()
        
        queue_len = len(state.queue)
        state.reset()
        
        await vc.disconnect()
        
        await interaction.followup.send(embed=success_embed(
            "Disconnected",
            f"Cleared {queue_len} track(s) from queue"
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
    
    @app_commands.command(name="save", description="Save a CDN link or URL to library")
    @app_commands.describe(name="Name for the sound", url="CDN link or direct audio URL")
    @check_permissions()
    @safe_defer
    async def save(self, interaction: discord.Interaction, name: str, url: str):
        # Validate URL format
        if not url.startswith('http://') and not url.startswith('https://'):
            await interaction.followup.send(embed=error_embed("Invalid URL! Must start with http:// or https://"))
            return
        
        # Check if name exists
        if self.db.get_track(interaction.guild_id, name):
            await interaction.followup.send(embed=warning_embed(f"'{name}' already exists in library!"))
            return
        
        # Extract filename and validate extension
        ALLOWED_EXTS = {'.mp3', '.wav', '.ogg', '.flac', '.m4a', '.aac', '.mp4', '.webm'}
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
            logging.error(f"Failed to get file size for {url}: {e}")
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
            
            logging.info(f"Saved CDN link to library: {name} ({url})")
            
        except asyncio.TimeoutError:
            if os.path.exists(file_path):
                os.remove(file_path)
            await interaction.followup.send(embed=error_embed("Download timed out! Try a smaller file."))
        except Exception as e:
            if os.path.exists(file_path):
                os.remove(file_path)
            logging.error(f"Failed to save CDN link: {e}")
            await interaction.followup.send(embed=error_embed(f"Failed to save: {str(e)[:100]}"))
    
    @app_commands.command(name="library", description="Browse and queue library sounds")
    @check_permissions()
    @safe_defer
    async def library(self, interaction: discord.Interaction):
        tracks = self.db.list_tracks(interaction.guild_id)
        
        if not tracks:
            await interaction.followup.send(embed=warning_embed(
                "Library is empty! Use /upload to add sounds."
            ))
            return
        
        view = LibraryView(tracks, interaction.guild_id, self)
        embed = view._build_embed()
        await interaction.followup.send(embed=embed, view=view)
    
    @app_commands.command(name="remove_sound", description="Remove from library")
    @admin_only()
    @safe_defer
    async def remove_sound(self, interaction: discord.Interaction, name: str):
        file_path = self.db.remove_track(interaction.guild_id, name)
        
        if not file_path:
            await interaction.followup.send(embed=error_embed(f"'{name}' not found in library"))
            return
        
        if os.path.exists(file_path):
            os.remove(file_path)
        
        await interaction.followup.send(embed=success_embed(
            "Removed from Library",
            f"**{name}** has been removed"
        ))
    
    # ========== Playlist Commands ==========

    playlist_group = app_commands.Group(name="playlist", description="Manage playlists from the library")

    @playlist_group.command(name="list", description="Browse and manage playlists")
    @check_permissions()
    @safe_defer
    async def playlist_list(self, interaction: discord.Interaction):
        view  = PlaylistManagerView(self, interaction.guild, interaction.user)
        embed = view.build_embed()
        await interaction.followup.send(embed=embed, view=view)

    @playlist_group.command(name="view", description="View a specific playlist's tracks")
    @app_commands.describe(name="Playlist name")
    @check_permissions()
    @safe_defer
    async def playlist_view(self, interaction: discord.Interaction, name: str):
        playlist = self.db.get_playlist(interaction.guild_id, name)
        if not playlist:
            await interaction.followup.send(embed=error_embed(f"Playlist **{name}** not found."), ephemeral=True)
            return
        tracks = self.db.get_playlist_tracks(interaction.guild_id, name)
        view   = PlaylistDetailView(name, playlist, tracks, self, interaction.guild, interaction.user)
        await interaction.followup.send(embed=view.build_embed(), view=view)

    @playlist_group.command(name="play", description="Queue all tracks from a playlist")
    @app_commands.describe(name="Playlist name")
    @check_permissions()
    @safe_defer
    async def playlist_play(self, interaction: discord.Interaction, name: str):
        if not interaction.user.voice:
            await interaction.followup.send(embed=warning_embed("Join a voice channel first!"), ephemeral=True)
            return

        added = await self._queue_playlist_tracks(
            name, interaction.guild, interaction.user, interaction.channel_id)

        if not added:
            await interaction.followup.send(
                embed=error_embed(f"Playlist **{name}** is empty or doesn't exist."), ephemeral=True)
            return

        if not interaction.guild.voice_client:
            vc = await self.voice.connect(interaction.user.voice.channel)
            if not vc:
                await interaction.followup.send(embed=error_embed("Failed to connect to voice channel."), ephemeral=True)
                return

        state = await self._get_state(interaction)
        if not interaction.guild.voice_client.is_playing():
            if state.queue_position < 0:
                state.queue_position = 0
            await self._play_next(interaction.guild, force=True, advance=False)

        await interaction.followup.send(embed=success_embed(
            "Playlist Queued",
            f"{EMOJI['queue']} **{name}** — {added} track(s) added to queue."
        ), ephemeral=True)

    @playlist_group.command(name="create", description="Create a new playlist")
    @app_commands.describe(name="Playlist name", description="Optional description")
    @admin_only()
    @safe_defer
    async def playlist_create(self, interaction: discord.Interaction, name: str, description: str = ''):
        if len(name) > 50:
            await interaction.followup.send(embed=warning_embed("Playlist name must be 50 characters or fewer."), ephemeral=True)
            return
        if self.db.create_playlist(interaction.guild_id, name, interaction.user.display_name, description):
            await interaction.followup.send(embed=success_embed(
                "Playlist Created",
                f"{EMOJI['queue']} **{name}** created.\nUse `/playlist list` to manage it."
            ), ephemeral=True)
        else:
            await interaction.followup.send(embed=warning_embed(f"Playlist **{name}** already exists!"), ephemeral=True)

    @playlist_group.command(name="delete", description="Delete a playlist")
    @app_commands.describe(name="Playlist name")
    @admin_only()
    @safe_defer
    async def playlist_delete(self, interaction: discord.Interaction, name: str):
        if self.db.delete_playlist(interaction.guild_id, name):
            await interaction.followup.send(embed=success_embed(
                "Playlist Deleted", f"**{name}** has been deleted."), ephemeral=True)
        else:
            await interaction.followup.send(embed=error_embed(f"Playlist **{name}** not found."), ephemeral=True)

    @playlist_group.command(name="add", description="Add a track to a playlist")
    @app_commands.describe(name="Playlist name", track="Track name from library")
    @admin_only()
    @safe_defer
    async def playlist_add(self, interaction: discord.Interaction, name: str, track: str = None):
        playlist = self.db.get_playlist(interaction.guild_id, name)
        if not playlist:
            await interaction.followup.send(embed=error_embed(f"Playlist **{name}** not found."), ephemeral=True)
            return

        # Direct add if track specified
        if track:
            err = self.db.add_to_playlist(interaction.guild_id, name, track, interaction.user.display_name)
            if err:
                await interaction.followup.send(embed=error_embed(err), ephemeral=True)
            else:
                await interaction.followup.send(embed=success_embed(
                    "Track Added", f"**{track}** added to **{name}**."), ephemeral=True)
            return

        # Otherwise open interactive add view
        library  = self.db.list_tracks(interaction.guild_id)
        existing = {t['track_name'] for t in self.db.get_playlist_tracks(interaction.guild_id, name)}
        available = [t for t in library if t['track_name'] not in existing]

        if not library:
            await interaction.followup.send(embed=warning_embed("Library is empty! Upload sounds first."), ephemeral=True)
            return
        if not available:
            await interaction.followup.send(embed=info_embed(
                "All Tracks Added", f"Every library track is already in **{name}**."), ephemeral=True)
            return

        view = PlaylistAddView(available, name, self, interaction.guild, interaction.user)
        await interaction.followup.send(embed=view.build_embed(), view=view)

    @playlist_group.command(name="remove", description="Remove a track from a playlist")
    @app_commands.describe(name="Playlist name", track="Track name to remove")
    @admin_only()
    @safe_defer
    async def playlist_remove(self, interaction: discord.Interaction, name: str, track: str):
        if self.db.remove_from_playlist(interaction.guild_id, name, track):
            await interaction.followup.send(embed=success_embed(
                "Track Removed", f"**{track}** removed from **{name}**."), ephemeral=True)
        else:
            await interaction.followup.send(embed=error_embed(
                f"**{track}** not found in playlist **{name}**."), ephemeral=True)

    # ---- Autocomplete ----

    async def _playlist_autocomplete(self, interaction: discord.Interaction, current: str):
        playlists = self.db.list_playlists(interaction.guild_id)
        return [
            app_commands.Choice(name=p['playlist_name'], value=p['playlist_name'])
            for p in playlists if current.lower() in p['playlist_name'].lower()
        ][:25]

    async def _playlist_track_autocomplete(self, interaction: discord.Interaction, current: str):
        playlist_name = getattr(interaction.namespace, 'name', None)
        if not playlist_name:
            return []
        tracks = self.db.get_playlist_tracks(interaction.guild_id, playlist_name)
        return [
            app_commands.Choice(name=t['track_name'], value=t['track_name'])
            for t in tracks if current.lower() in t['track_name'].lower()
        ][:25]

    async def _library_track_autocomplete(self, interaction: discord.Interaction, current: str):
        tracks = self.db.list_tracks(interaction.guild_id)
        return [
            app_commands.Choice(name=t['track_name'], value=t['track_name'])
            for t in tracks if current.lower() in t['track_name'].lower()
        ][:25]

    @play.autocomplete('name')
    async def _play_name(self, interaction: discord.Interaction, current: str):
        return await self._library_track_autocomplete(interaction, current)

    @playlist_view.autocomplete('name')
    async def _pv_name(self, interaction, current):
        return await self._playlist_autocomplete(interaction, current)

    @playlist_play.autocomplete('name')
    async def _pp_name(self, interaction, current):
        return await self._playlist_autocomplete(interaction, current)

    @playlist_delete.autocomplete('name')
    async def _pd_name(self, interaction, current):
        return await self._playlist_autocomplete(interaction, current)

    @playlist_add.autocomplete('name')
    async def _pa_name(self, interaction, current):
        return await self._playlist_autocomplete(interaction, current)

    @playlist_add.autocomplete('track')
    async def _pa_track(self, interaction, current):
        return await self._library_track_autocomplete(interaction, current)

    @playlist_remove.autocomplete('name')
    async def _pr_name(self, interaction, current):
        return await self._playlist_autocomplete(interaction, current)

    @playlist_remove.autocomplete('track')
    async def _pr_track(self, interaction, current):
        return await self._playlist_track_autocomplete(interaction, current)
    
    # ========== Radio Commands ==========
    
    
    # ========== Help Command ==========
    
    @app_commands.command(name="help", description="Show all commands")
    async def help(self, interaction: discord.Interaction):
        embed = create_embed(color=Colors.PRIMARY)
        embed.title = f"{EMOJI['music']} SporkMP3 Commands"
        embed.description = "A feature-rich music bot for Discord"
        
        # Playback commands
        playback = (
            "`/play [name]` › Play from queue or library\n"
            "`/pause` › Pause playback\n"
            "`/resume` › Resume playback\n"
            "`/stop` › Stop (queue intact)\n"
            "`/skip` › Skip to next track\n"
            "`/previous` › Go back to previous track\n"
            "`/volume` › Set volume (0-120)\n"
            "`/speed` › Adjust speed (menu)\n"
            "`/forward` `/backward` › Seek by seconds\n"
            "`/seek` › Jump to specific time\n"
            "`/loop [mode]` › Loop current track (menu)"
        )
        embed.add_field(name=f"{EMOJI['play']} Playback", value=playback, inline=False)
        
        # Queue commands
        queue = (
            "`/queue` › View queue\n"
            "`/seekqueue` › Jump to position\n"
            "`/playing` › Current track info\n"
            "`/clear` › Clear entire queue\n"
            "`/remove` › Remove specific track"
        )
        embed.add_field(name=f"{EMOJI['queue']} Queue", value=queue, inline=False)
        
        # Library commands
        library = (
            "`/upload <name>` › Save file or URL to library\n"
            "`/save <name> <url>` › Save CDN link to library\n"
            "`/library` › Browse & queue library sounds\n"
            "`/remove_sound` › Delete from library"
        )
        embed.add_field(name=f"{EMOJI['cd']} Library", value=library, inline=False)

        # Playlist commands
        playlists = (
            "`/playlist create` › Create a playlist *(admin)*\n"
            "`/playlist delete` › Delete a playlist *(admin)*\n"
            "`/playlist add` › Add library tracks *(admin)*\n"
            "`/playlist remove` › Remove a track *(admin)*\n"
            "`/playlist list` › View all playlists\n"
            "`/playlist view` › Browse & queue from playlist\n"
            "`/playlist play` › Queue entire playlist"
        )
        embed.add_field(name=f"{EMOJI['queue']} Playlists", value=playlists, inline=False)
        
        # Admin commands
        admin = (
            "`/settings` › Interactive settings panel\n"
            "`/autoplay` › Toggle autoplay\n"
            "`/autodisconnect` › Toggle auto-disconnect\n"
            "`/blacklist` › Manage user blacklist\n"
            "`/role_config` › Configure role permissions\n"
            "`/health` › Bot health stats"
        )
        embed.add_field(name=f"{EMOJI['settings']} Admin", value=admin, inline=False)
        
        # Upload tips
        embed.add_field(
            name=f"{EMOJI['info']} Quick Features",
            value=(
                "**Queue:** Mention the bot with files or CDN links to add to queue\n"
                "**Library:** Use `/upload` with file or URL to save permanently"
            ),
            inline=False
        )
        
        await interaction.response.send_message(embed=embed)


async def setup(bot):
    await bot.add_cog(Music(bot))