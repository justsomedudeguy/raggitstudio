from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any


def capture_monitor(output_dir: Path, monitor_index: int = 1) -> dict[str, Any]:
    import mss
    from PIL import Image

    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = output_dir / f"screenshot-{timestamp}-monitor-{monitor_index}.png"
    with mss.mss() as screenshotter:
        monitors = screenshotter.monitors
        if monitor_index < 0 or monitor_index >= len(monitors):
            raise ValueError(f"Monitor index {monitor_index} is not available.")
        monitor = monitors[monitor_index]
        raw = screenshotter.grab(monitor)
        image = Image.frombytes("RGB", raw.size, raw.rgb)
        image.save(path)
    return {
        "path": str(path),
        "monitor_index": monitor_index,
        "width": monitor["width"],
        "height": monitor["height"],
        "captured_at": timestamp,
    }

