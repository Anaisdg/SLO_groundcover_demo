#!/usr/bin/env python3
"""
SLO Remediation Agent
=====================
Autonomous agent that:
  1. Detects SLO breaches via Groundcover MCP (get_workloads)
  2. Diagnoses root cause via Groundcover MCP (query_traces)
  3. Files a Linear ticket via Linear MCP (save_issue)
  4. Outputs a suggested code patch

Architecture:
  - boto3 Bedrock Converse API  → LLM reasoning loop
  - mcp SDK (Streamable HTTP)   → Tool execution against Groundcover + Linear
  - Single-pass agentic loop    → detect → diagnose → ticket → patch

Usage:
    export GROUNDCOVER_MCP_URL="https://mcp.groundcover.com/sse"
    export LINEAR_MCP_URL="https://mcp.linear.app/mcp"
    export GROUNDCOVER_API_KEY="..."   # if required by the MCP server
    export LINEAR_API_KEY="..."        # if required by the MCP server
    python slo_agent.py
"""

import asyncio
import contextlib
import json
import os
import sys
from datetime import datetime, timezone

import boto3

# ---------------------------------------------------------------------------
# MCP Client (lazy import — only needed at tool-execution time)
# ---------------------------------------------------------------------------
# We use the mcp SDK's StreamableHTTP transport to call Groundcover and Linear.
# Each MCP server gets its own client session.

from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.client.streamable_http import streamablehttp_client


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-opus-4-6-v1")
BEDROCK_REGION = os.environ.get("AWS_REGION", "us-east-2")
SLO_THRESHOLD_MS = float(os.environ.get("SLO_THRESHOLD_MS", "500"))

GROUNDCOVER_MCP_URL = os.environ.get("GROUNDCOVER_MCP_URL", "https://mcp.groundcover.com/api/mcp")
LINEAR_MCP_URL = os.environ.get("LINEAR_MCP_URL", "https://mcp.linear.app/mcp")

# Linear target
LINEAR_TEAM = "Marketing"
LINEAR_PROJECT = "SLO Demo"
LINEAR_LABELS = ["slo-breach", "Bug"]
LINEAR_PRIORITY = 1  # Urgent

# Groundcover filters (adjust to match your cluster)
TARGET_NAMESPACE = os.environ.get("TARGET_NAMESPACE", "slo-demo")


# ---------------------------------------------------------------------------
# Bedrock Tool Definitions
# ---------------------------------------------------------------------------
# These mirror the Groundcover + Linear MCP schemas so Bedrock knows what
# tools are available. The agent picks which to call; we execute via MCP.

TOOL_CONFIG = {
    "tools": [
        {
            "toolSpec": {
                "name": "groundcover_get_workloads",
                "description": (
                    "List workloads from Groundcover with latency, RPS, and error rate metrics. "
                    "Use this to detect SLO breaches by sorting by p99 latency descending."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "namespaces": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Namespace filter",
                            },
                            "sortBy": {
                                "type": "string",
                                "enum": [
                                    "p50", "p95", "p99", "rps", "errorRate",
                                    "cpuUsage", "memoryUsage", "issueCount",
                                ],
                                "description": "Column to sort by",
                            },
                            "order": {
                                "type": "string",
                                "enum": ["asc", "desc"],
                                "description": "Sort order",
                            },
                            "limit": {
                                "type": "integer",
                                "description": "Max workloads to return",
                            },
                        },
                        "required": [],
                    }
                },
            }
        },
        {
            "toolSpec": {
                "name": "groundcover_query_traces",
                "description": (
                    "Query distributed traces from Groundcover. Use this to diagnose slow "
                    "endpoints by filtering for a specific workload and sorting by latency."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "namespaces": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Namespace filter",
                            },
                            "workloads": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Workload name filter",
                            },
                            "protocols": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Protocol filter (http, grpc, etc.)",
                            },
                            "sortBy": {
                                "type": "string",
                                "enum": ["time", "latency", "workload", "statusCode"],
                                "description": "Sort field",
                            },
                            "order": {
                                "type": "string",
                                "enum": ["asc", "desc"],
                            },
                            "period": {
                                "type": "string",
                                "description": "ISO 8601 duration, e.g. PT30M",
                            },
                            "limit": {
                                "type": "integer",
                                "description": "Max traces to return",
                            },
                        },
                        "required": [],
                    }
                },
            }
        },
        {
            "toolSpec": {
                "name": "groundcover_query_monitors",
                "description": (
                    "Query monitor status from Groundcover. Use this to check if an alert "
                    "is already firing for the workload."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "namespaces": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "workloads": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "statuses": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Filter: Alerting, Pending, Normal",
                            },
                            "limit": {"type": "integer"},
                        },
                        "required": [],
                    }
                },
            }
        },
        {
            "toolSpec": {
                "name": "linear_create_issue",
                "description": (
                    "Create an issue in Linear to track the SLO breach. "
                    "The issue will be filed in the Marketing team, SLO Demo project."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "title": {
                                "type": "string",
                                "description": "Issue title, e.g. '[SLO Breach] order-service POST /orders p99 > 2850ms'",
                            },
                            "description": {
                                "type": "string",
                                "description": "Markdown description with detection, diagnosis, and suggested fix sections",
                            },
                            "priority": {
                                "type": "integer",
                                "description": "0=None, 1=Urgent, 2=High, 3=Normal, 4=Low",
                            },
                        },
                        "required": ["title", "description"],
                    }
                },
            }
        },
    ]
}


# ---------------------------------------------------------------------------
# System Prompt
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = f"""You are an autonomous SLO remediation agent. Your job is to detect, diagnose, and file tickets for SLO breaches.

## Context
- SLO target: p99 latency < {SLO_THRESHOLD_MS}ms for all workloads in the "{TARGET_NAMESPACE}" namespace
- Current time: {{current_time}}
- You have access to Groundcover (observability) and Linear (issue tracking)

## Workflow — execute these steps in order:

### Step 1: Detect
Call `groundcover_get_workloads` with:
- namespaces: ["{TARGET_NAMESPACE}"]
- sortBy: "p99"
- order: "desc"
- limit: 10

Identify any workload where p99 > {SLO_THRESHOLD_MS}ms. If none found, report "No SLO breaches detected" and stop.

### Step 2: Diagnose
For each breaching workload, call `groundcover_query_traces` with:
- workloads: ["<workload_name>"]
- namespaces: ["{TARGET_NAMESPACE}"]
- protocols: ["http"]
- sortBy: "latency"
- order: "desc"
- period: "PT30M"
- limit: 10

Analyze the traces to determine:
- Which endpoint is slowest
- What the latency distribution looks like
- Any patterns (N+1 queries, slow dependencies, etc.)

### Step 3: File Ticket
Call `linear_create_issue` with:
- title: "[SLO Breach] <workload> <method> <endpoint> p99 > <value>ms (target: {SLO_THRESHOLD_MS}ms)"
- description: A detailed markdown report with sections:
  ## SLO Breach Detected
  (workload, namespace, endpoint, current p99, target, breach factor, detection time)

  ## Root Cause Analysis
  (what the traces revealed, pattern identified)

  ## Suggested Fix
  (concrete code-level recommendation)

  ## Evidence
  (top 3-5 trace samples with latency values)
- priority: 1 (Urgent) if breach > 2x target, else 2 (High)

### Step 4: Output Patch
After filing the ticket, output a suggested code patch that would fix the issue.
Format it as a unified diff or clear before/after code blocks.

## Rules
- Be precise with numbers — report exact p99 values from the tools
- Always include trace evidence in the ticket
- If multiple workloads breach, handle each one separately
- Do NOT hallucinate trace data — only use what the tools return
"""


# ---------------------------------------------------------------------------
# MCP Tool Execution
# ---------------------------------------------------------------------------
class MCPToolRouter:
    """Routes Bedrock tool calls to the appropriate MCP server."""

    def __init__(self):
        self._gc_session: ClientSession | None = None
        self._linear_session: ClientSession | None = None
        self._exit_stack: contextlib.AsyncExitStack | None = None

    async def connect(self):
        """Establish MCP sessions to both servers."""
        self._exit_stack = contextlib.AsyncExitStack()
        await self._exit_stack.__aenter__()

        print("[MCP] Connecting to Groundcover (via mcp-remote)...", flush=True)
        gc_server = StdioServerParameters(
            command="npx",
            args=[
                "-y", "mcp-remote@0.1.30",
                GROUNDCOVER_MCP_URL,
                "54278",
                "--header", f"X-Timezone:{os.environ.get('GROUNDCOVER_TIMEZONE', 'America/Chicago')}",
                "--header", f"X-Tenant-UUID:{os.environ.get('GROUNDCOVER_TENANT_UUID', '')}",
                "--header", f"X-Backend-Id:{os.environ.get('GROUNDCOVER_BACKEND_ID', '')}",
            ],
        )
        gc_read, gc_write = await self._exit_stack.enter_async_context(
            stdio_client(gc_server)
        )
        self._gc_session = await self._exit_stack.enter_async_context(
            ClientSession(gc_read, gc_write)
        )
        await self._gc_session.initialize()
        print("[MCP] Groundcover connected.", flush=True)

        print("[MCP] Connecting to Linear (via mcp-remote)...", flush=True)
        linear_server = StdioServerParameters(
            command="npx",
            args=["-y", "mcp-remote", LINEAR_MCP_URL],
        )
        lin_read, lin_write = await self._exit_stack.enter_async_context(
            stdio_client(linear_server)
        )
        self._linear_session = await self._exit_stack.enter_async_context(
            ClientSession(lin_read, lin_write)
        )
        await self._linear_session.initialize()
        print("[MCP] Linear connected.", flush=True)

    async def close(self):
        """Clean up MCP sessions."""
        if self._exit_stack:
            await self._exit_stack.aclose()

    def _gc_headers(self) -> dict:
        return {
            "X-Timezone": os.environ.get("GROUNDCOVER_TIMEZONE", "America/Chicago"),
            "X-Tenant-UUID": os.environ.get("GROUNDCOVER_TENANT_UUID", ""),
            "X-Backend-Id": os.environ.get("GROUNDCOVER_BACKEND_ID", ""),
        }

    def _linear_headers(self) -> dict:
        key = os.environ.get("LINEAR_API_KEY", "")
        if key:
            return {"Authorization": f"Bearer {key}"}
        return {}

    async def execute(self, tool_name: str, tool_input: dict) -> str:
        """Execute a tool call and return the JSON result as a string."""
        if tool_name == "groundcover_get_workloads":
            return await self._call_gc("get_workloads", tool_input)

        elif tool_name == "groundcover_query_traces":
            return await self._call_gc("query_traces", tool_input)

        elif tool_name == "groundcover_query_monitors":
            return await self._call_gc("query_monitors", tool_input)

        elif tool_name == "linear_create_issue":
            return await self._call_linear_create_issue(tool_input)

        else:
            return json.dumps({"error": f"Unknown tool: {tool_name}"})

    async def _call_gc(self, tool_name: str, params: dict) -> str:
        """Call a Groundcover MCP tool."""
        result = await self._gc_session.call_tool(tool_name, params)
        # MCP returns content blocks; extract text
        texts = [block.text for block in result.content if hasattr(block, "text")]
        return "\n".join(texts) if texts else json.dumps({"result": "empty"})

    async def _call_linear_create_issue(self, params: dict) -> str:
        """Call Linear save_issue via MCP with our fixed team/project/labels."""
        linear_params = {
            "title": params["title"],
            "description": params.get("description", ""),
            "team": LINEAR_TEAM,
            "project": LINEAR_PROJECT,
            "labels": LINEAR_LABELS,
            "priority": params.get("priority", LINEAR_PRIORITY),
            "state": "Backlog",
        }
        result = await self._linear_session.call_tool("save_issue", linear_params)
        texts = [block.text for block in result.content if hasattr(block, "text")]
        return "\n".join(texts) if texts else json.dumps({"result": "issue created"})


# ---------------------------------------------------------------------------
# Bedrock Converse Agent Loop
# ---------------------------------------------------------------------------
class SLOAgent:
    """Single-pass agentic loop using Bedrock Converse + MCP tools."""

    def __init__(self):
        self.bedrock = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)
        self.router = MCPToolRouter()
        self.messages = []
        self.max_turns = 15  # Safety limit

    async def run(self):
        """Execute the full detect → diagnose → ticket → patch workflow."""
        await self.router.connect()

        try:
            # Build system prompt with current time
            system = SYSTEM_PROMPT.format(
                current_time=datetime.now(timezone.utc).isoformat()
            )

            # Initial user message kicks off the workflow
            self.messages = [
                {
                    "role": "user",
                    "content": [
                        {
                            "text": (
                                f"Run the SLO remediation workflow now. "
                                f"Check the '{TARGET_NAMESPACE}' namespace for any workloads "
                                f"with p99 latency above {SLO_THRESHOLD_MS}ms. "
                                f"Diagnose, file a ticket, and suggest a fix."
                            )
                        }
                    ],
                }
            ]

            for turn in range(self.max_turns):
                print(f"\n{'='*60}")
                print(f"[Agent] Turn {turn + 1}")
                print(f"{'='*60}")

                # Call Bedrock
                response = self.bedrock.converse(
                    modelId=BEDROCK_MODEL_ID,
                    system=[{"text": system}],
                    messages=self.messages,
                    toolConfig=TOOL_CONFIG,
                )

                stop_reason = response["stopReason"]
                output_message = response["output"]["message"]
                self.messages.append(output_message)

                # Process response content
                for block in output_message["content"]:
                    if "text" in block:
                        print(f"\n[Agent] {block['text']}")
                    elif "toolUse" in block:
                        tool = block["toolUse"]
                        print(f"\n[Tool Call] {tool['name']}")
                        print(f"  Input: {json.dumps(tool['input'], indent=2)}")

                # If the model is done, we're finished
                if stop_reason == "end_turn":
                    print("\n[Agent] Workflow complete.")
                    break

                # If the model wants to use tools, execute them
                if stop_reason == "tool_use":
                    tool_results = []
                    for block in output_message["content"]:
                        if "toolUse" not in block:
                            continue
                        tool = block["toolUse"]
                        tool_id = tool["toolUseId"]
                        tool_name = tool["name"]
                        tool_input = tool["input"]

                        print(f"\n[Executing] {tool_name}...")
                        try:
                            result_text = await self.router.execute(tool_name, tool_input)
                            print(f"[Result] {result_text[:500]}...")
                            tool_results.append(
                                {
                                    "toolResult": {
                                        "toolUseId": tool_id,
                                        "content": [{"text": result_text}],
                                    }
                                }
                            )
                        except Exception as e:
                            print(f"[Error] {tool_name}: {e}")
                            tool_results.append(
                                {
                                    "toolResult": {
                                        "toolUseId": tool_id,
                                        "content": [{"text": f"Error: {str(e)}"}],
                                        "status": "error",
                                    }
                                }
                            )

                    # Feed tool results back to the model
                    self.messages.append(
                        {"role": "user", "content": tool_results}
                    )

            else:
                print(f"\n[Agent] Hit max turns ({self.max_turns}). Stopping.")

        finally:
            await self.router.close()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
async def main():
    agent = SLOAgent()
    await agent.run()


if __name__ == "__main__":
    asyncio.run(main())
