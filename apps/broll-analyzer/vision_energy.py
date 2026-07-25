"""
vision_energy.py
Optional, fully local "excitement / high energy" scoring for video frames.

Uses CLIP (via the open-source `open_clip` library) for zero-shot
classification: each frame is compared against a small set of
"exciting/high energy" text prompts versus "calm/static" text prompts,
and the relative similarity becomes a 0-100 "energy score".

IMPORTANT - this is intentionally NOT connected to Anthropic's API (or
any other cloud API) in any way:
  - The model (open_clip's "ViT-B-32", OpenAI-trained weights) is a
    free, open-source checkpoint. It is downloaded once, on first use,
    from its normal public host (Hugging Face Hub / LAION's release
    infrastructure) via the open_clip library itself, and then cached
    locally (typically under ~/.cache).
  - After that one-time download, every frame is scored entirely
    on-device (CPU or GPU, whatever's available) with zero network
    calls, zero external API keys, and no data leaving the machine.

This module is optional: if `torch` / `open_clip_torch` aren't
installed, `is_available()` returns False and the rest of the app
degrades gracefully (technical scoring still works normally).
"""

import threading
from typing import List, Optional, Sequence

import numpy as np
import cv2

MODEL_NAME = "ViT-B-32"
PRETRAINED = "openai"

# Averaging several phrasings smooths out the sensitivity CLIP has to
# exact wording, giving a steadier score than any single prompt would.
POSITIVE_PROMPTS = [
    "an exciting, high energy, dynamic action shot",
    "fast motion, adrenaline, a thrilling moment",
    "intense action with rapid movement and energy",
    "a dramatic, high-intensity moment",
]
NEGATIVE_PROMPTS = [
    "a calm, static, low energy shot",
    "a still, quiet, boring scene",
    "a slow, motionless, uneventful moment",
    "a plain, low-intensity everyday scene",
]

# Softmax temperature for turning the pos/neg similarity gap into a
# 0-100 probability-like score. Lower = sharper separation.
_TEMPERATURE = 0.05

# How many frames go through the model per forward pass in
# score_frames_energy. Large enough to amortize the per-call dispatch/
# overhead that made the old one-frame-at-a-time path slow, small enough
# that a batch of preprocessed 224x224 tensors plus activations stays
# comfortably bounded in memory. Callers that accumulate frames across a
# decode loop (analyzer.analyze_clip) also use this as their flush
# threshold, so a long clip never holds more than one batch of frames at
# a time.
BATCH_SIZE = 32

_lock = threading.Lock()
_model = None
_preprocess = None
_device = "cpu"
_text_features = None  # (pos_mean, neg_mean) tensors, set once loaded


class VisionEnergyError(Exception):
    """Raised when the optional local vision model can't be used."""
    pass


def is_available() -> bool:
    """Cheap check for whether the optional dependencies are installed.
    Does not load the (larger) model weights."""
    try:
        import torch  # noqa: F401
        import open_clip  # noqa: F401
        return True
    except ImportError:
        return False


def _ensure_loaded():
    global _model, _preprocess, _device, _text_features
    if _model is not None:
        return
    with _lock:
        if _model is not None:
            return
        try:
            import torch
            import open_clip
        except ImportError as e:
            raise VisionEnergyError(
                "Local vision model dependencies aren't installed. "
                "Run: pip install torch open_clip_torch pillow"
            ) from e

        try:
            # Prefer Apple's Metal backend (MPS) when present -- this app's
            # primary deployment is Apple Silicon Macs, where torch never
            # reports CUDA, so the old "cuda else cpu" pick silently ran
            # every frame on the CPU. getattr() guards torch builds old
            # enough to predate torch.backends.mps entirely.
            if getattr(torch.backends, "mps", None) is not None \
                    and torch.backends.mps.is_available():
                _device = "mps"
            elif torch.cuda.is_available():
                _device = "cuda"
            else:
                _device = "cpu"
            model, _, preprocess = open_clip.create_model_and_transforms(
                MODEL_NAME, pretrained=PRETRAINED)
            model.eval()
            model.to(_device)
            tokenizer = open_clip.get_tokenizer(MODEL_NAME)

            with torch.no_grad():
                pos_tok = tokenizer(POSITIVE_PROMPTS).to(_device)
                neg_tok = tokenizer(NEGATIVE_PROMPTS).to(_device)
                pos_feat = model.encode_text(pos_tok)
                neg_feat = model.encode_text(neg_tok)
                pos_feat = pos_feat / pos_feat.norm(dim=-1, keepdim=True)
                neg_feat = neg_feat / neg_feat.norm(dim=-1, keepdim=True)
                pos_mean = pos_feat.mean(dim=0, keepdim=True)
                neg_mean = neg_feat.mean(dim=0, keepdim=True)
                pos_mean = pos_mean / pos_mean.norm(dim=-1, keepdim=True)
                neg_mean = neg_mean / neg_mean.norm(dim=-1, keepdim=True)
        except Exception as e:
            raise VisionEnergyError(f"Failed to load local vision model: {e}") from e

        _model = model
        _preprocess = preprocess
        _text_features = (pos_mean, neg_mean)


def score_frames_energy(pil_images: Sequence) -> List[float]:
    """
    Score a sequence of PIL RGB images for "exciting / high energy"
    content, batched: one model forward pass per BATCH_SIZE chunk instead
    of one per frame. Per-call dispatch overhead dominated the old
    frame-at-a-time path, so batching is the whole performance win --
    the math per image is identical to score_frame_energy (same
    preprocess, same prompt features, same softmax), so the returned
    scores match the single-frame path up to floating-point association
    noise from the batched matmul.

    Returns one float in [0, 100] per input image, in input order.
    Raises VisionEnergyError if the optional dependencies aren't
    installed or the model fails to load.
    """
    _ensure_loaded()
    import torch

    if not pil_images:
        return []

    pos_feat, neg_feat = _text_features
    scores: List[float] = []
    with torch.no_grad():
        # Chunked rather than one giant stack so callers can hand us an
        # arbitrarily long list without memory scaling with clip length.
        for start in range(0, len(pil_images), BATCH_SIZE):
            chunk = pil_images[start:start + BATCH_SIZE]
            batch = torch.stack([_preprocess(img) for img in chunk]).to(_device)
            img_feat = _model.encode_image(batch)
            img_feat = img_feat / img_feat.norm(dim=-1, keepdim=True)
            # (N, 1) similarities against the mean pos/neg prompt
            # embeddings -- the single-frame path's scalars, N rows at
            # a time. Pull back to numpy once per chunk (not per image)
            # to keep device->host syncs off the per-frame path.
            sim_pos = (img_feat @ pos_feat.T).squeeze(1).float().cpu().numpy()
            sim_neg = (img_feat @ neg_feat.T).squeeze(1).float().cpu().numpy()
            exp_pos = np.exp(sim_pos / _TEMPERATURE)
            exp_neg = np.exp(sim_neg / _TEMPERATURE)
            prob_high_energy = exp_pos / (exp_pos + exp_neg)
            scores.extend(float(p * 100.0) for p in prob_high_energy)
    return scores


def score_frame_energy(frame_bgr: np.ndarray) -> float:
    """
    Score a single OpenCV BGR frame for "exciting / high energy" content.
    Returns a float in [0, 100]. Raises VisionEnergyError if the optional
    dependencies aren't installed or the model fails to load.

    Kept for backward compatibility with existing callers; it's now just
    a batch of one through score_frames_energy so both paths share a
    single implementation.
    """
    from PIL import Image

    rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    return score_frames_energy([Image.fromarray(rgb)])[0]


def availability_message() -> Optional[str]:
    """None if ready to use; otherwise a short human-readable reason."""
    if is_available():
        return None
    return ("Optional local vision model not installed. "
            "Run: pip install torch open_clip_torch pillow")
