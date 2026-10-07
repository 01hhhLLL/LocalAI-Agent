"""Loopback OpenAI chat-completions adapter for the existing Qwen3 NF4 model.

This exposes only inference. Hermes owns tool execution and passes tool results
back in subsequent chat requests.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))

MODEL_ID = "local-qwen3-8b-nf4"
TOOL_CALL = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)


def normalize_messages(messages: object) -> list[dict]:
    if not isinstance(messages, list) or not messages or len(messages) > 64:
        raise ValueError("messages must be a non-empty list of at most 64 items")
    normalized = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in {
            "system", "developer", "user", "assistant", "tool"
        }:
            raise ValueError("unsupported message role")
        role = message["role"]
        if role == "developer":
            role = "system"
        content = message.get("content")
        if isinstance(content, list):
            if any(not isinstance(part, dict) or part.get("type") != "text" for part in content):
                raise ValueError("local text model accepts text content only")
            content = "\n".join(str(part.get("text", "")) for part in content)
        if content is None and role == "assistant" and message.get("tool_calls"):
            content = ""
        if not isinstance(content, str) or len(content) > 32000:
            raise ValueError("message content must be text up to 32000 characters")
        item = {"role": role, "content": content}
        if role == "tool":
            # OpenAI sends tool_call_id but no name on a tool-result message.
            if isinstance(message.get("name"), str):
                item["name"] = message["name"]
        if role == "assistant" and message.get("tool_calls"):
            for call in message["tool_calls"]:
                function = call.get("function", {})
                if not isinstance(function.get("name"), str):
                    raise ValueError("invalid tool call history")
                arguments = function.get("arguments", "{}")
                if isinstance(arguments, str):
                    arguments = json.loads(arguments)
                item["content"] += ("\n<tool_call>" + json.dumps({
                    "name": function["name"], "arguments": arguments
                }, ensure_ascii=False) + "</tool_call>")
        normalized.append(item)
    return normalized


def parse_output(output: str) -> tuple[str, list[dict]]:
    calls = []
    for match in TOOL_CALL.finditer(output):
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict) or not isinstance(data.get("name"), str):
            continue
        arguments = data.get("arguments", {})
        if not isinstance(arguments, dict):
            continue
        calls.append({
            "id": "call_" + uuid.uuid4().hex[:24],
            "type": "function",
            "function": {"name": data["name"],
                         "arguments": json.dumps(arguments, ensure_ascii=False)},
        })
    return TOOL_CALL.sub("", output).strip(), calls


class LocalModel:
    def __init__(self):
        from text_model import TextModel
        self.backend = TextModel()
        self.lock = threading.Lock()

    def complete(self, request: dict) -> dict:
        import torch

        if request.get("model") != MODEL_ID:
            raise ValueError("unknown model")
        messages = normalize_messages(request.get("messages"))
        tools = request.get("tools") or []
        if not isinstance(tools, list) or len(tools) > 32:
            raise ValueError("tools must be a list of at most 32 items")
        max_tokens = request.get("max_completion_tokens", request.get("max_tokens", 256))
        if not isinstance(max_tokens, int) or not 1 <= max_tokens <= 512:
            raise ValueError("max_tokens must be between 1 and 512")
        with self.lock:
            prompt = self.backend.tokenizer.apply_chat_template(
                messages, tools=tools or None, tokenize=False,
                add_generation_prompt=True, enable_thinking=False)
            inputs = self.backend.tokenizer(prompt, return_tensors="pt")
            input_tokens = inputs.input_ids.shape[1]
            if input_tokens + max_tokens > 16384:
                raise ValueError("context exceeds the 16384-token local limit")
            inputs = inputs.to("cuda")
            with torch.inference_mode():
                generated = self.backend.model.generate(
                    **inputs, max_new_tokens=max_tokens, do_sample=False,
                    pad_token_id=self.backend.tokenizer.eos_token_id)
            output_ids = generated[0, input_tokens:]
            output = self.backend.tokenizer.decode(output_ids, skip_special_tokens=True)
        content, calls = parse_output(output)
        message = {"role": "assistant", "content": content or None}
        if calls:
            message["tool_calls"] = calls
        return {
            "id": "chatcmpl-" + uuid.uuid4().hex,
            "object": "chat.completion", "created": int(time.time()),
            "model": MODEL_ID,
            "choices": [{"index": 0, "message": message,
                         "finish_reason": "tool_calls" if calls else "stop"}],
            "usage": {"prompt_tokens": input_tokens,
                      "completion_tokens": len(output_ids),
                      "total_tokens": input_tokens + len(output_ids)},
        }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Avoid logging user prompts or model output.
        pass

    def send_json(self, status: int, body: dict):
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        if self.path == "/healthz":
            self.send_json(200, {"status": "ready", "model": MODEL_ID})
        elif self.path == "/v1/models":
            self.send_json(200, {"object": "list", "data": [{
                "id": MODEL_ID, "object": "model", "created": 0, "owned_by": "local"
            }]})
        else:
            self.send_json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_json(404, {"error": {"message": "not found"}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1024 * 1024:
                raise ValueError("request body must be between 1 byte and 1 MiB")
            request = json.loads(self.rfile.read(length))
            if not isinstance(request, dict):
                raise ValueError("request must be a JSON object")
            response = self.server.model.complete(request)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            self.send_json(400, {"error": {"message": str(error), "type": "invalid_request_error"}})
            return
        except Exception as error:
            self.send_json(500, {"error": {"message": type(error).__name__, "type": "server_error"}})
            return
        if request.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            choice = response["choices"][0]
            chunk = {"id": response["id"], "object": "chat.completion.chunk",
                     "created": response["created"], "model": MODEL_ID,
                     "choices": [{"index": 0, "delta": choice["message"],
                                  "finish_reason": None}]}
            self.wfile.write(("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode())
            chunk["choices"] = [{"index": 0, "delta": {}, "finish_reason": choice["finish_reason"]}]
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode())
            self.wfile.flush()
        else:
            self.send_json(200, response)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.model = LocalModel()
    print(f"READY http://127.0.0.1:{args.port}/v1 model={MODEL_ID}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
