import csv
import os
from typing import Any, Dict, List

from .base_exporter import BaseExporter


class CSVExporter(BaseExporter):
    DEFAULT_FILENAME = "greenkube-report.csv"

    def export(self, data: List[Dict[str, Any]], path: str | None = None) -> str:
        """Export data to CSV file. Returns path written.

        Data is expected to be a list of dict-like records. If empty, an empty
        file (or header-free) will be created.
        """
        out_path = path or self.DEFAULT_FILENAME
        # Ensure we have a list
        rows = list(data or [])
        # If no rows, just create an empty file
        if not rows:
            open(out_path, "w", encoding="utf-8").close()
            return out_path

        # Collect headers from union of keys to keep stable order
        headers = []
        for r in rows:
            for k in r.keys():
                if k not in headers:
                    headers.append(k)

        # Ensure parent directory exists
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

        with open(out_path, "w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=headers)
            writer.writeheader()
            for r in rows:
                sanitized_row = {k: self._sanitize_cell(v) for k, v in r.items()}
                writer.writerow(sanitized_row)

        return out_path

    def _sanitize_cell(self, value: Any) -> Any:
        """
        Sanitize value to prevent CSV formula injection.
        If the value is a string starting with =, +, -, or @, prefix it with a single quote.
        """
        if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
            return f"'{value}"
        return value
