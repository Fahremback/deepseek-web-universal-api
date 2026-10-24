"""Anthropic Messages request translation for the DeepSeek Web gateway."""
from __future__ import annotations

import json
from typing import Any

from harness_adapter import RequestError


def normalize_anthropic_request(request: dict[str, Any]) -> dict[str, Any]:
    model = request.get("model", "deepseek-web")
    if not isinstance(model, str) or not model:
        raise RequestError("model must be a string")
    if not (model in {"deepseek-web", "deepseek-web-reasoner", "deepseek-chat", "deepseek-reasoner"}
            or model.startswith("claude-")):
        raise RequestError("Unknown Anthropic-compatible model")
    thinking_cfg = request.get("thinking", {})
    thinking = isinstance(thinking_cfg, dict) and thinking_cfg.get("type") == "enabled"
    effective_model = (
        "deepseek-web-reasoner"
        if thinking or "reasoner" in model or "thinking" in model
        else "deepseek-web"
    )
    raw = request.get("messages")
    if not isinstance(raw, list) or not raw:
        raise RequestError("messages must be a nonempty array")
    converted: list[dict[str, Any]] = []
    system = request.get("system")
    if isinstance(system, str) and system:
        converted.append({"role": "system", "content": system})
    elif isinstance(system, list):
        content = "\n".join(
            str(x.get("text", "")) for x in system
            if isinstance(x, dict) and x.get("type") == "text"
        )
        if content:
            converted.append({"role": "system", "content": content})
    elif system is not None:
        raise RequestError("system must be text or text blocks")
    for msg in raw:
        if not isinstance(msg, dict) or msg.get("role") not in {"user", "assistant"}:
            raise RequestError("Anthropic message role must be user or assistant")
        role = msg["role"]
        parts = msg.get("content", "")
        if isinstance(parts, str):
            converted.append({"role": role, "content": parts})
            continue
        if not isinstance(parts, list):
            raise RequestError("Anthropic message content must be text or array")
        text_parts = []
        calls = []
        deferred_tool_results = []
        for block in parts:
            if not isinstance(block, dict):
                raise RequestError("Invalid Anthropic content block")
            kind = block.get("type")
            if kind == "text":
                text_parts.append({"type": "text", "text": str(block.get("text", ""))})
            elif kind == "image":
                source = block.get("source", {})
                if not isinstance(source, dict) or source.get("type") != "base64":
                    raise RequestError("Anthropic image source must be base64")
                text_parts.append({"type": "image_url", "image_url": {
                    "url": f"data:{source.get('media_type', 'image/png')};base64,{source.get('data', '')}",
                }})
            elif kind == "document":
                source = block.get("source", {})
                if not isinstance(source, dict) or source.get("type") != "base64":
                    raise RequestError("Anthropic document source must be base64")
                text_parts.append({"type": "file", "file": {
                    "filename": str(block.get("title") or "document.pdf"),
                    "file_data": f"data:{source.get('media_type', 'application/pdf')};base64,{source.get('data', '')}",
                }})
            elif kind == "tool_use" and role == "assistant":
                calls.append({
                    "id": block.get("id"), "type": "function",
                    "function": {"name": block.get("name"),
                                 "arguments": json.dumps(block.get("input", {}), ensure_ascii=False)},
                })
            elif kind == "tool_result" and role == "user":
                result = block.get("content", "")
                if isinstance(result, list):
                    result = "\n".join(str(x.get("text", "")) for x in result
                                       if isinstance(x, dict) and x.get("type") == "text")
                deferred_tool_results.append({
                    "role": "tool", "tool_call_id": block.get("tool_use_id"), "content": str(result),
                })
            elif kind in {"thinking", "redacted_thinking"}:
                continue
            else:
                raise RequestError(f"Unsupported Anthropic block type: {kind}")
        if text_parts or calls:
            converted.append({"role": role, "content": text_parts if text_parts else None,
                              **({"tool_calls": calls} if calls else {})})
        converted.extend(deferred_tool_results)
    tools = []
    for tool in request.get("tools", []) or []:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            raise RequestError("Invalid Anthropic tool")
        tools.append({"type": "function", "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
        }})
    raw_choice = request.get("tool_choice", {"type": "auto"})
    typ = raw_choice.get("type", "auto") if isinstance(raw_choice, dict) else raw_choice
    if typ == "any":
        tool_choice = "required"
    elif typ == "tool" and isinstance(raw_choice, dict):
        tool_choice = {"type": "function", "function": {"name": raw_choice.get("name")}}
    elif typ in {"auto", "none"}:
        tool_choice = typ
    else:
        raise RequestError("Unsupported Anthropic tool_choice")
    return {"messages": converted, "model": effective_model, "tools": tools,
            "tool_choice": tool_choice, "requested_model": model}
