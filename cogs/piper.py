"""MIT License

Copyright (c) 2023 - present Vocard Development

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

import discord
import voicelink
import function as func

from discord import app_commands
from discord.ext import commands
from function import (
    get_aliases,
    cooldown_check
)

from voicelink import MongoDBHandler, LangHandler
from voicelink.announcer import PiperClient
from voicelink.utils import dispatch_message, send_localized_message

# Keys a guild may override, in the order they are shown.
_SETTING_KEYS: tuple = ("voice",) + PiperClient.OPTION_KEYS


class Piper(commands.Cog, name="piper"):
    def __init__(self, bot) -> None:
        self.bot: commands.Bot = bot
        self.description = "Tune the Piper text-to-speech voice used for announcements."

    @property
    def _announcer(self):
        return getattr(self.bot, "announcer", None)

    @staticmethod
    def _collect(
        voice: str,
        length_scale: float,
        noise_scale: float,
        length_w_scale: float,
        speaker_id: int
    ) -> dict:
        """Builds a settings dict from whichever command options were given."""
        supplied = {
            "voice": voice,
            "length_scale": length_scale,
            "noise_scale": noise_scale,
            "length_w_scale": length_w_scale,
            "speaker_id": speaker_id
        }
        return {key: value for key, value in supplied.items() if value is not None}

    @staticmethod
    def _describe(settings: dict) -> str:
        """Renders a settings dict for the status embed."""
        lines = [f"{key}: {settings[key]}" for key in _SETTING_KEYS if key in settings]
        return "\n".join(lines) if lines else "-"

    def _global_settings(self) -> dict:
        """The bot-wide defaults, read from the live client rather than the file.

        The client is what actually synthesises, so reading it means the embed
        can never disagree with what the next announcement will sound like.
        """
        piper = self._announcer.piper
        settings = dict(piper.options)
        if piper.voice:
            settings["voice"] = piper.voice
        return settings

    @commands.hybrid_group(
        name="piper",
        aliases=get_aliases("piper"),
        invoke_without_command=True
    )
    async def piper(self, ctx: commands.Context):
        "Tune the announcement voice."
        await self.show(ctx)

    @piper.command(name="show", aliases=get_aliases("show"))
    @commands.dynamic_cooldown(cooldown_check, commands.BucketType.guild)
    async def show(self, ctx: commands.Context):
        "Show the announcement voice settings in effect here."
        if not self._announcer:
            return await send_localized_message(ctx, "settings.actions.announceNotConfigured", ephemeral=True)

        settings = await MongoDBHandler.get_settings(ctx.guild.id)
        guild_cfg = (settings.get("tts_announce") or {}).get("piper") or {}

        texts = await LangHandler.get_lang(ctx.guild.id, "settings.piper.title", "settings.piper.value")
        embed = discord.Embed(title=texts[0], color=voicelink.Config().embed_color)
        embed.description = texts[1].format(
            self._describe(self._global_settings()),
            self._describe(guild_cfg),
            self._describe({**self._global_settings(), **guild_cfg})
        )
        await dispatch_message(ctx, embed)

    @piper.command(name="set", aliases=get_aliases("set"))
    @app_commands.describe(
        voice="Piper voice model, e.g. en_US-lessac-medium. It must exist on the Piper server.",
        length_scale="Speaking speed. Higher is slower. 1.0 is the model default.",
        noise_scale="Expressiveness. Lower is flatter and more robotic.",
        length_w_scale="Cadence looseness. Lower is more clipped.",
        speaker_id="Speaker index, for multi-speaker models only."
    )
    @commands.has_permissions(manage_guild=True)
    @commands.dynamic_cooldown(cooldown_check, commands.BucketType.guild)
    async def set(
        self,
        ctx: commands.Context,
        voice: str = None,
        length_scale: commands.Range[float, 0.1, 3.0] = None,
        noise_scale: commands.Range[float, 0.0, 1.0] = None,
        length_w_scale: commands.Range[float, 0.0, 3.0] = None,
        speaker_id: commands.Range[int, 0, 999] = None
    ):
        "Override the announcement voice for this server."
        if not self._announcer:
            return await send_localized_message(ctx, "settings.actions.announceNotConfigured", ephemeral=True)

        updates = self._collect(voice, length_scale, noise_scale, length_w_scale, speaker_id)
        if not updates:
            return await self.show(ctx)

        await MongoDBHandler.update_settings(
            ctx.guild.id,
            {"$set": {f"tts_announce.piper.{key}": value for key, value in updates.items()}}
        )

        # Guild overrides are read straight off the player's settings at
        # synthesis time, so a live player has to be told about them.
        player: voicelink.Player = ctx.guild.voice_client
        if player:
            fresh = await MongoDBHandler.get_settings(ctx.guild.id)
            player.settings["tts_announce"] = fresh.get("tts_announce", {})

        await send_localized_message(ctx, "settings.actions.piperUpdated", ", ".join(updates))

    @piper.command(name="reset", aliases=get_aliases("reset"))
    @commands.has_permissions(manage_guild=True)
    @commands.dynamic_cooldown(cooldown_check, commands.BucketType.guild)
    async def reset(self, ctx: commands.Context):
        "Drop this server's overrides and fall back to the bot defaults."
        await MongoDBHandler.update_settings(ctx.guild.id, {"$unset": {"tts_announce.piper": ""}})

        player: voicelink.Player = ctx.guild.voice_client
        if player:
            player.settings.get("tts_announce", {}).pop("piper", None)

        await send_localized_message(ctx, "settings.actions.piperReset")

    @piper.command(name="default", aliases=get_aliases("default"))
    @app_commands.describe(
        voice="Piper voice model, e.g. en_US-lessac-medium. It must exist on the Piper server.",
        length_scale="Speaking speed. Higher is slower. 1.0 is the model default.",
        noise_scale="Expressiveness. Lower is flatter and more robotic.",
        length_w_scale="Cadence looseness. Lower is more clipped.",
        speaker_id="Speaker index, for multi-speaker models only."
    )
    @commands.dynamic_cooldown(cooldown_check, commands.BucketType.guild)
    async def default(
        self,
        ctx: commands.Context,
        voice: str = None,
        length_scale: commands.Range[float, 0.1, 3.0] = None,
        noise_scale: commands.Range[float, 0.0, 1.0] = None,
        length_w_scale: commands.Range[float, 0.0, 3.0] = None,
        speaker_id: commands.Range[int, 0, 999] = None
    ):
        "Change the bot-wide defaults. Bot owners only."
        if ctx.author.id not in voicelink.Config().bot_access_user:
            return await dispatch_message(ctx, "You are not able to use this command!", ephemeral=True)

        if not self._announcer:
            return await send_localized_message(ctx, "settings.actions.announceNotConfigured", ephemeral=True)

        updates = self._collect(voice, length_scale, noise_scale, length_w_scale, speaker_id)
        if not updates:
            return await self.show(ctx)

        options = PiperClient.filter_options(updates)
        self._announcer.piper.configure(voice=updates.get("voice"), options=options)

        # Mirror the change into the loaded config and back out to disk, so a
        # restart does not quietly undo it. announce_settings is the same dict
        # Config handed out at startup, so mutating it is what the rest of the
        # bot already reads.
        announce_settings = voicelink.Config().announce_settings
        piper_cfg = announce_settings.setdefault("piper", {})
        if "voice" in updates:
            piper_cfg["voice"] = updates["voice"]
        if options:
            piper_cfg.setdefault("options", {}).update(options)

        func.update_json("settings.json", {"announce_settings": announce_settings})
        await send_localized_message(ctx, "settings.actions.piperUpdated", ", ".join(updates))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Piper(bot))
