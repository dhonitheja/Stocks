"""Shared base for Claude-powered sub-agents (code review + QA testing)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import anthropic
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel

from agent.config import logger

MODEL = "claude-opus-4-7"
THINKING: dict = {"type": "adaptive"}
MAX_TOKENS = 16000
CACHE_THRESHOLD_CHARS = 8000  # cache content blocks larger than this

_console = Console()


class BaseAgent:
    def __init__(self) -> None:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise EnvironmentError("ANTHROPIC_API_KEY not set")
        self.client = anthropic.Anthropic(api_key=api_key)
        self.project_root = Path(__file__).parent.parent.parent.resolve()

    # ── Tool implementations ───────────────────────────────────────────────

    def _tool_read_file(self, file_path: str) -> str:
        path = Path(file_path).resolve()
        if not str(path).startswith(str(self.project_root)):
            return f"ERROR: path traversal denied — {file_path}"
        if not path.exists():
            return f"ERROR: file not found — {file_path}"
        try:
            return path.read_text(encoding="utf-8")
        except Exception as exc:
            return f"ERROR: {exc}"

    def _tool_list_directory(self, directory_path: str, pattern: str = "**/*.py") -> str:
        path = Path(directory_path).resolve()
        if not str(path).startswith(str(self.project_root)):
            return json.dumps({"error": "path traversal denied"})
        if not path.is_dir():
            return json.dumps({"error": f"not a directory: {directory_path}"})
        try:
            entries = []
            for item in sorted(path.glob(pattern)):
                entries.append({
                    "path": str(item.relative_to(self.project_root)),
                    "type": "file" if item.is_file() else "dir",
                    "size_bytes": item.stat().st_size if item.is_file() else 0,
                })
            return json.dumps(entries, indent=2)
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    def _execute_tool(self, name: str, tool_input: dict) -> str:
        if name == "read_file":
            return self._tool_read_file(tool_input["file_path"])
        if name == "list_directory":
            return self._tool_list_directory(
                tool_input["directory_path"],
                tool_input.get("pattern", "**/*.py"),
            )
        return f"ERROR: unknown tool {name}"

    # ── Caching helpers ────────────────────────────────────────────────────

    def _make_text_block(self, text: str, cache: bool = False) -> dict:
        block: dict = {"type": "text", "text": text}
        if cache and len(text) > CACHE_THRESHOLD_CHARS:
            block["cache_control"] = {"type": "ephemeral"}
        return block

    def _make_system_block(self, text: str) -> list[dict]:
        """System prompt always cached."""
        return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]

    # ── Agentic loop ───────────────────────────────────────────────────────

    def _run_agentic_loop(
        self,
        system: list[dict],
        messages: list[dict],
        tools: list[dict],
        title: str = "Agent",
    ) -> str:
        """Run tool-use loop, stream text to terminal. Returns full text output."""
        full_text = ""

        while True:
            with Live(
                Panel(Markdown(full_text or "_thinking…_"), title=title),
                console=_console,
                refresh_per_second=8,
            ) as live:
                response_text = ""
                tool_use_blocks: list[dict] = []

                with self.client.messages.stream(
                    model=MODEL,
                    max_tokens=MAX_TOKENS,
                    thinking=THINKING,
                    system=system,
                    tools=tools,
                    messages=messages,
                ) as stream:
                    for event in stream:
                        if event.type == "content_block_delta":
                            delta = event.delta
                            if delta.type == "text_delta":
                                response_text += delta.text
                                live.update(
                                    Panel(Markdown(full_text + response_text), title=title)
                                )

                    final = stream.get_final_message()

                full_text += response_text

                # Log cache usage
                usage = final.usage
                if hasattr(usage, "cache_read_input_tokens") and usage.cache_read_input_tokens:
                    logger.debug(
                        f"[{title}] cache hit: {usage.cache_read_input_tokens} tokens read from cache"
                    )

                # Collect tool_use blocks
                for block in final.content:
                    if block.type == "tool_use":
                        tool_use_blocks.append(block)

            if final.stop_reason == "end_turn" or not tool_use_blocks:
                break

            # Append assistant turn
            messages.append({"role": "assistant", "content": final.content})

            # Execute tools
            tool_results = []
            for block in tool_use_blocks:
                result = self._execute_tool(block.name, block.input)
                logger.debug(f"[{title}] tool={block.name} input={block.input}")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

            messages.append({"role": "user", "content": tool_results})

        return full_text


READ_FILE_TOOL: dict[str, Any] = {
    "name": "read_file",
    "description": "Read the full text of a source file. Use to inspect code before reviewing.",
    "input_schema": {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Absolute path to the file",
            }
        },
        "required": ["file_path"],
    },
}

LIST_DIRECTORY_TOOL: dict[str, Any] = {
    "name": "list_directory",
    "description": "List files in a directory matching a glob pattern.",
    "input_schema": {
        "type": "object",
        "properties": {
            "directory_path": {
                "type": "string",
                "description": "Absolute path to the directory",
            },
            "pattern": {
                "type": "string",
                "description": "Glob pattern, default '**/*.py'",
            },
        },
        "required": ["directory_path"],
    },
}
