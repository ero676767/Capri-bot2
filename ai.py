import asyncio
import json
import logging
import base64
from dataclasses import dataclass
from typing import Optional

from google import genai
from google.genai import errors, types
from pydantic import BaseModel, Field

import config
import db
from personality import build_system_prompt

log = logging.getLogger("capri.ai")
client = genai.Client(api_key=config.GEMINI_API_KEY)


class CapriReply(BaseModel):
    answer: str = Field(description="Réponse naturelle de Capri")
    image_prompt: str = Field(
        default="", description="Prompt d'image si la personne demande explicitement une image, sinon vide"
    )
    facts_to_remember: list[str] = Field(default_factory=list)


@dataclass
class ImageResult:
    status: str
    data: Optional[bytes] = None
    mime: str = "image/png"


def _parse(response):
    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, CapriReply):
        return parsed
    raw = (getattr(response, "text", None) or "").strip()
    try:
        data = json.loads(raw)
        return CapriReply(
            answer=str(data.get("answer") or data.get("reply") or "").strip(),
            image_prompt=str(data.get("image_prompt") or "").strip(),
            facts_to_remember=data.get("facts_to_remember") or [],
        )
    except (json.JSONDecodeError, TypeError, AttributeError, ValueError):
        return CapriReply(answer=raw)


async def ask_capri(
    user_id: int,
    user_name: str,
    text: str,
    media_parts=None,
    media_note: str = "",
):
    """Retourne CapriReply ou None si Gemini ne répond pas."""
    media_parts = media_parts or []
    history = db.get_history(user_id, config.MAX_HISTORY_MESSAGES)
    facts = db.get_facts(user_id)

    contents = [
        types.Content(role=role, parts=[types.Part.from_text(text=msg)])
        for role, msg in history
    ]
    user_parts = [types.Part.from_text(text=(text + ("\n" + media_note if media_note else "")))]
    user_parts.extend(media_parts)
    contents.append(types.Content(role="user", parts=user_parts))

    generation_config = types.GenerateContentConfig(
        system_instruction=build_system_prompt(user_name, facts),
        temperature=1.0,
        response_mime_type="application/json",
        response_schema=CapriReply,
    )

    models = [config.GEMINI_MODEL]
    if config.GEMINI_FALLBACK_MODEL and config.GEMINI_FALLBACK_MODEL not in models:
        models.append(config.GEMINI_FALLBACK_MODEL)

    for model in models:
        for attempt in range(config.MAX_RETRIES):
            try:
                response = await client.aio.models.generate_content(
                    model=model, contents=contents, config=generation_config
                )
                result = _parse(response)
                if not result.answer:
                    log.warning("Réponse vide du modèle %s", model)
                    break
                db.add_exchange(user_id, text, result.answer)
                if result.facts_to_remember:
                    db.add_facts(user_id, result.facts_to_remember)
                return result
            except errors.APIError as exc:
                code = getattr(exc, "code", None)
                log.warning("Erreur Gemini %s sur %s", code, model)
                if code in (429, 500, 503, 504) and attempt + 1 < config.MAX_RETRIES:
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                break
            except Exception:
                log.exception("Erreur inattendue pendant ask_capri")
                break
    return None


async def generate_image(prompt: str, input_parts=None) -> ImageResult:
    """
    Génère ou modifie une image avec Gemini 3.1 Flash Image.
    Utilise l'API Interactions de Google.
    """
    input_parts = input_parts or []

    try:
        # Préparation du texte et des images de référence
        interaction_input = [
            {
                "type": "text",
                "text": prompt
            }
        ]

        for part in input_parts:
            inline = getattr(part, "inline_data", None)

            if inline is None or not inline.data:
                continue

            image_data = inline.data

            if isinstance(image_data, str):
                encoded_data = image_data
            else:
                encoded_data = base64.b64encode(image_data).decode("utf-8")

            interaction_input.append({
                "type": "image",
                "data": encoded_data,
                "mime_type": getattr(inline, "mime_type", None) or "image/png"
            })

        # L'appel SDK est synchrone : on le déplace dans un thread
        interaction = await asyncio.to_thread(
            client.interactions.create,
            model=config.GEMINI_IMAGE_MODEL,
            input=interaction_input,
        )

        # Récupération directe de l'image
        output_image = getattr(interaction, "output_image", None)

        if output_image and getattr(output_image, "data", None):
            image_data = output_image.data

            if isinstance(image_data, str):
                image_data = base64.b64decode(image_data)

            mime = getattr(output_image, "mime_type", None) or "image/png"

            return ImageResult(
                status="ok",
                data=image_data,
                mime=mime
            )

        # Solution de secours : lecture des étapes de réponse
        for step in getattr(interaction, "steps", []) or []:
            if getattr(step, "type", None) != "model_output":
                continue

            for block in getattr(step, "content", []) or []:
                if getattr(block, "type", None) == "image":
                    encoded = block.data
                    image_data = (
                        base64.b64decode(encoded)
                        if isinstance(encoded, str)
                        else encoded
                    )

                    return ImageResult(
                        status="ok",
                        data=image_data,
                        mime=getattr(block, "mime_type", None) or "image/png"
                    )

        log.warning("Gemini n'a retourné aucune image.")
        return ImageResult(status="unavailable")

    except errors.APIError as exc:
        code = getattr(exc, "code", None)
        log.exception("Erreur API génération image : %s", code)

        if code == 404:
            return ImageResult(status="model")

        if code == 403:
            return ImageResult(status="model")

        if code == 400:
            return ImageResult(status="blocked")

        return ImageResult(status="unavailable")

    except Exception:
        log.exception("Erreur inattendue pendant la génération d'image")
        return ImageResult(status="unavailable")