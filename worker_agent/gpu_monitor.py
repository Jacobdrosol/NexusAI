import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

try:
    import pynvml

    _NVML_AVAILABLE = True
except ImportError:
    _NVML_AVAILABLE = False
    logger.debug("pynvml not available; GPU monitoring disabled")


def get_gpu_info() -> List[Dict[str, Any]]:
    if not _NVML_AVAILABLE:
        return []
    try:
        pynvml.nvmlInit()
        count = pynvml.nvmlDeviceGetCount()
        gpus = []
        for i in range(count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
            name = pynvml.nvmlDeviceGetName(handle)
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            gpus.append(
                {
                    "id": f"GPU-{i}",
                    "name": name if isinstance(name, str) else name.decode(),
                    "memory_total": mem.total,
                    "memory_used": mem.used,
                }
            )
        return gpus
    except Exception as e:
        logger.warning("Failed to get GPU info: %s", e)
        return []
