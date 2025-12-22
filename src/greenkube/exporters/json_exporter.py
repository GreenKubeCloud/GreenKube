import json
import os
from typing import Any, Dict, List

import aiofiles

from .base_exporter import BaseExporter


class JSONExporter(BaseExporter):
    DEFAULT_FILENAME = "greenkube-report.json"

    async def export(self, data: List[Dict[str, Any]], path: str | None = None) -> str:
        out_path = path or self.DEFAULT_FILENAME
        rows = list(data or [])
        # Ensure parent directory exists
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

        async with aiofiles.open(out_path, "w", encoding="utf-8") as fh:
            content = json.dumps(rows, ensure_ascii=False, indent=2)
            await fh.write(content)
        return out_path
