"""User-facing answers collected from verified subtasks."""

import json

from pydantic import BaseModel, JsonValue

from ai_desktop_agent.privacy import redact


class TaskOutput(BaseModel):
    summary: str
    data: dict[str, JsonValue] | None = None


def _clean_json(value: JsonValue, depth: int = 0) -> JsonValue:
    if depth > 16:
        raise ValueError("結果の入れ子が深すぎます")
    if isinstance(value, dict):
        return {redact(key): _clean_json(item, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return [_clean_json(item, depth + 1) for item in value]
    return redact(value) if isinstance(value, str) else value


def parse_task_output(summary: object, data_json: object) -> TaskOutput | None:
    if not isinstance(summary, str) or not summary.strip():
        return None
    summary = redact(summary.strip())[:2000].encode("utf-8", errors="replace").decode()
    data = None
    if isinstance(data_json, str) and 0 < len(data_json) <= 8000:
        try:
            parsed = json.loads(data_json)
            if isinstance(parsed, dict):
                cleaned = _clean_json(parsed)
                encoded = json.dumps(
                    cleaned, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                )
                if len(encoded) <= 8000:
                    encoded.encode("utf-8")
                    data = cleaned
        except (ValueError, RecursionError):
            pass
    return TaskOutput(summary=summary, data=data)
