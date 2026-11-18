
import io
import logging
import re
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from google.genai import types

import config
import db
import moderation
from ai import ask_capri, generate_image


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("capri")

intents = discord.Intents.default()
intents.message_content = True

NO_PINGS = discord.AllowedMentions.none()

_last_message_time: dict[int, float] = {}
_busy_users: set[int] = set()


class CapriBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        db.init_db()
        moderation.init_db()
        if config.DEV_GUILD_ID:
            guild = discord.Object(id=int(config.DEV_GUILD_ID))
            self.tree.clear_commands(guild=guild)
            await self.tree.sync(guild=guild)
        await self.tree.sync()

    async def on_ready(self):
        print(f"✅ {config.BOT_NAME} est connecté en tant que {self.user}")


bot = CapriBot()


def everywhere(func):
    func = app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)(func)
    func = app_commands.allowed_installs(guilds=True, users=True)(func)
    return func


def is_owner(user_id: int) -> bool:
    return bool(config.OWNER_ID) and user_id == config.OWNER_ID


def channel_allowed(guild_id: Optional[int], channel_ids: list) -> bool:
    if guild_id is None:
        return True
    allowed = db.get_allowed_channels(guild_id)
    if not allowed:
        return True
    return any(c in allowed for c in channel_ids if c)


def too_fast(user_id: int) -> bool:
    now = time.monotonic()
    if now - _last_message_time.get(user_id, 0) < config.COOLDOWN_SECONDS:
        return True
    _last_message_time[user_id] = now
    return False


def split_message(text: str, limit: int = 1900) -> list[str]:
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        chunks.append(text)
    return [c for c in chunks if c]


def attachment_kind(mime: str) -> Optional[str]:
    if mime in config.IMAGE_MIMES:
        return "image"
    if mime in config.VIDEO_MIMES:
        return "video"
    if mime in config.AUDIO_MIMES:
        return "audio"
    return None


async def collect_media(attachments):
    parts = []
    counts = {"image": 0, "video": 0, "audio": 0}
    skipped = 0
    total = 0
    limits = {
        "image": config.MAX_IMAGE_BYTES,
        "video": config.MAX_VIDEO_BYTES,
        "audio": config.MAX_AUDIO_BYTES,
    }

    for att in attachments:
        if len(parts) >= config.MAX_ATTACHMENTS:
            skipped += 1
            continue
        mime = (att.content_type or "").split(";")[0].strip().lower()
        kind = attachment_kind(mime)
        if kind is None:
            skipped += 1
            continue
        if att.size > limits[kind] or total + att.size > config.MAX_TOTAL_MEDIA_BYTES:
            skipped += 1
            continue
        try:
            data = await att.read()
        except discord.HTTPException:
            skipped += 1
            continue
        total += len(data)
        mime = config.MIME_FIX.get(mime, mime)
        parts.append((kind, types.Part.from_bytes(data=data, mime_type=mime)))
        counts[kind] += 1

    return parts, counts, skipped


def media_note(counts: dict) -> str:
    names = []
    if counts["image"]:
        names.append(f"{counts['image']} image(s)")
    if counts["video"]:
        names.append(f"{counts['video']} vidéo(s)")
    if counts["audio"]:
        names.append(f"{counts['audio']} vocal/audio")
    return f"[Pièces jointes envoyées : {', '.join(names)}]" if names else ""


async def make_image(user, prompt: str, input_parts):
    if not config.IMAGES_ENABLED:
        return None, "La création d'images est désactivée pour le moment 🎨"

    if not is_owner(user.id) and db.get_usage(user.id, "image") >= config.DAILY_IMAGE_LIMIT:
        return None, (
            f"Tu as atteint ta limite de {config.DAILY_IMAGE_LIMIT} images pour aujourd'hui "
            "chou 🎨 Reviens demain !"
        )

    result = await generate_image(prompt, input_parts)

    if result.status == "ok" and result.data:
        db.add_usage(user.id, "image")
        ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}.get(result.mime, "png")
        return discord.File(io.BytesIO(result.data), filename=f"capri.{ext}"), ""

    log.warning("Image non créée : %s", result.status)
    messages = {
        "billing": "Je ne peux pas encore créer d'images ici 🥺 (la fonction n'est pas activée chez moi).",
        "model": "Mon atelier d'images est en panne de matériel 🔧 Réessaie plus tard.",
        "blocked": "Je n'ai pas pu créer cette image (elle ne passe pas les règles de sécurité). Essaie de reformuler 🙈",
        "unavailable": "Mon atelier d'images est surchargé, réessaie dans un instant 🎨",
    }
    return None, messages.get(result.status, "Je n'ai pas réussi à créer cette image, désolée chou 🥺")


async def build_response(user, text: str, attachments):
    owner = is_owner(user.id)

    if not owner and db.get_usage(user.id, "msg") >= config.DAILY_MESSAGE_LIMIT:
        return ["Tu m'as déjà beaucoup parlé aujourd'hui chou, je dois me reposer 😴 Reviens demain !"], None

    notes = []
    media, counts, skipped = await collect_media(attachments)

    if media and not owner:
        if db.get_usage(user.id, "media") + len(media) > config.DAILY_MEDIA_LIMIT:
            media, counts = [], {"image": 0, "video": 0, "audio": 0}
            notes.append("(Tu as atteint ta limite de fichiers pour aujourd'hui, je n'ai pas pu les regarder 🙈)")

    if skipped:
        notes.append(
            f"(Je n'ai pas pu lire {skipped} fichier(s) : format non pris en charge ou trop lourd.)"
        )

    text = text.strip()[: config.MAX_INPUT_CHARS]
    if not text and media:
        text = "(La personne n'a envoyé que des fichiers, sans texte.)"
    if not text:
        text = "(message vide)"

    result = await ask_capri(
        user.id, user.display_name, text, [p for _, p in media], media_note(counts)
    )
    if result is None:
        return [config.ERROR_MESSAGE], None

    db.add_usage(user.id, "msg")
    if media:
        db.add_usage(user.id, "media", len(media))

    file = None
    if result.image_prompt.strip():
        input_parts = [p for kind, p in media if kind == "image"]
        file, image_note = await make_image(user, result.image_prompt, input_parts)
        if image_note:
            notes.append(image_note)

    answer = result.answer
    if notes:
        answer += "\n\n" + "\n".join(notes)
    return split_message(answer) or [config.ERROR_MESSAGE], file


async def send_to_interaction(interaction: discord.Interaction, chunks, file=None):
    for i, chunk in enumerate(chunks):
        kwargs = {"allowed_mentions": NO_PINGS}
        if file is not None and i == len(chunks) - 1:
            kwargs["file"] = file
        await interaction.followup.send(chunk, **kwargs)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    is_dm = message.guild is None

    if is_dm:
        text = message.content
    else:
        if bot.user not in message.mentions:
            return
        channel_ids = [message.channel.id, getattr(message.channel, "parent_id", None)]
        if not channel_allowed(message.guild.id, channel_ids):
            return
        text = re.sub(rf"<@!?{bot.user.id}>", "", message.content)

    text = text.strip()
    if not text and not message.attachments:
        await message.channel.send(
            "Oui chou ? Je t'écoute 💕",
            mention_author=False,
            allowed_mentions=NO_PINGS,
        )
        return

    user_id = message.author.id
    if user_id in _busy_users or too_fast(user_id):
        return

    _busy_users.add(user_id)
    try:
        if not is_dm and not message.attachments:
            if await moderation.handle_message(message, text, bot.user):
                return

        async with message.channel.typing():
            chunks, file = await build_response(message.author, text, message.attachments)

        for i, chunk in enumerate(chunks):
            kwargs = {"allowed_mentions": NO_PINGS}
            if file is not None and i == len(chunks) - 1:
                kwargs["file"] = file
            if i == 0:
                await message.reply(chunk, mention_author=False, **kwargs)
            else:
                await message.channel.send(chunk, **kwargs)
    except discord.HTTPException as e:
        log.warning("Erreur Discord : %s", e)
    finally:
        _busy_users.discard(user_id)


@bot.tree.command(name="ping", description="Vérifie que Capri est bien réveillé")
@everywhere
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("Coucou chou, je suis là ! 💕")


@bot.tree.command(name="capri", description="Discuter avec Capri")
@everywhere
@app_commands.describe(
    message="Ton message pour Capri",
    fichier="Une image, une vidéo ou un vocal (facultatif)",
)
async def capri(
    interaction: discord.Interaction,
    message: str,
    fichier: Optional[discord.Attachment] = None,
):
    channel_ids = [interaction.channel_id, getattr(interaction.channel, "parent_id", None)]
    if not channel_allowed(interaction.guild_id, channel_ids):
        await interaction.response.send_message(
            "Je ne peux pas parler dans ce salon, chou 🙈", ephemeral=True
        )
        return

    user = interaction.user
    if user.id in _busy_users or too_fast(user.id):
        await interaction.response.send_message(
            "Doucement chou, laisse-moi une seconde ⏳", ephemeral=True
        )
        return

    _busy_users.add(user.id)
    try:
        await interaction.response.defer(thinking=True)
        chunks, file = await build_response(user, message, [fichier] if fichier else [])
        await send_to_interaction(interaction, chunks, file)
    except discord.HTTPException as e:
        log.warning("Erreur Discord : %s", e)
    finally:
        _busy_users.discard(user.id)


@bot.tree.command(name="image", description="Demander à Capri de créer une image")
@everywhere
@app_commands.describe(
    description="Décris l'image que tu veux",
    image_de_depart="Une image à modifier (facultatif)",
)
async def image_command(
    interaction: discord.Interaction,
    description: str,
    image_de_depart: Optional[discord.Attachment] = None,
):
    channel_ids = [interaction.channel_id, getattr(interaction.channel, "parent_id", None)]
    if not channel_allowed(interaction.guild_id, channel_ids):
        await interaction.response.send_message(
            "Je ne peux pas parler dans ce salon, chou 🙈", ephemeral=True
        )
        return

    user = interaction.user
    if user.id in _busy_users or too_fast(user.id):
        await interaction.response.send_message(
            "Doucement chou, laisse-moi une seconde ⏳", ephemeral=True
        )
        return

    _busy_users.add(user.id)
    try:
        await interaction.response.defer(thinking=True)
        input_parts = []
        if image_de_depart:
            media, _, _ = await collect_media([image_de_depart])
            input_parts = [p for kind, p in media if kind == "image"]

        file, note = await make_image(user, description[: config.MAX_INPUT_CHARS], input_parts)
        text = "Voilà chou, j'espère que ça te plaît 🎨" if file else note
        await send_to_interaction(interaction, [text], file)
    except discord.HTTPException as e:
        log.warning("Erreur Discord : %s", e)
    finally:
        _busy_users.discard(user.id)


@bot.tree.command(name="reset", description="Repartir d'une conversation vierge (je garde tes infos)")
@everywhere
async def reset(interaction: discord.Interaction):
    db.clear_history(interaction.user.id)
    await interaction.response.send_message(
        "C'est fait chou, on repart sur une page blanche ✨\n"
        "(Je garde quand même ce que je sais sur toi. Utilise `/oublie` pour en supprimer.)",
        ephemeral=True,
    )


@bot.tree.command(name="memoire", description="Voir ce que Capri a retenu sur toi")
@everywhere
async def memoire(interaction: discord.Interaction):
    facts = db.get_facts(interaction.user.id)
    if not facts:
        await interaction.response.send_message(
            "Je ne sais encore rien sur toi chou, raconte-moi tout ! 💕", ephemeral=True
        )
        return

    lines = "\n".join(f"{i}. {fact}" for i, fact in enumerate(facts, start=1))
    embed = discord.Embed(
        title="🧠 Ce que je sais sur toi",
        description=lines[:4000],
        color=discord.Color.pink(),
    )
    embed.set_footer(text="Utilise /oublie pour supprimer une information.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="oublie", description="Faire oublier une information à Capri")
@everywhere
@app_commands.describe(information="L'information à oublier")
async def oublie(interaction: discord.Interaction, information: str):
    status, fact = db.delete_fact(interaction.user.id, information)

    if status == "deleted":
        msg = f"C'est oublié chou ✨ (« {fact} »)"
    elif status == "ambiguous":
        msg = (
            "Plusieurs infos correspondent 🤔 Choisis-en une dans la liste "
            "qui apparaît quand tu écris, ou sois plus précis."
        )
    else:
        msg = "Je n'ai rien trouvé qui corresponde. Regarde avec `/memoire` 🔍"

    await interaction.response.send_message(msg, ephemeral=True)


@oublie.autocomplete("information")
async def oublie_autocomplete(interaction: discord.Interaction, current: str):
    current = current.lower()
    facts = db.get_facts(interaction.user.id)
    return [
        app_commands.Choice(name=fact[:100], value=fact[:100])
        for fact in facts
        if current in fact.lower()
    ][:25]


@bot.tree.command(name="help", description="Afficher les commandes de Capri")
@everywhere
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title=f"💕 {config.BOT_NAME} — Aide",
        description="Je suis ta compagne de discussion préférée !",
        color=discord.Color.pink(),
    )
    embed.add_field(
        name="Me parler",
        value=(
            "• En **message privé** : écris-moi directement, sans commande\n"
            "• Sur un serveur : **mentionne-moi** (@Capri) ou utilise `/capri`\n"
            "• Tu peux m'envoyer des **images, vidéos et messages vocaux** 🎧"
        ),
        inline=False,
    )
    embed.add_field(
        name="Commandes",
        value=(
            "`/capri [message]` — discuter avec moi\n"
            "`/image [description]` — je crée une image 🎨\n"
            "`/memoire` — voir ce que j'ai retenu sur toi\n"
            "`/oublie [information]` — me faire oublier une info\n"
            "`/reset` — repartir d'une conversation vierge\n"
            "`/salons` — (admins) choisir où je peux répondre\n"
            "`/moderation` — (admins) activer ma modération 🛡️\n"
            "`/ping` — vérifier que je suis réveillée\n"
            "`/help` — afficher ce message"
        ),
        inline=False,
    )
    embed.add_field(
        name="Modération (si activée par un admin)",
        value=(
            "Les modérateurs peuvent m'écrire en mentionnant @Capri, par exemple :\n"
            "`ban @pseudo spam` • `mute @pseudo 30 minutes` • `kick @pseudo` • "
            "`unmute @pseudo` • `unban [ID]` • `supprime les 20 derniers messages`\n"
            "Je demande toujours confirmation avant d'agir."
        ),
        inline=False,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


salons = app_commands.Group(
    name="salons",
    description="Choisir les salons où Capri répond (admins)",
    guild_only=True,
    default_permissions=discord.Permissions(manage_guild=True),
)


def _is_admin(interaction: discord.Interaction) -> bool:
    return interaction.permissions.manage_guild or interaction.permissions.administrator


async def _refuse(interaction: discord.Interaction):
    await interaction.response.send_message(
        "Seuls les admins du serveur peuvent faire ça chou 🙈", ephemeral=True
    )


@salons.command(name="ajouter", description="Autoriser Capri à répondre dans un salon")
@app_commands.describe(salon="Le salon à autoriser")
async def salons_ajouter(interaction: discord.Interaction, salon: discord.TextChannel):
    if not _is_admin(interaction):
        return await _refuse(interaction)
    db.add_allowed_channel(interaction.guild_id, salon.id)
    await interaction.response.send_message(
        f"C'est noté chou : je réponds maintenant dans {salon.mention} ✅", ephemeral=True
    )


@salons.command(name="retirer", description="Retirer un salon de la liste")
@app_commands.describe(salon="Le salon à retirer")
async def salons_retirer(interaction: discord.Interaction, salon: discord.TextChannel):
    if not _is_admin(interaction):
        return await _refuse(interaction)
    db.remove_allowed_channel(interaction.guild_id, salon.id)
    await interaction.response.send_message(
        f"{salon.mention} retiré de la liste ✅", ephemeral=True
    )


@salons.command(name="liste", description="Voir les salons autorisés")
async def salons_liste(interaction: discord.Interaction):
    allowed = db.get_allowed_channels(interaction.guild_id)
    if not allowed:
        msg = "Aucun salon n'est configuré : je peux répondre partout (quand on me mentionne) 💕"
    else:
        msg = "Je réponds dans : " + ", ".join(f"<#{c}>" for c in allowed)
    await interaction.response.send_message(msg, ephemeral=True)


@salons.command(name="tous", description="Autoriser tous les salons (efface la liste)")
async def salons_tous(interaction: discord.Interaction):
    if not _is_admin(interaction):
        return await _refuse(interaction)
    db.clear_allowed_channels(interaction.guild_id)
    await interaction.response.send_message(
        "Parfait, je peux maintenant répondre dans tous les salons ✅", ephemeral=True
    )


bot.tree.add_command(salons)
bot.tree.add_command(moderation.group)


if not config.DISCORD_TOKEN:
    raise SystemExit("❌ DISCORD_TOKEN introuvable. Vérifie ton fichier .env")
if not config.GEMINI_API_KEY:
    raise SystemExit("❌ GEMINI_API_KEY introuvable. Vérifie ton fichier .env")

bot.run(config.DISCORD_TOKEN)