import json
from typing import List, Dict, Any
from .base_exporter import BaseExporter


class JSONExporter(BaseExporter):
    DEFAULT_FILENAME = "greenkube-report.json"

    def export(self, data: List[Dict[str, Any]], path: str | None = None) -> str:
        out_path = path or self.DEFAULT_FILENAME
        rows = list(data or [])
        # Ensure parent directory exists
        import os
        os.makedirs(os.path.dirname(out_path) or '.', exist_ok=True)

        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=2)
        return out_path
