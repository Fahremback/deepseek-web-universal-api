"""Translation between OpenAI-style agent messages and DeepSeek Web chat.

DeepSeek Web's private endpoint takes a single prompt, not native role/tool
messages. Role and tool protocols here are prompt-level emulation. Tool calls
are validated and returned to the harness; this process never executes tools.
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
import json
import os
import re
import uuid
from typing import Any


MAX_MESSAGES = 256
MAX_PROMPT_CHARS = 200_000
CODEX_MAX_PROMPT_CHARS = 1_000_000
MAX_ATTACHMENTS = 12
MAX_FILE_BYTES = 12 * 1024 * 1024
MAX_TOTAL_FILE_BYTES = 32 * 1024 * 1024
CALL_OPEN = "<deepseek_tool_calls>"
CALL_CLOSE = "</deepseek_tool_calls>"
MODEL_IDS = ("deepseek-web", "deepseek-web-reasoner", "deepseek-chat", "deepseek-reasoner")


class RequestError(ValueError):
    """Bad client request, suitable for an OpenAI-shaped HTTP 400."""


class ToolProtocolError(RuntimeError):
    """The remote model did not produce the required tool-call format."""


@dataclass
class PreparedRequest:
    prompt: str
    attachments: list[tuple[str, bytes]] = field(default_factory=list)
    tools: list[dict[str, Any]] = field(default_factory=list)
    tool_choice: Any = "none"
    thinking: bool = False
    model: str = "deepseek-web"
    output_format: dict[str, Any] | None = None


def select_model(model: str | None) -> tuple[str, bool]:
    model = model or "deepseek-web"
    if model not in MODEL_IDS:
        raise RequestError(
            f"Unknown model {model!r}. Available: {', '.join(MODEL_IDS)}"
        )
    return model, model.endswith("reasoner")


def normalize_tools(tools: Any) -> list[dict[str, Any]]:
    if tools is None:
        return []
    if not isinstance(tools, list):
        raise RequestError("tools must be an array")
    result = []
    names = set()
    for raw in tools:
        if not isinstance(raw, dict):
            raise RequestError("Invalid tool definition")
        wire_type = raw.get("type", "function")
        if wire_type not in {"function", "custom"}:
            # Hosted tools (for example web_search) are executed by their provider,
            # not by this local gateway; never offer them as executable functions.
            continue
        data = raw.get("function", raw)
        if not isinstance(data, dict):
            raise RequestError("Invalid function tool")
        name = data.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[\w.-]{1,64}", name):
            raise RequestError("Function tools need a valid name")
        if name in names:
            raise RequestError(f"Duplicate function name: {name}")
        names.add(name)
        params = (data.get("parameters", {"type": "object", "properties": {}})
                  if wire_type == "function" else
                  {"type": "object", "properties": {"input": {"type": "string"}},
                   "required": ["input"], "additionalProperties": False})
        if not isinstance(params, dict):
            raise RequestError(f"Invalid parameters for {name}")
        result.append({
            "name": name,
            "description": str(data.get("description", ""))[:2000],
            "parameters": params,
            "wire_type": wire_type,
        })
    return result


def normalize_tool_choice(value: Any, tools: list[dict[str, Any]]) -> Any:
    if value is None:
        value = "auto" if tools else "none"
    if isinstance(value, str):
        if value not in {"auto", "none", "required"}:
            raise RequestError("tool_choice must be auto, none, required, or a function")
        if value == "required" and not tools:
            raise RequestError("tool_choice=required needs tools")
        return value
    if isinstance(value, dict) and value.get("type") in {"function", "custom"}:
        data = value.get("function", value)
        name = data.get("name") if isinstance(data, dict) else None
        if not isinstance(name, str) or name not in {t["name"] for t in tools}:
            raise RequestError("tool_choice references an unknown function")
        return {"name": name}
    raise RequestError("Unsupported tool_choice")


def decode_file_data(uri: str) -> tuple[str, bytes]:
    if not isinstance(uri, str):
        raise RequestError("Attachment must use a base64 data URI")
    m = re.fullmatch(r"data:([^;,]+)?(;[^,]*)?,(.*)", uri, flags=re.S)
    if not m or ";base64" not in (m.group(2) or "").lower():
        raise RequestError("Only base64 data URIs are supported for inline media")
    mime = (m.group(1) or "application/octet-stream").lower()
    if len(m.group(3)) > MAX_FILE_BYTES * 4 // 3 + 8:
        raise RequestError("Inline attachment is too large")
    try:
        data = base64.b64decode(m.group(3), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RequestError("Invalid base64 attachment") from exc
    if not data or len(data) > MAX_FILE_BYTES:
        raise RequestError("Attachment is empty or exceeds 12 MiB")
    ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
           "image/gif": ".gif", "application/pdf": ".pdf", "text/plain": ".txt"}.get(mime, ".bin")
    return ext, data


def _flatten_content(content: Any, attachments: list[tuple[str, bytes]]) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise RequestError("message.content must be a string or content-part array")
    text_parts = []
    for p in content:
        if not isinstance(p, dict):
            raise RequestError("Invalid content part")
        typ = p.get("type")
        if typ in {"text", "input_text", "output_text"}:
            text_parts.append(str(p.get("text", "")))
        elif typ in {"image_url", "input_image"}:
            item = p.get("image_url", p.get("image"))
            url = item.get("url") if isinstance(item, dict) else item
            ext, blob = decode_file_data(url)
            name = f"image_{len(attachments)+1}{ext}"
            attachments.append((name, blob))
            text_parts.append(f"[Attached image: {name}]")
        elif typ in {"file", "input_file"}:
            f = p.get("file", p)
            if not isinstance(f, dict):
                raise RequestError("Invalid file part")
            data_uri = f.get("file_data", f.get("data"))
            ext, blob = decode_file_data(data_uri)
            filename = f.get("filename") or f"file_{len(attachments)+1}{ext}"
            if not isinstance(filename, str) or not filename.strip():
                raise RequestError("Invalid filename")
            filename = re.split(r"[/\\]", filename)[-1]
            attachments.append((filename, blob))
            text_parts.append(f"[Attached file: {filename}]")
        else:
            raise RequestError(f"Unsupported content part type: {typ}")
    return "\n".join(text_parts)


def _validate_messages(messages: Any) -> list[dict[str, Any]]:
    if not isinstance(messages, list) or not messages:
        raise RequestError("messages must be a nonempty array")
    if len(messages) > MAX_MESSAGES:
        raise RequestError("Too many messages")
    out = []
    attachments: list[tuple[str, bytes]] = []
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") not in {
            "system", "developer", "user", "assistant", "tool"
        }:
            raise RequestError("Invalid message role")
        rec = {"role": msg["role"], "content": _flatten_content(msg.get("content"), attachments)}
        if msg.get("name") is not None:
            rec["name"] = str(msg["name"])
        if msg.get("tool_call_id") is not None:
            rec["tool_call_id"] = str(msg["tool_call_id"])
        if msg.get("tool_calls") is not None:
            rec["tool_calls"] = msg["tool_calls"]
        out.append(rec)
    if len(attachments) > MAX_ATTACHMENTS or sum(len(blob) for _, blob in attachments) > MAX_TOTAL_FILE_BYTES:
        raise RequestError("Attachment count or total size exceeded")
    return out, attachments


def prepare_chat(request: dict[str, Any]) -> PreparedRequest:
    model, thinking = select_model(request.get("model"))
    tools = normalize_tools(request.get("tools"))
    choice = normalize_tool_choice(request.get("tool_choice"), tools)
    messages, attachments = _validate_messages(request.get("messages"))
    fmt = request.get("response_format")
    if fmt is not None and not isinstance(fmt, dict):
        raise RequestError("response_format must be an object")
    prompt = _build_prompt(messages, tools, choice, fmt)
    return PreparedRequest(prompt, attachments, tools, choice, thinking, model, fmt)


def _build_prompt(messages: list[dict[str, Any]], tools: list[dict[str, Any]], choice: Any,
                  response_format: dict[str, Any] | None) -> str:
    lines = [
        "You are responding through a structured conversation gateway. "
        "Follow system and developer messages as higher-priority instructions. "
        "The JSON records below are the full conversation, in chronological order. "
        "A tool message is the result of an earlier tool call; use its content to continue. "
        "Respond to the latest request, without role labels or transcript wrappers.",
    ]
    if choice != "none" and tools:
        directive = (
            f"If you need to invoke a function, output ONLY {CALL_OPEN}"
            '{"calls":[{"name":"FUNCTION_NAME","arguments":{"key":"value"}}]}'
            f"{CALL_CLOSE}, with valid JSON, real function names and arguments "
            "matching their JSON schemas. Do not execute functions yourself. "
            "The calling application will execute them and send back tool messages. "
            "Multiple calls may appear in the calls array. "
            "If no function is needed, respond normally."
        )
        if choice == "required":
            directive += " You MUST call at least one function; do not answer directly."
        if isinstance(choice, dict):
            directive += f" You MUST call function {choice['name']}."
        lines += [directive, "FUNCTIONS_JSON=" + json.dumps(tools, ensure_ascii=False, separators=(",", ":"))]
    if response_format:
        kind = response_format.get("type")
        if kind == "json_object":
            lines.append("Your final answer must be a valid JSON object with no Markdown fence.")
        elif kind == "json_schema":
            schema = output_schema(response_format)
            lines.append("Your final answer must satisfy this JSON schema: " +
                         json.dumps(schema, ensure_ascii=False) +
                         ". Output ONLY the JSON object without a Markdown fence.")
    lines.append("MESSAGES_JSONL:")
    lines.extend(json.dumps(m, ensure_ascii=False, separators=(",", ":")) for m in messages)
    prompt = "\n".join(lines)
    # The Codex launcher creates its HTTP gateway in this Python process.
    # Therefore the bridge setting must be evaluated when a request arrives,
    # not just once during module import.
    limit = (CODEX_MAX_PROMPT_CHARS if os.environ.get("DEEPSEEK_CODEX_BRIDGE") == "1"
             else MAX_PROMPT_CHARS)
    if len(prompt) > limit:
        raise RequestError("Prompt exceeds maximum size")
    return prompt


def _basic_schema_check(value: Any, schema: dict[str, Any]) -> bool:
    """Small subset of JSON Schema; unsupported keywords are provider hints."""
    typ = schema.get("type")
    checks = {"object": lambda x: isinstance(x, dict),
              "array": lambda x: isinstance(x, list),
              "string": lambda x: isinstance(x, str),
              "integer": lambda x: isinstance(x, int) and not isinstance(x, bool),
              "number": lambda x: isinstance(x, (float, int)) and not isinstance(x, bool),
              "boolean": lambda x: isinstance(x, bool),
              "null": lambda x: x is None}
    if isinstance(typ, str) and typ in checks and not checks[typ](value):
        return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    if isinstance(value, dict):
        if not all(k in value for k in schema.get("required", [])):
            return False
        props = schema.get("properties", {})
        if isinstance(props, dict):
            for k, v in value.items():
                if k in props and isinstance(props[k], dict) and not _basic_schema_check(v, props[k]):
                    return False
        if schema.get("additionalProperties") is False and any(k not in props for k in value):
            return False
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        return all(_basic_schema_check(item, schema["items"]) for item in value)
    return True


def output_schema(fmt: dict[str, Any]) -> dict[str, Any]:
    """Accept Chat Completions json_schema and Responses text.format variants."""
    spec = fmt.get("json_schema", fmt)
    if not isinstance(spec, dict) or not isinstance(spec.get("schema"), dict):
        raise RequestError("json_schema response format needs a schema object")
    return spec["schema"]


def valid_json_output(text: str, fmt: dict[str, Any] | None) -> bool:
    if not fmt or fmt.get("type") not in {"json_object", "json_schema"}:
        return True
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return False
    if not isinstance(value, dict):
        return False
    return fmt.get("type") != "json_schema" or _basic_schema_check(value, output_schema(fmt))


def parse_tool_calls(text: str, tools: list[dict[str, Any]], choice: Any) -> list[dict[str, Any]] | None:
    if not tools or choice == "none":
        return None
    match = re.search(re.escape(CALL_OPEN) + r"\s*(\{.*?\})\s*" + re.escape(CALL_CLOSE), text, re.S)
    raw = match.group(1) if match else text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", raw)
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        obj = _parse_dsml_tool_calls(text, tools)
    if not isinstance(obj, dict):
        return None
    calls = obj.get("calls", obj.get("tool_calls"))
    if not isinstance(calls, list) or not calls:
        return None
    by_name = {t["name"]: t for t in tools}
    parsed = []
    for call in calls:
        if not isinstance(call, dict):
            return None
        func = call.get("function", call)
        if not isinstance(func, dict):
            return None
        name = func.get("name")
        if name not in by_name or (isinstance(choice, dict) and name != choice["name"]):
            return None
        args = func.get("arguments", {})
        if isinstance(args, str) and by_name[name].get("wire_type") == "custom":
            args = {"input": args}
        elif isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                return None
        if not isinstance(args, dict) or not _basic_schema_check(args, by_name[name]["parameters"]):
            return None
        wire_type = by_name[name].get("wire_type", "function")
        parsed.append({
            "id": "call_" + uuid.uuid4().hex[:24],
            "type": wire_type,
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False, separators=(",", ":"))},
            **({"input": args["input"]} if wire_type == "custom" else {}),
        })
    return parsed


def _parse_dsml_tool_calls(text: str, tools: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Decode the native DeepSeek Web DSML tool syntax into the same call schema."""
    start = re.escape("<｜｜DSML｜｜ invoke")
    end = re.escape("</｜｜DSML｜｜ invoke>")
    pattern = start + r'\s+name="([\w.-]{1,64})"\s*>(.*?)' + end
    params_open = re.escape("<｜｜DSML｜｜ parameter")
    params_end = re.escape("</｜｜DSML｜｜ parameter>")
    params_pattern = params_open + r'\s+name="([\w.-]{1,64})"([^>]*)>(.*?)' + params_end
    calls = []
    by_name = {tool["name"]: tool for tool in tools}
    for match in re.finditer(pattern, text, re.S):
        name, content = match.groups()
        if name not in by_name:
            return None
        arguments: dict[str, Any] = {}
        for param in re.finditer(params_pattern, content, re.S):
            key, attrs, value = param.groups()
            if key in arguments:
                return None
            if 'string="true"' not in attrs:
                try:
                    value = json.loads(value)
                except ValueError:
                    pass
            arguments[key] = value
        if not arguments:
            return None
        if by_name[name].get("wire_type") == "custom":
            # Custom Responses tools consume a string. Preserve a lone raw input,
            # otherwise serialize DeepSeek's named parameters as the tool input.
            tool_input = (arguments["input"] if set(arguments) == {"input"} and
                          isinstance(arguments["input"], str) else
                          json.dumps(arguments, ensure_ascii=False, separators=(",", ":")))
            arguments = {"input": tool_input}
        calls.append({"name": name, "arguments": arguments})
    return {"calls": calls} if calls else None


def normalize_responses_input(data: dict[str, Any], previous: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    raw = data.get("input")
    if isinstance(raw, str):
        messages = [{"role": "user", "content": raw}]
    elif isinstance(raw, list):
        messages = []
        for item in raw:
            if not isinstance(item, dict):
                raise RequestError("Invalid Responses input")
            typ = item.get("type")
            if typ in {"function_call_output", "custom_tool_call_output"}:
                call_id = item.get("call_id")
                if not isinstance(call_id, str) or not call_id.strip():
                    raise RequestError("Tool output must have a nonempty call_id")
                output = item.get("output")
                if not isinstance(output, (str, list)):
                    raise RequestError("Tool output must be text or a content-part array")
                messages.append({"role": "tool", "tool_call_id": call_id,
                                 "content": output})
            elif typ in {"function_call", "custom_tool_call"}:
                call_id, name = item.get("call_id"), item.get("name")
                if not isinstance(call_id, str) or not call_id.strip():
                    raise RequestError("Tool call must have a nonempty call_id")
                if not isinstance(name, str) or not name.strip():
                    raise RequestError("Tool call must have a nonempty name")
                messages.append({"role": "assistant", "content": None, "tool_calls": [{
                    "id": call_id,
                    "type": typ,
                    "function": {"name": name, "arguments": item.get("arguments", item.get("input", ""))},
                }]})
            elif item.get("role") in {"system", "developer", "user", "assistant", "tool"}:
                messages.append({"role": item["role"], "content": item.get("content"),
                                 **({"tool_call_id": item["tool_call_id"]} if "tool_call_id" in item else {})})
            else:
                raise RequestError(f"Unsupported Responses input type: {typ}")
    else:
        raise RequestError("input must be a string or array")
    if previous:
        messages = previous + messages
    if data.get("instructions"):
        messages.insert(0, {"role": "system", "content": str(data["instructions"])})
    return {
        "messages": messages, "model": data.get("model"),
        "tools": data.get("tools", []),
        "tool_choice": data.get("tool_choice"),
        "response_format": data.get("text", {}).get("format") if isinstance(data.get("text"), dict) else None,
    }
