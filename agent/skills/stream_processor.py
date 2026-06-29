"""SSEStreamProcessor：NDJSON 字节流 → 完整行解析（处理跨 chunk 的半个 UTF-8 / 半行）。

复刻自 albert-agent-2 `app/infra/boundary/albert_skill_center/stream_processor.py`。
"""
from __future__ import annotations


class SSEStreamProcessor:
    def __init__(self, max_buffer_size: int = 10 * 1024 * 1024) -> None:
        self.buffer = ""
        self.byte_buffer = b""
        self.max_buffer_size = max_buffer_size

    def process_chunk(self, chunk: bytes) -> list[str]:
        self.byte_buffer += chunk
        decoded = self._safe_decode()
        if not decoded:
            return []
        self.buffer += decoded
        lines = self._extract_complete_lines()
        if len(self.buffer) > self.max_buffer_size:
            # 长时间无换行的超长 buffer：强制作为一行处理，防止内存无限增长
            lines.append(self.buffer)
            self.buffer = ""
        return lines

    def _safe_decode(self) -> str:
        try:
            decoded = self.byte_buffer.decode("utf-8")
            self.byte_buffer = b""
            return decoded
        except UnicodeDecodeError as exc:
            if exc.start > 0:
                valid = self.byte_buffer[:exc.start]
                self.byte_buffer = self.byte_buffer[exc.start:]
                return valid.decode("utf-8")
            return ""

    def _extract_complete_lines(self) -> list[str]:
        lines: list[str] = []
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            lines.append(line)
        return lines

    def finalize(self) -> list[str]:
        if self.byte_buffer:
            self.buffer += self.byte_buffer.decode("utf-8", errors="ignore")
            self.byte_buffer = b""
        out = [line for line in self.buffer.strip().split("\n") if line.strip()] if self.buffer.strip() else []
        self.buffer = ""
        return out
