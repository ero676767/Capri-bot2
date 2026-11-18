
import asyncio
import json
import logging
import base64
import time
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

# Délais maximums
AI_TIMEOUT = 35
IMAGE_TIMEOUT = 90


class CapriReply(BaseModel):
    answer: str = Field(description="Réponse naturelle de Capri")
    image_prompt: str = Field(
        default="",
        description="Prompt d'image si la personne demande explicitement une image, sinon vide"
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

    if isinstance(parsed, dict):
        return CapriReply(**parsed)

    raw = (getattr(response, "text", None) or "").strip()

    if not raw:
        return CapriReply(answer="")

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
    """Retourne CapriReply ou None si aucun modèle ne répond."""

    started = time.monotonic()
    media_parts = media_parts or []

    try:
        history = db.get_history(user_id, config.MAX_HISTORY_MESSAGES)
        facts = db.get_facts(user_id)

        contents = [
            types.Content(
                role=role,
                parts=[types.Part.from_text(text=msg)]
            )
            for role, msg in history
        ]

        user_parts = [
            types.Part.from_text(
                text=text + ("\n" + media_note if media_note else "")
            )
        ]

        user_parts.extend(media_parts)

        contents.append(
            types.Content(role="user", parts=user_parts)
        )

        generation_config = types.GenerateContentConfig(
            system_instruction=build_system_prompt(user_name, facts),
            temperature=1.0,
            response_mime_type="application/json",
            response_schema=CapriReply,
        )

    except Exception:
        log.exception("Erreur pendant la préparation de la demande Capri")
        return None

    models = [config.GEMINI_MODEL]

    if (
        config.GEMINI_FALLBACK_MODEL
        and config.GEMINI_FALLBACK_MODEL not in models
    ):
        models.append(config.GEMINI_FALLBACK_MODEL)

    for model_index, model in enumerate(models):

        log.info("Capri interroge le modèle %s", model)

        # On limite les tentatives pour éviter de faire attendre l'utilisateur.
        max_attempts = 2

        for attempt in range(max_attempts):

            try:
                response = await asyncio.wait_for(
                    client.aio.models.generate_content(
                        model=model,
                        contents=contents,
                        config=generation_config,
                    ),
                    timeout=AI_TIMEOUT,
                )

                result = _parse(response)

                if not result.answer:
                    log.warning("Réponse vide du modèle %s", model)
                    break

                db.add_exchange(user_id, text, result.answer)

                if result.facts_to_remember:
                    db.add_facts(user_id, result.facts_to_remember)

                elapsed = time.monotonic() - started

                log.info(
                    "Capri a répondu en %.2f secondes avec %s",
                    elapsed,
                    model,
                )

                return result

            except asyncio.TimeoutError:
                log.warning(
                    "Délai dépassé pour %s après %s secondes",
                    model,
                    AI_TIMEOUT,
                )
                # On passe directement au modèle de secours.
                break

            except errors.APIError as exc:
                code = getattr(exc, "code", None)

                log.warning(
                    "Erreur Gemini %s sur %s : %s",
                    code,
                    model,
                    exc,
                )

                # Si le service est indisponible, inutile d'attendre :
                # on passe directement au modèle suivant.
                if code in (500, 503, 504, 404):
                    break

                # En cas de limite de requêtes, une seule nouvelle tentative.
                if code == 429 and attempt == 0:
                    await asyncio.sleep(2)
                    continue

                break

            except Exception:
                log.exception(
                    "Erreur inattendue pendant ask_capri avec %s",
                    model,
                )
                break

        if model_index + 1 < len(models):
            log.info("Capri essaie maintenant le modèle de secours.")

    elapsed = time.monotonic() - started

    log.error(
        "Aucun modèle Gemini n'a répondu après %.2f secondes",
        elapsed,
    )

    return None


async def generate_image(prompt: str, input_parts=None) -> ImageResult:
    """
    Génère ou modifie une image avec Gemini.
    Utilise l'API Interactions de Google.
    """

    input_parts = input_parts or []

    try:
        interaction_input = [
            {
                "type": "text",
                "text": prompt,
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
                encoded_data = base64.b64encode(
                    image_data
                ).decode("utf-8")

            interaction_input.append({
                "type": "image",
                "data": encoded_data,
                "mime_type": getattr(
                    inline, "mime_type", None
                ) or "image/png",
            })

        log.info("Capri commence la génération d'une image.")

        interaction = await asyncio.wait_for(
            asyncio.to_thread(
                client.interactions.create,
                model=config.GEMINI_IMAGE_MODEL,
                input=interaction_input,
            ),
            timeout=IMAGE_TIMEOUT,
        )

        output_image = getattr(interaction, "output_image", None)

        if output_image and getattr(output_image, "data", None):
            image_data = output_image.data

            if isinstance(image_data, str):
                image_data = base64.b64decode(image_data)

            mime = getattr(
                output_image, "mime_type", None
            ) or "image/png"

            log.info("Image générée avec succès.")

            return ImageResult(
                status="ok",
                data=image_data,
                mime=mime,
            )

        # Recherche dans les étapes de réponse si nécessaire.
        for step in getattr(interaction, "steps", []) or []:

            if getattr(step, "type", None) != "model_output":
                continue

            for block in getattr(step, "content", []) or []:

                if getattr(block, "type", None) != "image":
                    continue

                encoded = block.data

                image_data = (
                    base64.b64decode(encoded)
                    if isinstance(encoded, str)
                    else encoded
                )

                return ImageResult(
                    status="ok",
                    data=image_data,
                    mime=getattr(
                        block, "mime_type", None
                    ) or "image/png",
                )

        log.warning("Gemini n'a retourné aucune image.")

        return ImageResult(status="unavailable")

    except asyncio.TimeoutError:
        log.error("La génération d'image a dépassé le délai maximum.")
        return ImageResult(status="unavailable")

    except errors.APIError as exc:
        code = getattr(exc, "code", None)

        log.exception("Erreur API génération image : %s", code)

        if code in (404, 403):
            return ImageResult(status="model")

        if code == 400:
            return ImageResult(status="blocked")

        return ImageResult(status="unavailable")

    except Exception:
        log.exception("Erreur inattendue pendant la génération d'image")
        return ImageResult(status="unavailable")
