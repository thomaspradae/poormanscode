from __future__ import annotations

POLICIES = {
    "unity": "Use the registered Unity Editor to create/open the project and save editor-owned assets. Never invent .unity scene YAML or ProjectSettings from memory.",
    "next": "Use create-next-app through the registered Node/npm toolchain, then run the real build and browser checks.",
    "vite": "Use the official Vite scaffolder through npm, then run the real build and browser checks.",
    "rust": "Use cargo init, then cargo build and cargo test.",
    "dotnet": "Use dotnet new, then dotnet build and dotnet test.",
    "python": "Use the registered Python environment and project tooling, then run the repository's real tests.",
}


def infer_stack(request: str) -> str | None:
    text = request.lower()
    if "unity" in text:
        return "unity"
    if any(x in text for x in ("next.js", "nextjs", "create-next-app")):
        return "next"
    if any(x in text for x in ("vite", "react website", "web app", "website")):
        return "vite"
    if any(x in text for x in ("rust", "cargo")):
        return "rust"
    if any(x in text for x in (".net", "dotnet", "asp.net")):
        return "dotnet"
    if any(x in text for x in ("python", "django", "fastapi", "flask")):
        return "python"
    return None


def bootstrap_guidance(request: str, skeletal: bool) -> str | None:
    stack = infer_stack(request)
    if not skeletal or not stack:
        return None
    return f"AUTHORITATIVE BOOTSTRAP POLICY ({stack}):\n{POLICIES[stack]}"
