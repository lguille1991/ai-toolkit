"""Shared utilities for eval-agent-md scripts."""
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import cast

PROMPTS_DIR = Path(__file__).parent / "prompts"


def stable_cache_key(*parts: object) -> str:
    """Build a stable SHA-256 cache key from structured inputs."""
    payload = json.dumps(list(parts), ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def read_json_cache(path: Path) -> dict | list | None:
    """Read JSON from a cache file, returning None on cache miss or parse failure."""
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def write_json_cache(path: Path, payload: dict | list) -> None:
    """Atomically write JSON cache content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2))
    tmp_path.replace(path)


def file_sha256(path: Path) -> str:
    """Hash a file's current contents."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def strip_markdown_fences(text: str) -> str:
    """Remove markdown code fences from LLM output."""
    text = text.strip()
    if text.startswith("```"):
        text = "\n".join(text.split("\n")[1:])
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    return text


def _extract_text_from_json_result(raw_json: str) -> str:
    """Extract text from claude -p --output-format json response.

    The JSON result object has a 'result' field that should contain the text.
    In some Claude Code versions (e.g. 2.1.83), 'result' may be empty even
    though the model produced content. Fall back to extracting from the
    stream-json assistant message content if needed.
    """
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError:
        return raw_json.strip()

    # Primary: use the result field
    result_text = data.get("result", "")
    if result_text:
        return result_text.strip()

    # Fallback not available in non-stream JSON — return whatever we got
    return result_text


def claude_pipe(
    prompt: str,
    *,
    model: str | None = None,
    system_prompt: str | None = None,
    system_file: Path | None = None,
    timeout: int = 300,
) -> str:
    """Call `claude -p` and return the model's text response.

    Provide either system_prompt (string, written to temp file) or
    system_file (path used directly). If both given, system_file wins.

    Uses --output-format stream-json --verbose to reliably extract content
    from assistant messages, working around a bug in some Claude Code versions
    where --output-format text/json returns an empty 'result' field.
    """
    tmp_file = None
    if system_prompt and not system_file:
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False)
        tmp.write(system_prompt)
        tmp.close()
        system_file = Path(tmp.name)
        tmp_file = system_file

    cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose"]
    if model:
        cmd.extend(["--model", model])
    if system_file:
        cmd.extend(["--system-prompt-file", str(system_file)])

    try:
        result = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True, timeout=timeout,
        )
    finally:
        if tmp_file:
            tmp_file.unlink(missing_ok=True)

    if result.returncode != 0:
        raise RuntimeError(
            f"claude -p failed (rc={result.returncode}):\n"
            f"  stderr: {result.stderr[:500]}\n"
            f"  stdout: {result.stdout[:500]}"
        )

    # Parse stream-json: extract text from assistant messages
    text_parts: list[str] = []
    result_text = ""
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("type") == "assistant":
            content = msg.get("message", {}).get("content", [])
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block["text"])
        elif msg.get("type") == "result":
            result_text = msg.get("result", "")

    # Prefer assistant message content; fall back to result field
    extracted = "\n".join(text_parts) if text_parts else result_text
    return extracted.strip()


def load_prompt(name: str) -> str:
    """Load a prompt template from the prompts/ directory."""
    return (PROMPTS_DIR / name).read_text().strip()


def parse_json_response(text: str, expect_type: type = list) -> dict | list:
    """Parse JSON from LLM response with fallback extraction.

    Tries direct parse first, then searches for the outermost [...] or {...}.
    """
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        if expect_type is list:
            start, end = text.find("["), text.rfind("]") + 1
        else:
            start, end = text.find("{"), text.rfind("}") + 1
        if start >= 0 and end > start:
            result = json.loads(text[start:end])
        else:
            print(f"Failed to parse JSON from response:\n{text[:500]}", file=sys.stderr)
            sys.exit(1)

    if not isinstance(result, expect_type):
        print(f"Expected {expect_type.__name__}, got {type(result).__name__}", file=sys.stderr)
        sys.exit(1)

    return cast(dict | list, result)
