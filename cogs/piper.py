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
from voicelink.announcer import TTSClient
from voicelink.utils import dispatch_message, send_localized_message

# Applied by the bot to the finished clip rather than sent to the engine, so it
# is stored and displayed alongside the engine's knobs but never forwarded.
_LOUDNESS: str = "loudness"

_LOUDNESS_HELP: str = "Boost quiet clips up to this % of full volume. 0 keeps the engine's own level."


class Piper(commands.Cog, name="piper"):
    def __init__(self, bot) -> None:
        self.bot: commands.Bot = bot
        self.description = "Tune the text-to-speech voice used for announcements."

    @property
    def _announcer(self):
        return getattr(self.bot, "announcer", None)

    @property
    def _client(self) -> TTSClient:
        """The engine actually synthesising announcements right now."""
        return self._announcer.tts

    @staticmethod
    def _keys(client: TTSClient) -> tuple:
        """Keys a guild may override on this engine, in the order they are shown.

        `loudness` is not one of the engine's own knobs - the bot applies it to
        the finished clip - so it is offered whichever engine is running.
        """
        return ("voice",) + client.OPTION_KEYS + (_LOUDNESS,)

    @staticmethod
    def _collect(
        voice: str,
        loudness: int,
        length_scale: float,
        noise_scale: float,
        length_w_scale: float,
        speaker_id: int
    ) -> dict:
        """Builds a settings dict from whichever command options were given."""
        supplied = {
            "voice": voice,
            _LOUDNESS: loudness,
            "length_scale": length_scale,
            "noise_scale": noise_scale,
            "length_w_scale": length_w_scale,
            "speaker_id": speaker_id
        }
        return {key: value for key, value in supplied.items() if value is not None}

    @classmethod
    def _describe(cls, client: TTSClient, settings: dict) -> str:
        """Renders a settings dict for the status embed.

        Only what the active engine can honour is listed. A guild that tuned
        Piper and then had the owner move to PocketTTS still has the old knobs
        stored, but they change nothing, so showing them would be a lie.
        """
        lines = [f"{key}: {settings[key]}" for key in cls._keys(client) if key in settings]
        return "\n".join(lines) if lines else "-"

    def _unsupported(self, updates: dict) -> list:
        """The requested keys the active engine has no knob for.

        Rejecting the whole command rather than quietly applying the rest keeps
        the stored settings and what was asked for in step: PocketTTS takes no
        numeric tuning at all, so `/piper set voice:alba noise_scale:0.3` would
        otherwise look half-honoured.
        """
        keys = self._keys(self._client)
        return [key for key in updates if key not in keys]

    def _global_settings(self) -> dict:
        """The bot-wide defaults, read from the live client rather than the file.

        The client is what actually synthesises, so reading it means the embed
        can never disagree with what the next announcement will sound like.
        """
        client = self._client
        settings = dict(client.options)
        if client.voice:
            settings["voice"] = client.voice
        settings[_LOUDNESS] = self._announcer.loudness
        return settings

    async def _voice_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str
    ) -> list[app_commands.Choice[str]]:
        """Suggests the engine's built-in voices, where it has a fixed set.

        Piper serves whatever was downloaded into its data directory, which the
        bot cannot enumerate, so there it suggests nothing and the field stays
        free text.
        """
        client = getattr(self._announcer, "tts", None)
        voices = getattr(client, "VOICES", ())
        current = (current or "").lower()
        return [
            app_commands.Choice(name=voice, value=voice)
            for voice in voices if current in voice
        ][:25]

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

        client = self._client
        defaults = self._global_settings()

        texts = await LangHandler.get_lang(ctx.guild.id, "settings.piper.title", "settings.piper.value")
        embed = discord.Embed(title=texts[0], color=voicelink.Config().embed_color)
        embed.description = texts[1].format(
            f"engine: {client.NAME}\n{self._describe(client, defaults)}",
            self._describe(client, guild_cfg),
            self._describe(client, {**defaults, **guild_cfg})
        )
        await dispatch_message(ctx, embed)

    @piper.command(name="set", aliases=get_aliases("set"))
    @app_commands.describe(
        voice="Voice name, e.g. en_US-lessac-medium (Piper) or alba (PocketTTS).",
        loudness=_LOUDNESS_HELP,
        length_scale="Piper only. Speaking speed. Higher is slower. 1.0 is the model default.",
        noise_scale="Piper only. Expressiveness. Lower is flatter and more robotic.",
        length_w_scale="Piper only. Cadence looseness. Lower is more clipped.",
        speaker_id="Piper only. Speaker index, for multi-speaker models only."
    )
    @app_commands.autocomplete(voice=_voice_autocomplete)
    @commands.has_permissions(manage_guild=True)
    @commands.dynamic_cooldown(cooldown_check, commands.BucketType.guild)
    async def set(
        self,
        ctx: commands.Context,
        voice: str = None,
        loudness: commands.Range[int, 0, 100] = None,
        length_scale: commands.Range[float, 0.1, 3.0] = None,
        noise_scale: commands.Range[float, 0.0, 1.0] = None,
        length_w_scale: commands.Range[float, 0.0, 3.0] = None,
        speaker_id: commands.Range[int, 0, 999] = None
    ):
        "Override the announcement voice for this server."
        if not self._announcer:
            return await send_localized_message(ctx, "settings.actions.announceNotConfigured", ephemeral=True)

        updates = self._collect(voice, loudness, length_scale, noise_scale, length_w_scale, speaker_id)
        if not updates:
            return await self.show(ctx)

        if unsupported := self._unsupported(updates):
            return await send_localized_message(
                ctx, "settings.actions.piperUnsupported", ", ".join(unsupported), self._client.NAME, ephemeral=True
            )

        # A custom voice is a URL the engine fetches reference audio from -
        # unlike a built-in voice name, it can be unreachable or something the
        # engine refuses to use. Catching that now, instead of letting every
        # future announcement fail silently in the background, costs one real
        # synthesis call up front.
        client = self._client
        url_schemes = getattr(client, "VOICE_URL_SCHEMES", ())
        if voice and voice.startswith(url_schemes):
            await ctx.defer()
            if not await client.synthesize("Testing this voice.", voice=voice):
                return await send_localized_message(
                    ctx, "settings.actions.piperVoiceUnreachable", voice, ephemeral=True
                )

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
        voice="Voice name, e.g. en_US-lessac-medium (Piper) or alba (PocketTTS).",
        loudness=_LOUDNESS_HELP,
        length_scale="Piper only. Speaking speed. Higher is slower. 1.0 is the model default.",
        noise_scale="Piper only. Expressiveness. Lower is flatter and more robotic.",
        length_w_scale="Piper only. Cadence looseness. Lower is more clipped.",
        speaker_id="Piper only. Speaker index, for multi-speaker models only."
    )
    @app_commands.autocomplete(voice=_voice_autocomplete)
    @commands.dynamic_cooldown(cooldown_check, commands.BucketType.guild)
    async def default(
        self,
        ctx: commands.Context,
        voice: str = None,
        loudness: commands.Range[int, 0, 100] = None,
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

        updates = self._collect(voice, loudness, length_scale, noise_scale, length_w_scale, speaker_id)
        if not updates:
            return await self.show(ctx)

        client = self._client
        if unsupported := self._unsupported(updates):
            return await send_localized_message(
                ctx, "settings.actions.piperUnsupported", ", ".join(unsupported), client.NAME, ephemeral=True
            )

        # See the matching check in set() - a broken bot-wide default is worse
        # than a broken guild override, since every guild without its own
        # voice override inherits it.
        url_schemes = getattr(client, "VOICE_URL_SCHEMES", ())
        if voice and voice.startswith(url_schemes):
            await ctx.defer()
            if not await client.synthesize("Testing this voice.", voice=voice):
                return await send_localized_message(
                    ctx, "settings.actions.piperVoiceUnreachable", voice, ephemeral=True
                )

        options = client.filter_options(updates)
        client.configure(voice=updates.get("voice"), options=options)
        if _LOUDNESS in updates:
            self._announcer.loudness = updates[_LOUDNESS]

        # Mirror the change into the loaded config and back out to disk, so a
        # restart does not quietly undo it. announce_settings is the same dict
        # Config handed out at startup, so mutating it is what the rest of the
        # bot already reads. The engine keeps its own block, so tuning one does
        # not disturb what the other is configured with; loudness is the bot's
        # own work and sits at the top level.
        announce_settings = voicelink.Config().announce_settings
        engine_cfg = announce_settings.setdefault(client.NAME, {})
        if "voice" in updates:
            engine_cfg["voice"] = updates["voice"]
        if options:
            engine_cfg.setdefault("options", {}).update(options)
        if _LOUDNESS in updates:
            announce_settings[_LOUDNESS] = self._announcer.loudness

        func.update_json("settings.json", {"announce_settings": announce_settings})
        await send_localized_message(ctx, "settings.actions.piperUpdated", ", ".join(updates))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Piper(bot))
