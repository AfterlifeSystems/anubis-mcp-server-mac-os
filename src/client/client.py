import asyncio
import base64
import json
import os
from pathlib import Path, PurePosixPath

from deepagents.backends import LocalShellBackend
from langchain_mcp_adapters.client import MultiServerMCPClient

MCP_SETTINGS = {
    "macOS-Filesystem": {
        "transport": "http",
        "url": "http://localhost:8000/mcp",
    }
}

DATA_DIR = os.path.expanduser(
    os.getenv(
        "HEALTH_DATA_DIR",
        "~/Documents/Health Auto Export/health_metric_data",
    )
)

ANALYSIS_ROOT = Path(
    os.getenv("ANALYSIS_ROOT", "/tmp/health-analysis"),
).resolve()

DEFAULT_RECURSION_LIMIT = int(os.getenv("AGENT_RECURSION_LIMIT", "100"))


def make_agent_config(
    thread_id: str | None = None,
    recursion_limit: int = DEFAULT_RECURSION_LIMIT,
) -> dict:
    """LangGraph config with a higher recursion limit for multi-step deep agents."""
    from langchain_core.utils.uuid import uuid7

    return {
        "configurable": {"thread_id": thread_id or str(uuid7())},
        "recursion_limit": recursion_limit,
    }


LOCAL_SHELL_DATA_PROMPT = (
    "Data files are at /data/ — use ls and read_file with that virtual path. "
    "For execute() shell/python commands, use relative paths like data/<filename> "
    "because the shell cwd is the analysis root, not /."
)


def parse_mcp_result(result):
    """Normalize LangChain MCP tool output to Python values."""
    if isinstance(result, list) and result and isinstance(result[0], dict) and "text" in result[0]:
        text = result[0]["text"]
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return result


async def mcp_tool(client: MultiServerMCPClient, name: str, args: dict):
    tools = await client.get_tools()
    tool = next(t for t in tools if t.name == name)
    return parse_mcp_result(await tool.ainvoke(args))


def create_local_backend(analysis_root: Path | str | None = None) -> LocalShellBackend:
    """Create a LocalShellBackend with virtual paths under analysis_root."""
    root = Path(analysis_root or ANALYSIS_ROOT)
    root.mkdir(parents=True, exist_ok=True)
    return LocalShellBackend(
        root_dir=str(root),
        virtual_mode=True,
        inherit_env=True,
    )


async def load_directory_into_local_backend(
    client: MultiServerMCPClient,
    directory: str,
    backend: LocalShellBackend,
    data_dir: str = "/data",
    extensions: tuple[str, ...] = (".csv", ".json"),
    limit: int | None = None,
) -> list[str]:
    """Discover host files via MCP and write them into the local backend root."""
    files = await mcp_tool(client, "list_all_files", {
        "directory": directory,
        "recursive": True,
    })
    targets = [f for f in files if f.lower().endswith(extensions)]
    if limit is not None:
        targets = targets[:limit]

    async def read_one(host_path: str) -> tuple[str, bytes]:
        b64 = await mcp_tool(client, "read_file_bytes", {"file_path": host_path})
        name = PurePosixPath(host_path).name
        virtual_path = f"{data_dir.rstrip('/')}/{name}"
        return virtual_path, base64.b64decode(b64)

    payloads = await asyncio.gather(*(read_one(p) for p in targets))
    uploaded_paths: list[str] = []

    def write_all() -> None:
        root = Path(backend.cwd)
        for virtual_path, content in payloads:
            disk_path = root / virtual_path.lstrip("/")
            disk_path.parent.mkdir(parents=True, exist_ok=True)
            disk_path.write_bytes(content)
            uploaded_paths.append(virtual_path)

    await asyncio.to_thread(write_all)
    return uploaded_paths


async def cleanup_analysis():
    import shutil
    shutil.rmtree(ANALYSIS_ROOT, ignore_errors=True)
    print(f"Cleaned {ANALYSIS_ROOT}")


# Backward-compatible alias
load_directory_into_sandbox = load_directory_into_local_backend


async def main():
    client = MultiServerMCPClient(MCP_SETTINGS)
    backend = create_local_backend()
    files = await mcp_tool(client, "list_all_files", {"directory": DATA_DIR})
    if files:
        preview = await mcp_tool(client, "preview_data", {"file_path": files[0]})
        print(preview)
        uploaded = await load_directory_into_local_backend(
            client, DATA_DIR, backend, limit=3,
        )
        print("uploaded:", uploaded)
