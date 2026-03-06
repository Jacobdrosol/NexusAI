import os

import uvicorn

from nexus_worker.agent import WORKER_CONFIG_PATH
from nexus_worker.config_loader import ConfigLoader


def main() -> None:
    cfg = {}
    try:
        cfg = ConfigLoader.load_yaml(WORKER_CONFIG_PATH)
    except Exception:
        cfg = {}
    uvicorn.run(
        "nexus_worker.agent:app",
        host=cfg.get("host", "0.0.0.0"),
        port=int(cfg.get("port", int(os.environ.get("NEXUS_WORKER_PORT", "8010")))),
        reload=False,
    )


if __name__ == "__main__":
    main()
