"""Bounded model catalogs and conservative admission estimates in GiB."""

_RPC_MODELS: dict[str, float] = {
    "qwen3.8:27b-q4_K_M": 19.0,
    "qwen3.6:27b": 17.0,
    "qwen3:32b": 20.0,
    "qwen3:30b": 19.0,
    "qwen3:30b-a3b": 19.0,
}

_RPC_DEFAULT_CONTEXT = {"qwen3:30b-a3b": 32768, "qwen3.8:27b-q4_K_M": 32768,
                        "devstral-small-2:24b": 16384}

# Prefer Ollama on a sufficiently large single GPU; otherwise form a native pair.
RPC_FALLBACK_MODELS = {"devstral-small-2:24b": 17.0}

# Resolve the upstream model ID to an explicit artifact supported by our runtime.
MODEL_ALIASES = {
    "Qwen/Qwen3.8-27B": "qwen3.8:27b-q4_K_M",
    "qwen3.8:27b": "qwen3.8:27b-q4_K_M",
}

_ON_DEMAND_MODELS: dict[str, float] = {
    "qwen3:8b": 5.2,
    "gemma4:12b": 8.0,
    "qwen3:14b": 9.3,
    "devstral-small-2:24b": 15.0,
}

IMAGE_MODELS = [
    {
        "id": "sdxl",
        "name": "Stable Diffusion XL",
        "vram_gb": 8,
        "hf_repo": "stabilityai/stable-diffusion-xl-base-1.0",
        "sizes": ["1024x1024", "1152x896", "896x1152", "1216x832", "832x1216"],
        "note": "Native resolution is 1024×1024. Smaller sizes produce significantly lower quality.",
    },
]

VIDEO_MODELS = [
    {
        "id": "ltx-video",
        "name": "LTX-Video",
        "vram_gb": 8,
        "hf_repo": "Lightricks/LTX-Video",
        "duration_min": 1,
        "duration_max": 10,
        "duration_default": 5,
        "sizes": ["704x480", "768x512", "576x320", "480x704", "512x768"],
        "supports_neg": True,
        "fps": 8,
        "guidance_scale": 3.5,
        "num_inference_steps": 50,
        "default_neg": "worst quality, inconsistent motion, blurry, jittery, distorted",
        "note": "Use detailed, film-style prompts (describe colors, lighting, camera motion). Vague prompts produce incoherent video.",
    },
    {
        "id": "cogvideox-2b",
        "name": "CogVideoX-2B",
        "vram_gb": 8,
        "hf_repo": "THUDM/CogVideoX-2b",
        "duration_min": 6,
        "duration_max": 6,
        "duration_default": 6,
        "sizes": ["720x480"],
        "supports_neg": False,
        "fps": 8,
        "guidance_scale": 6.0,
        "num_inference_steps": 50,
        "note": "Fixed 6s at 720×480; duration and resolution are not adjustable.",
    },
    {
        "id": "wan-2.1-t2v-1.3b",
        "name": "Wan 2.1 T2V 1.3B",
        "vram_gb": 8,
        "hf_repo": "Wan-AI/Wan2.1-T2V-1.3B-Diffusers",
        "duration_min": 1,
        "duration_max": 10,
        "duration_default": 2,
        "sizes": ["480x480", "480x832", "832x480", "512x512", "576x1024", "1024x576"],
        "supports_neg": True,
        "fps": 16,
        "guidance_scale": 5.0,
        "num_inference_steps": 20,
        "default_neg": "worst quality, blurry, jittery, distorted, low resolution, static, no motion",
    },
]
