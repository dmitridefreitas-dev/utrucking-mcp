"""Pytest bootstrap: stub the web deps so main.py imports as pure Python (no server, no network).
Lives at the repo root so `import main / engines / analytics` resolves for the tests/ package."""
import sys, types


def _stub_web_deps():
    if "mcp.server.fastmcp" in sys.modules:
        return

    def _mod(name):
        m = types.ModuleType(name)
        sys.modules[name] = m
        return m

    _mod("httpx").AsyncClient = object
    _mod("mcp")
    _mod("mcp.server")
    fm = _mod("mcp.server.fastmcp")

    class _FastMCP:
        def __init__(self, *a, **k):
            pass

        def custom_route(self, *a, **k):
            def deco(fn):
                return fn
            return deco

        def tool(self, *a, **k):
            def deco(fn):
                return fn
            return deco

        def streamable_http_app(self):
            router = types.SimpleNamespace(lifespan_context=(lambda app: None))
            return types.SimpleNamespace(router=router, add_middleware=lambda *a, **k: None)

    fm.FastMCP = _FastMCP
    sr = _mod("starlette.responses")
    sr.JSONResponse = lambda *a, **k: ("JSON", a, k)
    sr.HTMLResponse = lambda *a, **k: ("HTML",)
    _mod("starlette.requests").Request = object
    _mod("starlette")
    _mod("starlette.middleware")
    _mod("starlette.middleware.cors").CORSMiddleware = object
    _mod("starlette.middleware.trustedhost").TrustedHostMiddleware = object


_stub_web_deps()
