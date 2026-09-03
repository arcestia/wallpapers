"""Lightweight CLIP Vision Classifier for Visual Wallpaper Categorization."""

import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from PIL import Image

from .config import CATEGORIES
from curate_categories import CATEGORY_PROMPTS

_CLIP_MODEL = None
_CLIP_PROCESSOR = None
_CLIP_TEXT_EMBEDDINGS = None


def is_vision_available() -> bool:
    """Check if PyTorch and Transformers are installed."""
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
        return True
    except ImportError:
        return False


def load_clip_model(model_name: str = "openai/clip-vit-base-patch32"):
    """Lazy load CLIP vision model and compute pre-cached text embeddings."""
    global _CLIP_MODEL, _CLIP_PROCESSOR, _CLIP_TEXT_EMBEDDINGS

    if _CLIP_MODEL is not None:
        return _CLIP_MODEL, _CLIP_PROCESSOR, _CLIP_TEXT_EMBEDDINGS

    import torch
    from transformers import CLIPModel, CLIPProcessor

    device = "cuda" if torch.cuda.is_available() else "cpu"
    processor = CLIPProcessor.from_pretrained(model_name)
    model = CLIPModel.from_pretrained(model_name).to(device)
    model.eval()

    # Pre-encode text category prompts for ultra-fast visual inference
    prompt_list = [CATEGORY_PROMPTS[cat] for cat in CATEGORIES]
    text_inputs = processor(text=prompt_list, return_tensors="pt", padding=True).to(device)

    with torch.no_grad():
        text_features = model.get_text_features(**text_inputs)
        # Normalize text embeddings
        text_features = text_features / text_features.norm(dim=-1, keepdim=True)

    _CLIP_MODEL = model
    _CLIP_PROCESSOR = processor
    _CLIP_TEXT_EMBEDDINGS = text_features
    return _CLIP_MODEL, _CLIP_PROCESSOR, _CLIP_TEXT_EMBEDDINGS


def classify_image_visually(
    file_path: Path,
    model_name: str = "openai/clip-vit-base-patch32",
    top_k: int = 3,
) -> Optional[List[Tuple[str, float]]]:
    """
    Classify an image file using CLIP zero-shot vision model.
    Returns ranked list of (category, confidence) tuples, or None if unavailable.
    """
    if not is_vision_available():
        return None

    try:
        import torch
        model, processor, text_embeddings = load_clip_model(model_name)
        device = text_embeddings.device

        with Image.open(file_path) as img:
            rgb_img = img.convert("RGB")
            # Downsample for fast feature extraction
            inputs = processor(images=rgb_img, return_tensors="pt").to(device)

        with torch.no_grad():
            image_features = model.get_image_features(**inputs)
            image_features = image_features / image_features.norm(dim=-1, keepdim=True)

            # Cosine similarity * temperature scaling (100.0)
            similarity = (image_features @ text_embeddings.T) * 100.0
            probs = torch.nn.functional.softmax(similarity, dim=-1)[0]

        scores = [(CATEGORIES[i], float(probs[i])) for i in range(len(CATEGORIES))]
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:top_k]

    except Exception:
        return None
