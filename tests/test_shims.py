#!/usr/bin/env python3
"""Unit tests for the shared shim layer. No network, no server.

The boundaries that matter:
  * Responses request -> chat request (including the multi-turn tool loop)
  * chat reply -> Responses reply
  * tool-argument repair (the `justification` pairing that broke the first
    live Codex run)
  * the Responses SSE envelope Codex parses
  * the gate: harness tools are always added, never duplicated

Run: python3 tests/test_shims.py
"""
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "shared"))

import gate  # noqa: E402
import responses as R  # noqa: E402

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}  {detail}")


print("== gate: harness tools ==")
tools = [{"type": "function", "function": {"name": "exec_command"}}]
out = gate.add_harness_tools(tools)
names = [t["function"]["name"] for t in out]
check("caller tool kept", "exec_command" in names)
check("all 12 harness names present",
      set(gate.HARNESS_TOOLS).issubset(set(names)), sorted(names))
check("no duplicates on second pass",
      len(gate.add_harness_tools(out)) == len(out))
check("harness description says not callable",
      "Never call it" in out[1]["function"]["description"])
check("empty tool list yields 12", len(gate.add_harness_tools([])) == 12)

print()
print("== request translation ==")
body = {
    "model": "mimo-v2.6-flash-free",
    "instructions": "You are a coding agent.",
    "input": [
        {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "hello"}]},
        {"type": "function_call", "call_id": "c1", "name": "exec_command",
         "arguments": '{"cmd":"ls"}'},
        {"type": "function_call_output", "call_id": "c1", "output": "file.txt"},
    ],
    "tools": [{"type": "function", "name": "exec_command", "description": "run",
               "parameters": {"type": "object",
                              "properties": {"cmd": {"type": "string"}},
                              "required": ["cmd"]}}],
    "max_output_tokens": 500, "stream": True,
}
chat = R.to_chat_body(body)
roles = [m["role"] for m in chat["messages"]]
check("instructions -> system", roles[0] == "system", roles)
check("user message carried", "user" in roles, roles)
check("function_call -> assistant tool_calls",
      any(m.get("tool_calls") for m in chat["messages"]))
check("function_call_output -> tool message",
      any(m.get("role") == "tool" and m.get("tool_call_id") == "c1"
          for m in chat["messages"]))
cn = {t["function"]["name"] for t in chat["tools"]}
check("caller tool preserved", "exec_command" in cn)
check("harness names added", set(gate.HARNESS_TOOLS).issubset(cn))
check("max_output_tokens -> max_tokens", chat.get("max_tokens") == 500)
check("stream False (router folds)", chat.get("stream") is False)

print()
print("== reply fold ==")
reply = {"id": "a", "created": 1, "model": "m",
         "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
             "role": "assistant", "content": None,
             "tool_calls": [{"id": "call_1", "type": "function", "function": {
                 "name": "exec_command", "arguments": '{"cmd":"echo hi"}'}}]}}],
         "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
o = R.fold_chat_to_responses(reply, "m", {}, {"exec_command"})
check("object is a response", o["object"] == "response")
fc = [i for i in o["output"] if i["type"] == "function_call"]
check("one function_call item", len(fc) == 1)
check("call_id preserved", fc[0]["call_id"] == "call_1")
check("usage mapped", o["usage"]["total_tokens"] == 15)

print()
print("== harness-call filtering ==")
reply3 = {"id": "e", "choices": [{"index": 0, "finish_reason": "tool_calls",
          "message": {"role": "assistant", "content": None, "tool_calls": [
              {"id": "x", "type": "function",
               "function": {"name": "shell", "arguments": "{}"}},
              {"id": "y", "type": "function",
               "function": {"name": "exec_command", "arguments": '{"cmd":"ls"}'}}]}}],
          "usage": {}}
o3 = R.fold_chat_to_responses(reply3, "m", {}, {"exec_command"})
kept = [i["name"] for i in o3["output"] if i["type"] == "function_call"]
check("injected harness call dropped", "shell" not in kept, kept)
check("caller call kept", "exec_command" in kept, kept)

print()
print("== argument sanitizer ==")
schema = {"exec_command": {"properties": {
    "cmd": {"type": "string"}, "justification": {"type": "string"},
    "sandbox_permissions": {"type": "string"}}}}
s1 = R.sanitize_args("exec_command",
                     '{"cmd":"ls","justification":"need root"}', schema)
check("lone justification removed", "justification" not in json.loads(s1), s1)
check("cmd survives", json.loads(s1)["cmd"] == "ls", s1)
s2 = R.sanitize_args("exec_command",
                     '{"cmd":"ls","justification":"x",'
                     '"sandbox_permissions":"require_escalated"}', schema)
check("justification kept when paired", "justification" in json.loads(s2), s2)
s3 = R.sanitize_args("exec_command", '{"cmd":"ls","bogus":1}', schema)
check("undeclared field dropped", "bogus" not in json.loads(s3), s3)
s4 = R.sanitize_args("exec_command", "not json", schema)
check("malformed args do not raise", isinstance(s4, str), s4)

print()
print("== SSE envelope ==")
text = R.responses_stream(o).decode("utf8", "replace")
check("response.created emitted", '"response.created"' in text)
check("output_item.done emitted", '"response.output_item.done"' in text)
check("response.completed emitted", '"response.completed"' in text)
check("[DONE] terminator", "[DONE]" in text)
created = json.loads([l for l in text.split("\n")
                      if '"response.created"' in l][0][5:])
check("created envelope has EMPTY output (Codex needs an active item)",
      created["response"]["output"] == [], created["response"]["output"])
comp = json.loads([l for l in text.split("\n")
                   if '"response.completed"' in l][0][5:])["response"]
check("completed carries the tool call",
      any(i["type"] == "function_call" for i in comp["output"]))

print()
print("== chat -> responses body (OpenCode direction) ==")
rb = R.to_responses_body({"model": "m", "messages": [
    {"role": "system", "content": "be terse"},
    {"role": "user", "content": "hi"}],
    "max_tokens": 4, "stream": True})
check("system -> instructions", rb.get("instructions") == "be terse", rb)
check("messages dropped", "messages" not in rb)
check("max_tokens -> max_output_tokens", rb.get("max_output_tokens") == 16, rb)
check("stream forced true", rb.get("stream") is True)

print()
print(f"== {PASS} passed, {FAIL} failed ==")
sys.exit(1 if FAIL else 0)
