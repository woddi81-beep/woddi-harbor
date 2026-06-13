"""SAP documentation MCP server exposed through FastAPI at ``/mcp``."""
from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin
from uuid import uuid4

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "sap-docs-mcp-server"
SERVER_VERSION = "0.1.0"
DEFAULT_BASE_URL = "https://help.sap.com"
DEFAULT_TIMEOUT_SECONDS = 30.0


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self._parts: list[str] = []
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if tag in {"p", "br", "div", "section", "article", "main", "li", "h1", "h2", "h3", "h4", "tr"}:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self._skip_depth:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False
        if tag in {"p", "div", "section", "article", "main", "li", "h1", "h2", "h3", "h4", "tr"}:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self.title = f"{self.title} {text}".strip()
        self._parts.append(text)

    def text(self) -> str:
        lines = [line.strip() for line in " ".join(self._parts).splitlines() if line.strip()]
        return "\n".join(lines)


def _jsonrpc_result(request_id: Any, result: dict[str, Any], *, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result}, headers=headers)


def _jsonrpc_error(
    request_id: Any,
    code: int,
    message: str,
    *,
    data: Any = None,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
    if data is not None:
        payload["error"]["data"] = data
    return JSONResponse(payload, status_code=status_code, headers=headers)


def _tool_schema() -> list[dict[str, Any]]:
    return [
        {
            "name": "search_sap_docs",
            "description": "Search SAP Help Portal documentation.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query."},
                    "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 20},
                },
                "required": ["query"],
            },
        },
        {
            "name": "fetch_sap_doc",
            "description": "Fetch and extract readable text from an SAP Help Portal page.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Absolute SAP Help URL or path relative to help.sap.com."},
                    "max_chars": {"type": "integer", "default": 12000, "minimum": 1000, "maximum": 50000},
                },
                "required": ["url"],
            },
        },
    ]


@dataclass
class SapDocsBackend:
    base_url: str = DEFAULT_BASE_URL
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    def _client(self) -> httpx.Client:
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/json",
            "User-Agent": "woddi-harbor-sap-docs-mcp/0.1",
        }
        return httpx.Client(timeout=self.timeout_seconds, follow_redirects=True, headers=headers)

    def _absolute_url(self, url_or_path: str) -> str:
        value = url_or_path.strip()
        if value.startswith("http://") or value.startswith("https://"):
            return value
        return urljoin(self.base_url.rstrip("/") + "/", value.lstrip("/"))

    def health(self) -> dict[str, Any]:
        return {"ok": True, "server": SERVER_NAME, "base_url": self.base_url}

    def list_tools(self) -> list[dict[str, Any]]:
        tools = _tool_schema()
        for tool in tools:
            tool["annotations"] = {"title": tool["name"], "readOnlyHint": True}
        return tools

    def search_sap_docs(self, query: str, limit: int = 10) -> dict[str, Any]:
        clean_query = query.strip()
        if not clean_query:
            raise ValueError("query is required.")
        limit = max(1, min(int(limit), 20))
        search_url = self._absolute_url("/http.svc/elasticsearch")
        params: dict[str, str | int] = {
            "q": clean_query,
            "area": "content",
            "transtype": "standard,html,pdf,others",
            "size": limit,
        }
        with self._client() as client:
            response = client.get(search_url, params=params)
        response.raise_for_status()
        payload = response.json()
        return {"query": clean_query, "results": self._extract_search_results(payload, limit)}

    def _extract_search_results(self, payload: Any, limit: int) -> list[dict[str, Any]]:
        candidates: list[Any] = []
        if isinstance(payload, dict):
            hits = payload.get("hits")
            if isinstance(hits, dict) and isinstance(hits.get("hits"), list):
                candidates = hits["hits"]
            elif isinstance(payload.get("results"), list):
                candidates = payload["results"]
            elif isinstance(payload.get("items"), list):
                candidates = payload["items"]
        results: list[dict[str, Any]] = []
        for item in candidates[:limit]:
            source = item.get("_source", item) if isinstance(item, dict) else {}
            if not isinstance(source, dict):
                continue
            title = str(source.get("title") or source.get("loioTitle") or source.get("name") or "").strip()
            path = str(source.get("url") or source.get("path") or source.get("href") or "").strip()
            snippet = str(source.get("description") or source.get("summary") or source.get("snippet") or "").strip()
            if not title and not path:
                continue
            results.append({"title": title or path, "url": self._absolute_url(path) if path else "", "snippet": snippet})
        return results

    def fetch_sap_doc(self, url: str, max_chars: int = 12000) -> dict[str, Any]:
        target_url = self._absolute_url(url)
        max_chars = max(1000, min(int(max_chars), 50000))
        with self._client() as client:
            response = client.get(target_url)
        response.raise_for_status()
        try:
            from bs4 import BeautifulSoup
        except ModuleNotFoundError:
            parser = _TextExtractor()
            parser.feed(response.text)
            title = parser.title
            compact_text = parser.text()
        else:
            soup = BeautifulSoup(response.text, "html.parser")
            for tag in soup(["script", "style", "noscript", "svg"]):
                tag.decompose()
            title = soup.title.get_text(" ", strip=True) if soup.title else ""
            main = soup.find("main") or soup.find("article") or soup.body or soup
            text = main.get_text("\n", strip=True)
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            compact_text = "\n".join(lines)
        return {"url": str(response.url), "title": title, "text": compact_text[:max_chars], "truncated": len(compact_text) > max_chars}

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "search_sap_docs":
            payload = self.search_sap_docs(str(arguments["query"]), int(arguments.get("limit", 10)))
            return self._tool_result(name, arguments, payload)
        if name == "fetch_sap_doc":
            payload = self.fetch_sap_doc(str(arguments["url"]), int(arguments.get("max_chars", 12000)))
            return self._tool_result(name, arguments, payload)
        raise ValueError(f"Unknown tool: {name}")

    def _tool_result(self, tool_name: str, arguments: dict[str, Any], payload: Any) -> dict[str, Any]:
        summary = {"tool": tool_name, "arguments": arguments, "data": payload}
        return {"content": [{"type": "text", "text": f"{tool_name} completed successfully."}], "structuredContent": summary}


def create_sap_docs_app(base_url: str = DEFAULT_BASE_URL) -> FastAPI:
    backend = SapDocsBackend(base_url=base_url)
    sessions: set[str] = set()
    app = FastAPI(title="SAP Docs MCP Server")

    @app.get("/health")
    def health() -> dict[str, Any]:
        return backend.health()

    @app.post("/mcp")
    async def mcp_endpoint(request: Request) -> Response:
        try:
            payload = await request.json()
        except Exception as exc:
            return _jsonrpc_error(None, -32700, "Parse error", data=str(exc), status_code=400)
        if not isinstance(payload, dict):
            return _jsonrpc_error(None, -32600, "Invalid Request", status_code=400)
        request_id = payload.get("id")
        if payload.get("jsonrpc") != "2.0":
            return _jsonrpc_error(request_id, -32600, "Invalid Request", data="Expected jsonrpc='2.0'.", status_code=400)
        method = payload.get("method")
        params = payload.get("params") or {}
        session_id = request.headers.get("mcp-session-id")
        session_headers = {"mcp-session-id": session_id} if session_id else None
        if not isinstance(method, str) or not method:
            return _jsonrpc_error(request_id, -32600, "Invalid Request", data="Missing method.", status_code=400)
        if not isinstance(params, dict):
            return _jsonrpc_error(request_id, -32602, "Invalid params", data="Expected params to be an object.", status_code=400, headers=session_headers)
        try:
            if method == "initialize":
                session_id = str(uuid4())
                sessions.add(session_id)
                return _jsonrpc_result(
                    request_id,
                    {
                        "protocolVersion": MCP_PROTOCOL_VERSION,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    },
                    headers={"mcp-session-id": session_id},
                )
            if method == "notifications/initialized":
                headers = {"mcp-session-id": session_id} if session_id else None
                return Response(status_code=202, headers=headers)
            if session_id and session_id not in sessions:
                return _jsonrpc_error(request_id, -32001, "Unknown MCP session", status_code=400)
            if method == "tools/list":
                return _jsonrpc_result(request_id, {"tools": backend.list_tools()}, headers=session_headers)
            if method == "tools/call":
                tool_name = params.get("name")
                arguments = params.get("arguments") or {}
                if not isinstance(tool_name, str) or not tool_name:
                    return _jsonrpc_error(request_id, -32602, "Invalid params", data="Tool name is required.", status_code=400, headers=session_headers)
                if not isinstance(arguments, dict):
                    return _jsonrpc_error(request_id, -32602, "Invalid params", data="Tool arguments must be an object.", status_code=400, headers=session_headers)
                return _jsonrpc_result(request_id, backend.call_tool(tool_name, arguments), headers=session_headers)
            return _jsonrpc_error(request_id, -32601, "Method not found", headers=session_headers)
        except httpx.HTTPStatusError as exc:
            detail = {"status_code": exc.response.status_code, "response": exc.response.text[:1000], "url": str(exc.request.url)}
            return _jsonrpc_error(request_id, -32002, "SAP docs request failed", data=detail, headers=session_headers)
        except KeyError as exc:
            return _jsonrpc_error(request_id, -32602, "Invalid params", data=f"Missing required argument: {exc.args[0]}", status_code=400, headers=session_headers)
        except ValueError as exc:
            return _jsonrpc_error(request_id, -32602, "Invalid params", data=str(exc), status_code=400, headers=session_headers)
        except Exception as exc:
            return _jsonrpc_error(request_id, -32000, "Server error", data=str(exc), headers=session_headers)

    return app


__all__ = ["SapDocsBackend", "create_sap_docs_app", "MCP_PROTOCOL_VERSION"]
