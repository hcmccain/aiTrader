"""Multi-provider adapter for AI model APIs.

Normalizes Anthropic, OpenAI, and Google Gemini into a common interface
so the trading loop in trader.py stays provider-agnostic.
"""

import json
import logging
from dataclasses import dataclass, field

from config import ANTHROPIC_API_KEY, OPENAI_API_KEY, GOOGLE_API_KEY

logger = logging.getLogger(__name__)


def get_provider(model: str) -> str:
    if model.startswith("claude-"):
        return "anthropic"
    elif model.startswith("gpt-") or model.startswith("o3"):
        return "openai"
    elif model.startswith("gemini-"):
        return "google"
    return "anthropic"


def create_client(provider: str):
    if provider == "anthropic":
        import anthropic
        if not ANTHROPIC_API_KEY:
            raise ValueError("ANTHROPIC_API_KEY not configured")
        return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    elif provider == "openai":
        from openai import OpenAI
        if not OPENAI_API_KEY:
            raise ValueError("OPENAI_API_KEY not configured")
        return OpenAI(api_key=OPENAI_API_KEY)

    elif provider == "google":
        from google import genai
        if not GOOGLE_API_KEY:
            raise ValueError("GOOGLE_API_KEY not configured")
        return genai.Client(api_key=GOOGLE_API_KEY)

    raise ValueError(f"Unknown provider: {provider}")


def convert_tools(tools: list, provider: str) -> list:
    """Convert Anthropic-format tool definitions to the target provider's format."""
    if provider == "anthropic":
        return tools

    elif provider == "openai":
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["input_schema"],
                },
            }
            for t in tools
        ]

    elif provider == "google":
        return tools

    return tools


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class ModelResponse:
    text_parts: list[str] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    is_done: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    _raw: object = field(default=None, repr=False)


def call_model(
    client, provider: str, model: str, system_prompt: str,
    tools: list, messages: list,
) -> ModelResponse:
    """Make a single API call and return a normalized ModelResponse."""
    result = ModelResponse()

    if provider == "anthropic":
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            system=system_prompt,
            tools=tools,
            messages=messages,
        )
        result.input_tokens = response.usage.input_tokens
        result.output_tokens = response.usage.output_tokens
        result.is_done = response.stop_reason == "end_turn"
        result._raw = response.content

        for block in response.content:
            if block.type == "text":
                result.text_parts.append(block.text)
            elif block.type == "tool_use":
                result.tool_calls.append(ToolCall(
                    id=block.id, name=block.name, input=block.input,
                ))

    elif provider == "openai":
        oai_messages = [{"role": "system", "content": system_prompt}]
        for msg in messages:
            oai_messages.append(_convert_message_to_openai(msg))

        response = client.chat.completions.create(
            model=model,
            max_tokens=4096,
            messages=oai_messages,
            tools=tools if tools else None,
        )
        choice = response.choices[0]
        result.input_tokens = response.usage.prompt_tokens
        result.output_tokens = response.usage.completion_tokens
        result.is_done = choice.finish_reason == "stop"
        result._raw = choice.message

        if choice.message.content:
            result.text_parts.append(choice.message.content)

        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                result.tool_calls.append(ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    input=json.loads(tc.function.arguments),
                ))

    elif provider == "google":
        from google.genai import types

        gemini_contents = _build_gemini_contents(messages)
        func_decls = _build_gemini_tools(tools)

        response = client.models.generate_content(
            model=model,
            contents=gemini_contents,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                tools=[types.Tool(function_declarations=func_decls)] if func_decls else None,
                max_output_tokens=4096,
            ),
        )

        if response.usage_metadata:
            result.input_tokens = response.usage_metadata.prompt_token_count or 0
            result.output_tokens = response.usage_metadata.candidates_token_count or 0

        result._raw = response

        has_tool_calls = False
        if response.candidates and response.candidates[0].content:
            for part in response.candidates[0].content.parts:
                if part.text:
                    result.text_parts.append(part.text)
                elif part.function_call:
                    has_tool_calls = True
                    fc = part.function_call
                    result.tool_calls.append(ToolCall(
                        id=fc.name,
                        name=fc.name,
                        input=dict(fc.args) if fc.args else {},
                    ))

        result.is_done = not has_tool_calls and len(result.text_parts) > 0

    return result


def append_assistant(messages: list, provider: str, response: ModelResponse):
    """Append the model's response to the conversation history."""
    if provider == "anthropic":
        messages.append({"role": "assistant", "content": response._raw})

    elif provider == "openai":
        messages.append({"role": "assistant", "_openai_msg": response._raw})

    elif provider == "google":
        messages.append({"role": "assistant", "_gemini_response": response._raw})


def append_tool_results(
    messages: list, provider: str,
    tool_results: list[dict],
):
    """Append tool results to the conversation. Each item has 'id', 'name', 'content'."""
    if provider == "anthropic":
        messages.append({
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": tr["id"], "content": tr["content"]}
                for tr in tool_results
            ],
        })

    elif provider == "openai":
        for tr in tool_results:
            messages.append({
                "role": "tool",
                "tool_call_id": tr["id"],
                "content": tr["content"],
            })

    elif provider == "google":
        from google.genai import types
        parts = []
        for tr in tool_results:
            try:
                response_data = json.loads(tr["content"])
            except (json.JSONDecodeError, TypeError):
                response_data = {"result": tr["content"]}
            parts.append(types.Part(function_response=types.FunctionResponse(
                name=tr["name"],
                response=response_data,
            )))
        messages.append({"role": "user", "_gemini_parts": parts})


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _convert_message_to_openai(msg: dict) -> dict:
    """Convert a normalized message dict into OpenAI's format."""
    if "_openai_msg" in msg:
        return msg["_openai_msg"]

    role = msg.get("role", "user")

    if role == "tool":
        return msg

    content = msg.get("content", "")

    if isinstance(content, str):
        return {"role": role, "content": content}

    if isinstance(content, list):
        text_parts = []
        tool_calls_out = []
        tool_results_out = []

        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "tool_result":
                    tool_results_out.append({
                        "role": "tool",
                        "tool_call_id": item["tool_use_id"],
                        "content": item.get("content", ""),
                    })
                elif item.get("type") == "text":
                    text_parts.append(item.get("text", ""))
            elif hasattr(item, "type"):
                if item.type == "text":
                    text_parts.append(item.text)
                elif item.type == "tool_use":
                    tool_calls_out.append({
                        "id": item.id,
                        "type": "function",
                        "function": {
                            "name": item.name,
                            "arguments": json.dumps(item.input),
                        },
                    })

        if tool_results_out:
            return tool_results_out[0] if len(tool_results_out) == 1 else tool_results_out

        result = {"role": role}
        if text_parts:
            result["content"] = "\n".join(text_parts)
        if tool_calls_out:
            result["tool_calls"] = tool_calls_out
            if "content" not in result:
                result["content"] = None
        return result

    return {"role": role, "content": str(content)}


def _build_gemini_contents(messages: list) -> list:
    """Convert message history to Gemini's content format."""
    from google.genai import types

    contents = []
    for msg in messages:
        if "_gemini_parts" in msg:
            contents.append(types.Content(
                role="user",
                parts=msg["_gemini_parts"],
            ))
        elif "_gemini_response" in msg:
            resp = msg["_gemini_response"]
            if resp.candidates and resp.candidates[0].content:
                contents.append(resp.candidates[0].content)
        else:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            gemini_role = "model" if role == "assistant" else "user"
            if isinstance(content, str):
                contents.append(types.Content(
                    role=gemini_role,
                    parts=[types.Part(text=content)],
                ))

    return contents


def _build_gemini_tools(tools: list) -> list:
    """Convert Anthropic-format tool definitions to Gemini FunctionDeclarations."""
    from google.genai import types

    decls = []
    for t in tools:
        schema = t.get("input_schema", {})
        props = schema.get("properties", {})
        required = schema.get("required", [])

        gemini_props = {}
        for pname, pdef in props.items():
            gemini_props[pname] = _convert_schema_to_gemini(pdef)

        decls.append(types.FunctionDeclaration(
            name=t["name"],
            description=t["description"],
            parameters=types.Schema(
                type="OBJECT",
                properties=gemini_props,
                required=required,
            ) if gemini_props else None,
        ))
    return decls


def _convert_schema_to_gemini(schema: dict) -> "types.Schema":
    """Convert a JSON Schema property definition to a Gemini Schema object."""
    from google.genai import types

    type_map = {
        "string": "STRING",
        "number": "NUMBER",
        "integer": "INTEGER",
        "boolean": "BOOLEAN",
        "array": "ARRAY",
        "object": "OBJECT",
    }

    schema_type = type_map.get(schema.get("type", "string"), "STRING")

    kwargs = {
        "type": schema_type,
        "description": schema.get("description", ""),
    }

    if "enum" in schema:
        kwargs["enum"] = schema["enum"]

    if schema_type == "ARRAY" and "items" in schema:
        kwargs["items"] = _convert_schema_to_gemini(schema["items"])

    return types.Schema(**kwargs)
