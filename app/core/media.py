"""Bounded image preparation and source-to-thread media completeness checks."""
from .image_runtime import REPAIR_MESSAGE
from dataclasses import dataclass
from io import BytesIO
import logging
import warnings

# app.bsky.embed.images currently permits 2,000,000 bytes per image.
MAX_IMAGE_BYTES = 2_000_000
MAX_IMAGE_DIMENSION = 4000
MAX_SOURCE_BYTES = 20_000_000
MAX_SOURCE_PIXELS = 40_000_000
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreparedImage:
    data: bytes
    mime_type: str
    width: int
    height: int


class MediaMismatchError(ValueError):
    pass


def prepare_image(data):
    # Text-only sync remains available when image processing is unavailable.
    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
    except (ImportError, OSError) as exc:
        raise RuntimeError(REPAIR_MESSAGE) from exc
    if not data or len(data) > MAX_SOURCE_BYTES:
        raise ValueError("Source image is empty or exceeds the download size limit")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as original:
                width, height = original.size
                if width * height > MAX_SOURCE_PIXELS:
                    raise ValueError("Source image exceeds the decoded pixel limit")
                if getattr(original, "n_frames", 1) != 1:
                    raise ValueError("Animated images require video handling")
                original.load()
                orientation = original.getexif().get(274, 1)
                if (original.format in {"JPEG", "PNG"} and len(data) <= MAX_IMAGE_BYTES
                        and max(original.size) <= MAX_IMAGE_DIMENSION and orientation == 1):
                    return PreparedImage(data, "image/jpeg" if original.format == "JPEG" else "image/png", width, height)
                image = ImageOps.exif_transpose(original)
                image.thumbnail((MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION), Image.Resampling.LANCZOS)
                alpha = image.mode in {"RGBA", "LA"} or "transparency" in image.info
                use_png = original.format == "PNG" or alpha
                if use_png:
                    image = image.convert("RGBA" if alpha else "RGB")
                    for _ in range(18):
                        output = BytesIO()
                        image.save(output, format="PNG", optimize=True)
                        result = output.getvalue()
                        if len(result) <= MAX_IMAGE_BYTES:
                            logger.info("Prepared image: %d -> %d bytes (%dx%d)", len(data), len(result), *image.size)
                            return PreparedImage(result, "image/png", *image.size)
                        image.thumbnail((max(1, int(image.width * .85)), max(1, int(image.height * .85))), Image.Resampling.LANCZOS)
                else:
                    profile = original.info.get("icc_profile") if original.mode == "RGB" else None
                    options = {"icc_profile": profile} if isinstance(profile, bytes) and len(profile) <= 100_000 else {}
                    image = image.convert("RGB")
                    for _ in range(10):
                        for quality in (92, 85, 78, 70, 60):
                            output = BytesIO()
                            image.save(output, format="JPEG", quality=quality, optimize=True, progressive=True, **options)
                            result = output.getvalue()
                            if len(result) <= MAX_IMAGE_BYTES:
                                logger.info("Prepared image: %d -> %d bytes (%dx%d)", len(data), len(result), *image.size)
                                return PreparedImage(result, "image/jpeg", *image.size)
                        image.thumbnail((max(1, int(image.width * .85)), max(1, int(image.height * .85))), Image.Resampling.LANCZOS)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError("Invalid or unsafe image data") from exc
    raise ValueError("Could not prepare image within Bluesky's size limit")


def validate_image_inventory(records, expected_alts):
    """Require every declared image on the root, with original alt text."""
    if expected_alts is None:
        raise MediaMismatchError("Source image inventory is missing; review this recovered thread")
    if not isinstance(expected_alts, list) or len(expected_alts) > 4:
        raise MediaMismatchError("Invalid source image inventory")
    values = [record.model_dump(mode="json", by_alias=True, exclude_none=True)
              if hasattr(record, "model_dump") else record for record in records]
    if not values:
        raise MediaMismatchError("A thread must have a first post")
    images = (values[0].get("embed") or {}).get("images", [])
    if len(images) != len(expected_alts):
        raise MediaMismatchError(f"Expected {len(expected_alts)} image(s) on the first post; found {len(images)}")
    for image, alt in zip(images, expected_alts):
        if image.get("alt", "") != (alt or ""):
            raise MediaMismatchError("The first post does not preserve source image alt text")
        size = (image.get("image") or {}).get("size")
        if not isinstance(size, int) or size > MAX_IMAGE_BYTES or size <= 0:
            raise MediaMismatchError("An image blob exceeds Bluesky's size limit or has invalid size metadata")
    if any((record.get("embed") or {}).get("images") for record in values[1:]):
        raise MediaMismatchError("Source images must be attached to the first thread post")
