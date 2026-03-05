import platform
from typing import Any, Dict, List

try:
    import psutil
except Exception:  # pragma: no cover - optional dependency fallback
    psutil = None

try:
    import pynvml

    _NVML_OK = True
except Exception:  # pragma: no cover - optional dependency fallback
    pynvml = None
    _NVML_OK = False


def _cpu_profile() -> Dict[str, Any]:
    if psutil is None:
        return {
            "physical_cores": None,
            "logical_cores": None,
            "max_freq_mhz": None,
            "usage_percent": None,
            "arch": platform.machine(),
        }
    freq = psutil.cpu_freq()
    return {
        "physical_cores": psutil.cpu_count(logical=False),
        "logical_cores": psutil.cpu_count(logical=True),
        "max_freq_mhz": float(freq.max) if freq else None,
        "usage_percent": float(psutil.cpu_percent(interval=None)),
        "arch": platform.machine(),
    }


def _memory_profile() -> Dict[str, Any]:
    if psutil is None:
        return {"total_bytes": None, "available_bytes": None, "used_bytes": None, "usage_percent": None}
    vm = psutil.virtual_memory()
    return {
        "total_bytes": int(vm.total),
        "available_bytes": int(vm.available),
        "used_bytes": int(vm.used),
        "usage_percent": float(vm.percent),
    }


def _gpu_profile() -> List[Dict[str, Any]]:
    if not _NVML_OK:
        return []
    out: List[Dict[str, Any]] = []
    try:
        pynvml.nvmlInit()
        count = pynvml.nvmlDeviceGetCount()
        for i in range(count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            util = pynvml.nvmlDeviceGetUtilizationRates(handle)
            name = pynvml.nvmlDeviceGetName(handle)
            out.append(
                {
                    "id": f"GPU-{i}",
                    "name": name if isinstance(name, str) else name.decode("utf-8", errors="ignore"),
                    "memory_total_bytes": int(mem.total),
                    "memory_used_bytes": int(mem.used),
                    "utilization_percent": float(util.gpu),
                }
            )
    except Exception:
        return []
    return out


def detect_hardware_profile() -> Dict[str, Any]:
    return {
        "cpu": _cpu_profile(),
        "memory": _memory_profile(),
        "gpus": _gpu_profile(),
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "python_version": platform.python_version(),
        },
    }

