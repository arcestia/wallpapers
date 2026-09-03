"""Classification for AI detection and Wallpaper Categorization."""

import re
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple
from PIL import Image
Image.MAX_IMAGE_PIXELS = None  # Allow 8K/16K wallpapers without decompression warning
from PIL.ExifTags import TAGS

from .config import CATEGORIES, TYPES
from .vision_classifier import classify_image_visually, is_vision_available
from curate_categories import (
    CATEGORY_KEYWORD_MAP,
    CATEGORY_PATTERNS,
    normalize_category_hint,
    resolve_category_tie,
    score_text_against_registry,
    score_text_with_matches,
)


class ClassificationResult(NamedTuple):
    type: str  # "AI", "NON-AI", "UNKNOWN"
    category: str  # One of the official categories
    ai_confidence: float  # 0.0 to 1.0
    detected_signals: str = ""  # Explanation of detected signals


def normalize_text(text: str) -> str:
    """Normalize text by replacing separators and non-alphanumeric chars with spaces."""
    return re.sub(r"[^a-zA-Z0-9]+", " ", text).lower()


def extract_detailed_image_metadata(file_path: Path) -> Dict[str, Any]:
    """
    Extract comprehensive metadata from image headers, EXIF tags, and PNG chunks.
    """
    metadata: Dict[str, Any] = {
        "text_chunks": {},
        "exif_tags": {},
        "has_camera_exif": False,
        "ai_generation_parameters": None,
    }

    try:
        with Image.open(file_path) as img:
            # 1. PNG / WebP Text Info Chunks
            if hasattr(img, "info") and img.info:
                for k, v in img.info.items():
                    if isinstance(v, str):
                        metadata["text_chunks"][k] = v
                        # Check for AI generation parameters
                        if k in ["parameters", "prompt", "workflow", "Comment", "sd-metadata"]:
                            metadata["ai_generation_parameters"] = v

            # 2. EXIF Data (JPEG, TIFF, WebP)
            exif_raw = img.getexif()
            if exif_raw:
                for tag_id, val in exif_raw.items():
                    tag_name = TAGS.get(tag_id, str(tag_id))
                    if isinstance(val, (str, int, float)):
                        metadata["exif_tags"][tag_name] = str(val)

                # Check for camera hardware signatures
                camera_keys = ["Make", "Model", "FNumber", "ExposureTime", "ISOSpeedRatings", "FocalLength"]
                if any(k in metadata["exif_tags"] for k in camera_keys):
                    metadata["has_camera_exif"] = True

    except Exception:
        pass

    return metadata


def classify_ai(
    file_path: Path,
    source: Optional[str] = None,
    source_url: Optional[str] = None,
    tags: Optional[List[str]] = None,
    metadata_hint: Optional[Dict] = None,
) -> Tuple[str, float, str]:
    """
    Classify whether an image is AI, NON-AI, or UNKNOWN with confidence score.
    Returns (classification, confidence, detected_signal).
    """
    hint_type = (metadata_hint or {}).get("type")
    if hint_type in TYPES:
        return hint_type, 1.0, f"Explicit type hint: {hint_type}"

    # Extract deep file metadata
    img_meta = extract_detailed_image_metadata(file_path)

    # 1. Embedded AI Prompt / Generation Parameters Check (Definitive AI)
    if img_meta.get("ai_generation_parameters"):
        param_str = str(img_meta["ai_generation_parameters"]).lower()
        sd_markers = ["steps:", "sampler:", "cfg scale:", "seed:", "model:", "negative prompt:",
                      "denoising strength:", "clip skip:", "ensd:", "vae:", "lora:", "lycoris:",
                      " hires", "adetailer", "controlnet", "ip-adapter", "faceid"]
        if any(w in param_str for w in sd_markers):
            return "AI", 0.99, "Embedded Stable Diffusion/WebUI generation parameters in PNG chunk"
        if any(w in param_str for w in ["comfyui", "comfy", "workflow", "node id", "class type"]):
            return "AI", 0.99, "Embedded ComfyUI workflow metadata"

    # Build searchable corpus
    tag_corpus = " ".join(tags or [])
    raw_text = f"{file_path.name} {file_path.parent.name} {source or ''} {source_url or ''} {tag_corpus}"
    text_chunks_str = " ".join(f"{k}: {v}" for k, v in img_meta.get("text_chunks", {}).items())
    exif_str = " ".join(f"{k}: {v}" for k, v in img_meta.get("exif_tags", {}).items())
    combined = normalize_text(f"{raw_text} {text_chunks_str} {exif_str}")

    # 2. Known AI Indicators (Filename, Source, Tags, Software)
    ai_patterns = [
        (r"\bmidjourney\b", "Midjourney tag/metadata"),
        (r"\bmidjourney\s*v?\d+\b", "Midjourney version tag"),
        (r"\bstable\s+diffusion\b", "Stable Diffusion tag/metadata"),
        (r"\bstable\s+diffusion\s*xl\b", "SDXL model"),
        (r"\bsdxl\b", "SDXL model"),
        (r"\bsd\s*3\b", "Stable Diffusion 3"),
        (r"\bflux\b", "FLUX.1 AI model"),
        (r"\bflux\.1\b", "FLUX.1 AI model"),
        (r"\bdall\s*e\b", "DALL-E signature"),
        (r"\bdall\s*e\s*\d+\b", "DALL-E version signature"),
        (r"\bnovelai\b", "NovelAI generation tag"),
        (r"\bcomfyui\b", "ComfyUI workflow metadata"),
        (r"\bautomatic1111\b", "Automatic1111 WebUI"),
        (r"\binvokeai\b", "InvokeAI"),
        (r"\bfooocus\b", "Fooocus UI"),
        (r"\bleonardo\.ai\b", "Leonardo.AI"),
        (r"\bimagine\.ai\b", "Imagine.AI"),
        (r"\bnightcafe\b", "NightCafe"),
        (r"\bartbreeder\b", "Artbreeder"),
        (r"\bcraiyon\b", "Craiyon (DALL-E mini)"),
        (r"\bhugging\s*face\b", "Hugging Face / Diffusers"),
        (r"\bdiffusers\b", "Hugging Face Diffusers"),
        (r"\btext2image\b", "Text-to-image pipeline"),
        (r"\btext\s*to\s*image\b", "Text-to-image pipeline"),
        (r"\bimg2img\b", "Image-to-image pipeline"),
        (r"\bimage2image\b", "Image-to-image pipeline"),
        (r"\bai\s+generated\b", "AI-generated source tag"),
        (r"\bai\s+art\b", "AI Art tag"),
        (r"\bgenerated\s*by\s*ai\b", "AI-generated declaration"),
        (r"\bcreated\s*with\s*ai\b", "AI-generated declaration"),
        (r"\bmade\s*with\s*ai\b", "AI-generated declaration"),
        (r"\bprompt\s*:\s*", "Prompt syntax header"),
        (r"\bnegative\s*prompt\s*:\s*", "Negative prompt syntax"),
        (r"\bsteps:\s*\d+", "Generation steps metadata"),
        (r"\bcivitai\b", "Civitai model platform"),
        (r"\blexica\b", "Lexica AI repository"),
        (r"\bprompthero\b", "PromptHero AI gallery"),
        (r"\bopenart\b", "OpenArt AI gallery"),
        (r"\bpixelz\.ai\b", "Pixelz.AI"),
        (r"\bskiddle\s+generated\b", "Skiddle AI Generator"),
    ]

    for pat, label in ai_patterns:
        if re.search(pat, combined):
            return "AI", 0.95, f"AI indicator detected: {label}"

    # 3. Known NON-AI Indicators (Camera EXIF, Official Studios, Verified Stock Photo Portals)
    if img_meta.get("has_camera_exif"):
        make = img_meta["exif_tags"].get("Make", "Camera")
        model = img_meta["exif_tags"].get("Model", "")
        return "NON-AI", 0.95, f"Physical camera hardware EXIF ({make} {model})".strip()

    non_ai_patterns = [
        (r"\bunsplash\b", "Unsplash photography"),
        (r"\bpixabay\b", "Pixabay stock photo"),
        (r"\bpexels\b", "Pexels photography"),
        (r"\bshutterstock\b", "Shutterstock"),
        (r"\badobe\s*stock\b", "Adobe Stock"),
        (r"\bgetty\s*images\b", "Getty Images"),
        (r"\bistock\b", "iStock"),
        (r"\balamy\b", "Alamy stock"),
        (r"\bdreamstime\b", "Dreamstime"),
        (r"\bflickr\b", "Flickr"),
        (r"\b500px\b", "500px"),
        (r"\bsmugmug\b", "SmugMug"),
        (r"\bcanon\b", "Canon photography"),
        (r"\bcanon\s*eos\b", "Canon EOS camera"),
        (r"\bnikon\b", "Nikon photography"),
        (r"\bsony\s*alpha\b", "Sony Alpha camera"),
        (r"\bfujifilm\b", "Fujifilm camera"),
        (r"\bolympus\b", "Olympus camera"),
        (r"\bpanasonic\b", "Panasonic camera"),
        (r"\bpanasonic\s*lumix\b", "Panasonic Lumix"),
        (r"\bleica\b", "Leica camera"),
        (r"\bhasselblad\b", "Hasselblad camera"),
        (r"\bphase\s*one\b", "Phase One camera"),
        (r"\bdji\b", "DJI drone"),
        (r"\bdrone\s*photography\b", "Drone photography"),
        (r"\bpokemon\b", "Official Pokémon franchise"),
        (r"\bpikachu\b", "Official Pokémon media"),
        (r"\bnintendo\b", "Official Nintendo artwork"),
        (r"\bdisney\b", "Official Disney artwork"),
        (r"\bpixar\b", "Official Pixar artwork"),
        (r"\bmarvel\b", "Official Marvel artwork"),
        (r"\bdc\s*comics\b", "Official DC artwork"),
        (r"\bwarner\s*bros\b", "Official Warner Bros artwork"),
        (r"\buniversal\s*pictures\b", "Official Universal artwork"),
        (r"\bparamount\b", "Official Paramount artwork"),
        (r"\bsony\s*pictures\b", "Official Sony Pictures artwork"),
        (r"\bofficial\b", "Official/licensed artwork"),
        (r"\bconcept\s*art\b", "Official concept art"),
        (r"\bofficial\s*art\b", "Official artwork"),
        (r"\bkyoto\s*animation\b", "Kyoto Animation studio"),
        (r"\bufotable\b", "Ufotable studio"),
        (r"\bghibli\b", "Studio Ghibli"),
        (r"\bmappa\b", "MAPPA studio"),
        (r"\bwit\s*studio\b", "WIT Studio"),
        (r"\btrigger\b", "Studio Trigger"),
        (r"\bbones\b", "Bones studio"),
        (r"\bmadhouse\b", "Madhouse studio"),
        (r"\ba1\s*pictures\b", "A-1 Pictures"),
        (r"\bproduction\s*i\.g\b", "Production I.G"),
        (r"\bsunrise\b", "Sunrise studio"),
        (r"\bgainax\b", "Gainax studio"),
    ]

    for pat, label in non_ai_patterns:
        if re.search(pat, combined):
            return "NON-AI", 0.90, f"NON-AI source/studio detected: {label}"

    # 4. Fallback to UNKNOWN when evidence is not conclusive
    return "UNKNOWN", 0.5, "Insufficient provenance data for definitive classification"


def classify_category(
    file_path: Path,
    category_hint: Optional[str] = None,
    source_url: Optional[str] = None,
    tags: Optional[List[str]] = None,
) -> str:
    """
    Assign one of the registered official categories using weighted multi-signal scoring.
    """
    if category_hint:
        canonical = normalize_category_hint(category_hint)
        if canonical:
            return canonical

    tag_str = " ".join(tags or [])
    raw_text = f"{file_path.name} {file_path.parent.name} {source_url or ''} {tag_str}"

    scores = score_text_against_registry(raw_text)
    best = resolve_category_tie(scores)
    if best != "Other":
        return best

    # Visual Fallback: Use CLIP zero-shot vision model if available
    if is_vision_available():
        vision_scores = classify_image_visually(file_path, top_k=1)
        if vision_scores and vision_scores[0][1] >= 0.20:
            return vision_scores[0][0]

    return "Other"


def classify_category_detailed(
    file_path: Path,
    category_hint: Optional[str] = None,
    source_url: Optional[str] = None,
    tags: Optional[List[str]] = None,
) -> Tuple[str, str, int, List[str]]:
    """
    Full classification with transparent scoring breakdown.
    Returns: (category, source_method, confidence_percent, matched_keywords)
    """
    if category_hint:
        canonical = normalize_category_hint(category_hint)
        if canonical:
            return canonical, "explicit hint", 100, [category_hint]

    tag_str = " ".join(tags or [])
    raw_text = f"{file_path.name} {file_path.parent.name} {source_url or ''} {tag_str}"

    scores, matches = score_text_with_matches(raw_text)
    best = resolve_category_tie(scores)
    max_score = max(scores.values())

    if best != "Other":
        conf = min(99, 50 + max_score * 5)
        kw = matches[best]
        sig = f"Rule-based scoring ({max_score} pts): {', '.join(kw[:6]) or 'matched'}"
        return best, "weighted rules", conf, kw

    # Visual Fallback: Use CLIP zero-shot vision model if available
    if is_vision_available():
        vision_scores = classify_image_visually(file_path, top_k=1)
        if vision_scores and vision_scores[0][1] >= 0.20:
            return vision_scores[0][0], "clip zero-shot vision", int(vision_scores[0][1] * 100), []

    return "Other", "fallback", 50, []


def classify_image(
    file_path: Path,
    source: Optional[str] = None,
    source_url: Optional[str] = None,
    tags: Optional[List[str]] = None,
    category_hint: Optional[str] = None,
    metadata_hint: Optional[Dict] = None,
) -> ClassificationResult:
    """
    Full classification pipeline: AI detection + category assignment.
    """
    ai_type, ai_conf, ai_signal = classify_ai(file_path, source, source_url, tags, metadata_hint)
    category = classify_category(file_path, category_hint, source_url, tags)
    return ClassificationResult(
        type=ai_type,
        category=category,
        ai_confidence=ai_conf,
        detected_signals=ai_signal,
    )
