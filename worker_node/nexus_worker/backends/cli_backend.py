import asyncio
from typing import Any


async def infer(command: str, params: dict[str, Any]) -> dict[str, Any]:
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return {
        "output": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
        "returncode": proc.returncode,
        "usage": {},
    }
