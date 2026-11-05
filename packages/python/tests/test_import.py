import subprocess
import sys


def test_jeff_imports_without_torch_or_mlx() -> None:
    code = "import sys, jeff; bad = [m for m in ('torch', 'transformers', 'mlx') if m in sys.modules]; print(bad)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()
    assert out == "[]"


def test_jeff_and_its_command_import_without_the_server_or_the_mcp_sdk() -> None:
    """The MCP SDK (the extra jeff[mcp]) is imported only by jeff.mcp, when it is used."""
    code = ("import sys, jeff, jeff.cli, jeff.remote, jeff.listing; bad = [m for m in ('mcp', 'mcp_types', 'fastapi', "
            "'uvicorn', 'jeff_server') if m in sys.modules]; print(bad)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()
    assert out == "[]"


def test_jeff_and_remote_mode_import_without_websockets() -> None:
    """websockets is imported only by the channel transport (jeff.channel_client), when channel=True is used."""
    code = "import sys, jeff, jeff.remote; print([m for m in sys.modules if m.split('.')[0] == 'websockets'])"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()
    assert out == "[]"


def test_runtime_imports() -> None:
    import jeff_runtime  # noqa: F401


def test_jeff_exports_every_error_and_the_public_types_without_leaking_typing() -> None:
    import jeff
    import jeff.errors

    for name in jeff.errors.__all__:
        assert getattr(jeff, name) is getattr(jeff.errors, name) and name in jeff.__all__
    errors = {name for name, value in vars(jeff.errors).items()
              if isinstance(value, type) and issubclass(value, jeff.errors.JeffError)}
    assert errors == set(jeff.errors.__all__)
    for name in ("Engine", "EngineResult", "Graph", "GraphResult", "Skipped", "load_graph"):
        assert name in jeff.__all__ and hasattr(jeff, name)
    assert not hasattr(jeff, "Any")
    assert all(hasattr(jeff, name) for name in jeff.__all__)
