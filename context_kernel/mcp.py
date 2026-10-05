"""Small stdio MCP server: local reads and proposals, no owner mutations.

Implements the common tools subset of MCP 2024-11-05 through 2025-06-18.
Newer clients receive the explicit 2025-06-18 protocol negotiation fallback.
"""

from .common import KernelError, canonical
from .compiler import Compiler, READER_RULES
from . import __version__
from .protocol import parse_json


VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18"}


def schema(properties=None, required=None):
    return {"type": "object", "properties": properties or {}, "required": required or [], "additionalProperties": False}


STRING = {"type": "string", "minLength": 1, "maxLength": 16384}
TOOLS = [
    {"name": "memory_context", "description": "Get current scoped evidence. Always supply query: the actual user question, in English or Spanish. Never call with {}. A failed/unavailable call is NOT evidence that no fact exists; retry invalid arguments once with the original question. A successful empty result means no context was selected, not that the whole database was searched exhaustively.",
     "inputSchema": schema({"query": STRING | {"description": "Required original question, for example: Who is the current release approver?"}}, ["query"])},
    {"name": "memory_inspect", "description": "Inspect current scoped evidence. Withdrawn/history records are available only through the owner CLI.",
     "inputSchema": schema({"id": STRING}, ["id"])},
    {"name": "memory_status", "description": "Inspect counts in this server's fixed scope.", "inputSchema": schema()},
    {"name": "memory_why", "description": "Explain a previously prepared projection using its metadata-only trace.",
     "inputSchema": schema({"id": STRING}, ["id"])},
    {"name": "memory_propose", "description": "Propose remembering, correcting, or a delivery transition. Does not commit a fact; only the local owner CLI can approve.",
     "inputSchema": schema({"operation": {"type": "string", "enum": ["remember", "correct", "transition"]},
                            "payload": schema({"entity": STRING, "predicate": STRING, "target_id": STRING,
                                               "value": {}, "evidence": STRING, "valid_from": STRING, "valid_until": STRING,
                                               "event": {"type": "string", "enum": ["ordered", "not_arrived", "arrived", "returned"]}})},
                           ["operation", "payload"])},
]
for tool in TOOLS:
    tool["annotations"] = {"readOnlyHint": tool["name"] != "memory_propose", "destructiveHint": False,
                           "idempotentHint": tool["name"] != "memory_propose", "openWorldHint": False}


class ArgumentError(KernelError):
    def __init__(self, message, required=()):
        super().__init__(message)
        self.required = list(required)


def validate(arguments, contract):
    if not isinstance(arguments, dict) or set(arguments) - set(contract["properties"]):
        raise ArgumentError("Unsupported tool arguments.")
    if not set(contract["required"]) <= set(arguments):
        missing = sorted(set(contract["required"]) - set(arguments))
        raise ArgumentError("Missing required tool arguments: " + ", ".join(missing) + ".", missing)
    for name, value in arguments.items():
        spec = contract["properties"][name]
        if spec.get("type") == "string":
            if not isinstance(value, str) or not value.strip() or not 1 <= len(value) <= spec.get("maxLength", 16384):
                raise ArgumentError("Invalid tool string argument.")
            if "enum" in spec and value not in spec["enum"]:
                raise ArgumentError("Unsupported tool operation.")
        if spec.get("type") == "object":
            validate(value, spec)


class Server:
    def __init__(self, store):
        self.store = store
        self.initialized = False
        self.ready = False

    def call(self, name, arguments):
        tool = next((t for t in TOOLS if t["name"] == name), None)
        if tool is None:
            raise KernelError("Unknown memory tool.")
        validate(arguments, tool["inputSchema"])
        if name == "memory_context":
            compiler = Compiler(self.store)
            projection = compiler.prepare(arguments["query"])
            return {"status": projection.trace["status"], "projection_id": projection.id,
                    "context": parse_json(projection.content) if projection.content else None,
                    "trace": projection.trace, "plan": projection.plan.to_dict()}
        if name == "memory_inspect":
            record = self.store.inspect(arguments["id"])
            if record["effective_state"] != "active" or record["assertion_kind"] not in {"user_statement", "observed"}:
                raise KernelError("Statement is not eligible for agent access. Use the owner CLI for history.")
            return record
        if name == "memory_status":
            return self.store.status()
        if name == "memory_why":
            return self.store.trace(arguments["id"])
        return self.store.propose(arguments["operation"], arguments["payload"])

    def dispatch(self, request):
        request_id = request.get("id") if isinstance(request, dict) else None
        valid_id = request_id is None or (isinstance(request_id, (str, int)) and not isinstance(request_id, bool))

        def error(code, message):
            return {"jsonrpc": "2.0", "id": request_id if valid_id else None,
                    "error": {"code": code, "message": message}}

        if (not isinstance(request, dict) or not valid_id or request.get("jsonrpc") != "2.0"
                or not isinstance(request.get("method"), str)
                or set(request) - {"jsonrpc", "id", "method", "params"}):
            return error(-32600, "Invalid request.")
        method, params = request["method"], request.get("params", {})
        if "id" not in request:
            if method == "notifications/initialized" and self.initialized:
                self.ready = True
            return None
        if not isinstance(params, dict):
            return error(-32602, "Params must be an object.")
        if method not in {"ping", "initialize", "tools/list", "tools/call"}:
            return error(-32601, "Method not found.")
        if method == "ping":
            result = {}
        elif method == "initialize":
            if self.initialized:
                return error(-32600, "Server is already initialized.")
            if (not isinstance(params.get("protocolVersion"), str)
                    or not isinstance(params.get("capabilities"), dict)
                    or not isinstance(params.get("clientInfo"), dict)):
                return error(-32602, "Invalid initialization parameters.")
            version = params["protocolVersion"]
            self.initialized = True
            result = {"protocolVersion": version if version in VERSIONS else "2025-06-18",
                      "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "context-kernel", "version": __version__},
                      "instructions": " ".join(READER_RULES) + " Only owner-approved facts are retrieved. Supply the original question in memory_context.query. Tool errors mean unavailable evidence, not a missing fact. Retry invalid arguments at most once. This server cannot approve, revoke, or forget."}
        elif not self.ready:
            return error(-32600, "Initialize the server before calling tools.")
        elif method == "tools/list":
            if params.get("cursor"):
                return error(-32602, "This server has no additional tool pages.")
            result = {"tools": TOOLS}
        elif method == "tools/call":
            if set(params) - {"name", "arguments", "_meta"} or not isinstance(params.get("name"), str):
                return error(-32602, "Invalid tool call parameters.")
            try:
                value = self.call(params["name"], params.get("arguments", {}))
                result = {"content": [{"type": "text", "text": canonical(value)}], "structuredContent": value,
                          "isError": value.get("status") in {"unavailable", "insufficient_context"}}
            except KernelError as exc:
                arguments_error = isinstance(exc, ArgumentError)
                value = {"status": "unavailable", "error": {
                    "code": "invalid_arguments" if arguments_error else "memory_operation_failed",
                    "message": str(exc), "retryable": arguments_error,
                    "required_arguments": exc.required if arguments_error else [],
                    "guidance": "Retry once with the original question as query; never turn a tool error into UNKNOWN."
                                if arguments_error and params["name"] == "memory_context"
                                else "Evidence could not be obtained; report uncertainty rather than inventing a fact."}}
                result = {"content": [{"type": "text", "text": canonical(value)}], "structuredContent": value, "isError": True}
        else:
            return error(-32601, "Method not found.")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}


def serve(store, source, destination):
    server = Server(store)
    while True:
        raw = source.readline(65537)
        if not raw:
            return
        try:
            if len(raw) > 65536:
                while raw and not raw.endswith(b"\n"):
                    raw = source.readline(65537)
                raise KernelError("MCP input exceeds the byte limit.")
            response = server.dispatch(parse_json(raw))
        except KernelError:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid or oversized JSON input."}}
        except Exception:
            # Never disclose stored evidence or Python exception payloads to the client.
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": "Local memory operation failed."}}
        if response is not None:
            destination.write(canonical(response) + "\n")
            destination.flush()
            result = response.get("result", {})
            value = result.get("structuredContent", {})
            if "projection_id" in value:
                store.mark_emitted(value["projection_id"])
