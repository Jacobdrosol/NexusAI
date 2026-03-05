from typing import Any, Dict, List


def _vram_required_gb(model_name: str) -> float:
    name = model_name.lower()
    if "70b" in name:
        return 40.0
    if "34b" in name:
        return 24.0
    if "13b" in name:
        return 10.0
    if "8b" in name:
        return 6.0
    if "7b" in name:
        return 5.0
    if "3b" in name:
        return 2.5
    return 8.0


def compatibility_for_models(models: List[str], hardware_profile: Dict[str, Any]) -> List[Dict[str, Any]]:
    gpus = hardware_profile.get("gpus") or []
    max_vram = 0.0
    for g in gpus:
        total = float(g.get("memory_total_bytes") or 0.0) / (1024**3)
        max_vram = max(max_vram, total)

    out = []
    for model in models:
        req = _vram_required_gb(model)
        if max_vram <= 0:
            fit = "cpu_only"
            note = "No GPU detected; expect slower throughput."
        elif max_vram >= req:
            fit = "fits_gpu"
            note = "Model should fit in available GPU VRAM."
        elif max_vram >= req * 0.6:
            fit = "partial_offload"
            note = "Likely needs CPU offload/quantization."
        else:
            fit = "too_large"
            note = "Model likely too large for available VRAM."
        out.append(
            {
                "model": model,
                "required_vram_gb": round(req, 2),
                "max_gpu_vram_gb": round(max_vram, 2),
                "fit": fit,
                "note": note,
            }
        )
    return out


def estimate_eta_seconds(token_count: int, model_name: str, hardware_profile: Dict[str, Any]) -> float:
    # Very rough estimator for scheduling hints.
    base_tps = 10.0
    name = model_name.lower()
    if "70b" in name:
        base_tps = 2.0
    elif "34b" in name:
        base_tps = 3.5
    elif "13b" in name:
        base_tps = 7.0
    elif "8b" in name or "7b" in name:
        base_tps = 12.0
    elif "3b" in name:
        base_tps = 20.0

    gpus = hardware_profile.get("gpus") or []
    if not gpus:
        base_tps *= 0.35

    logical_cores = ((hardware_profile.get("cpu") or {}).get("logical_cores") or 4)
    base_tps *= max(0.8, min(float(logical_cores) / 8.0, 2.5))
    return max(1.0, float(token_count) / max(base_tps, 0.1))

