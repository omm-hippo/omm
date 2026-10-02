"""FastAPI application for omm GUI - web interface for core omm commands."""

from __future__ import annotations

import json
import subprocess
import sys
import jinja2
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# Add omm to path so we can import its modules
OMM_SRC = Path(__file__).parent.parent
sys.path.insert(0, str(OMM_SRC))

# Import omm modules
from omm import benchmark as benchmark_mod
from omm import catalog
from omm import config as config_mod
from omm import doctor as doctor_mod
from omm import linker
from omm import predictor
from omm import recommend_ui
from omm import registry
from omm import search as search_mod
from omm.cli_views import print_scan
from omm.config import MODELS_DIR, OMM_HOME, load_config
from omm.hardware import scan_hardware, calculate_memory_budget
from omm.hub import resolve_model, parse_model_ref
from omm.quality import load_pack
from omm.recommend_selection import quantization_label
from omm.scan_import import find_external_model_identities

app = FastAPI(
    title="omm GUI",
    description="Web-based interface for core omm commands",
    version="0.1.0",
    docs_url="/api/docs",
    redoc_url=None,
)

# Templates and static files
templates_dir = Path(__file__).parent / "templates"
static_dir = Path(__file__).parent / "static"
templates_dir.mkdir(exist_ok=True)
static_dir.mkdir(exist_ok=True)

# Use direct Jinja2 environment to avoid Starlette 1.7 + Jinja2 3.1 cache bug
_jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(templates_dir.resolve())),
    autoescape=jinja2.select_autoescape(['html', 'xml']),
)
app.mount("/static", StaticFiles(directory=str(static_dir.resolve())), name="static")


# --- Pydantic Models ---

class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=200)
    limit: int = Field(20, ge=1, le=100)


class InstallRequest(BaseModel):
    model: str = Field(..., min_length=1)
    quant: Optional[str] = None
    yes: bool = False


class RunRequest(BaseModel):
    model: str = Field(..., min_length=1)
    engine: Optional[str] = None
    args: list[str] = []


class BenchmarkRequest(BaseModel):
    models: list[str] = Field(..., min_length=1)
    engine: Optional[str] = None
    profile: str = "balanced"


class TuneRequest(BaseModel):
    model: str = Field(..., min_length=1)
    profile: str = "balanced"
    purpose: Optional[str] = None


class RecommendRequest(BaseModel):
    profile: str = "balanced"
    purpose: Optional[str] = None
    json: bool = False


# --- Helper Functions ---

def run_omm_command(args: list[str], json_output: bool = False) -> dict[str, Any]:
    """Run an omm command and return the result."""
    cmd = [sys.executable, "-m", "omm"] + args
    if json_output:
        cmd.append("--json")
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=OMM_SRC)
    if json_output:
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return {"error": result.stderr or result.stdout, "exit_code": result.returncode}
    return {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.returncode,
    }


def get_hardware_info() -> dict[str, Any]:
    """Get hardware information."""
    info = scan_hardware()
    budget = calculate_memory_budget(info)
    return {
        "os": f"{info.os_name} {info.os_version}",
        "cpu": info.cpu,
        "ram_total_gb": round(info.ram_total_gb, 1),
        "ram_available_gb": round(info.ram_available_gb, 1),
        "model_budget_gb": round(budget.model_budget_gb, 1),
        "gpu_name": info.gpu_name,
        "unified_memory": info.unified_memory,
        "vram_total_gb": round(info.vram_total_gb, 1) if info.vram_total_gb else None,
        "vram_free_gb": round(info.vram_free_gb, 1) if info.vram_free_gb else None,
    }


def get_installed_models() -> list[dict[str, Any]]:
    """Get list of installed models."""
    reg = registry.load_registry()
    models = []
    for filename, entry in reg.items():
        models.append({
            "filename": filename,
            "size_gb": round(entry.get("size_bytes", 0) / (1024**3), 2),
            "parameter_count_b": entry.get("parameter_count_b"),
            "quant_bits": entry.get("quant_bits"),
            "engines": [name for name, on in entry.get("linked", {}).items() if on],
            "source": entry.get("source", "unknown"),
        })
    return models


def get_external_models() -> list[dict[str, Any]]:
    """Get models found outside the omm hub."""
    external = find_external_model_identities()
    return [
        {
            "filename": item.display_name,
            "path": str(item.path),
            "engine": item.engine,
            "size_gb": round(item.size_bytes / (1024**3), 2) if item.size_bytes else None,
        }
        for item in external
    ]


# --- API Routes ---

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Main GUI page."""
    template = _jinja_env.get_template("index.html")
    return HTMLResponse(template.render(request=request))


@app.get("/api/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok", "version": "0.1.0"}


@app.get("/api/hardware")
async def hardware():
    """Get hardware information."""
    return get_hardware_info()


@app.get("/api/models")
async def models():
    """Get installed and external models."""
    return {
        "installed": get_installed_models(),
        "external": get_external_models(),
    }


@app.get("/api/engines")
async def engines():
    """Get available engines and their installation status."""
    engine_specs = []
    for spec in linker.ENGINES:
        installed = linker.is_engine_installed(spec.key)
        engine_specs.append({
            "key": spec.key,
            "label": spec.label,
            "installed": installed,
        })
    return {"engines": engine_specs}


@app.post("/api/search")
async def search(request: SearchRequest):
    """Search for models."""
    # Use omm's search module directly
    try:
        config = load_config()
        pool = search_mod.local_candidate_pool(
            config.get("model_url"),
            manifest_url=config.get("catalog_manifest_url"),
            public_key=config.get("catalog_public_key"),
        )
        local_matches = search_mod.match_candidates(pool, request.query)
        
        # Also search HuggingFace
        hf_matches = search_mod.search_huggingface(request.query, limit=request.limit)
        local_repo_ids = {c.get("repo_id") for c in local_matches if c.get("repo_id")}
        hf_matches = [c for c in hf_matches if c.get("repo_id") not in local_repo_ids]
        
        combined = search_mod.dedupe_by_base_repo(local_matches + hf_matches)
        combined = combined[:request.limit]
        return {"results": combined}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/install")
async def install(request: InstallRequest):
    """Install a model."""
    args = ["install", request.model]
    if request.quant:
        args.extend(["--quant", request.quant])
    if request.yes:
        args.append("--yes")
    result = run_omm_command(args, json_output=True)
    if result.get("exit_code", 0) != 0:
        raise HTTPException(status_code=400, detail=result.get("error", "Install failed"))
    return result


@app.post("/api/uninstall")
async def uninstall(model: str = Form(...), yes: bool = Form(False)):
    """Uninstall a model."""
    args = ["uninstall", model]
    if yes:
        args.append("--yes")
    result = run_omm_command(args, json_output=True)
    if result.get("exit_code", 0) != 0:
        raise HTTPException(status_code=400, detail=result.get("error", "Uninstall failed"))
    return result


@app.post("/api/run")
async def run_model(request: RunRequest):
    """Run a model."""
    args = ["run", request.model]
    if request.engine:
        args.extend(["--engine", request.engine])
    if request.args:
        args.extend(["--"] + request.args)
    result = run_omm_command(args)
    return result


@app.post("/api/fit")
async def fit(model: str = Form(...), profile: str = Form("balanced")):
    """Check if model fits in memory."""
    args = ["fit", model, "--profile", profile]
    result = run_omm_command(args, json_output=True)
    if result.get("exit_code", 0) != 0:
        raise HTTPException(status_code=400, detail=result.get("error", "Fit check failed"))
    return result


@app.post("/api/recommend")
async def recommend(request: RecommendRequest):
    """Get model recommendations."""
    args = ["recommend", "--profile", request.profile]
    if request.purpose:
        args.extend(["--for", request.purpose])
    if request.json:
        args.append("--json")
    result = run_omm_command(args, json_output=request.json)
    return result


@app.post("/api/tune")
async def tune(request: TuneRequest):
    """Tune a model."""
    args = ["tune", request.model, "--profile", request.profile]
    if request.purpose:
        args.extend(["--for", request.purpose])
    result = run_omm_command(args, json_output=True)
    if result.get("exit_code", 0) != 0:
        raise HTTPException(status_code=400, detail=result.get("error", "Tune failed"))
    return result


@app.post("/api/benchmark")
async def benchmark(request: BenchmarkRequest):
    """Benchmark models."""
    args = ["benchmark", "--profile", request.profile]
    if request.engine:
        args.extend(["--engine", request.engine])
    args.extend(request.models)
    result = run_omm_command(args, json_output=True)
    if result.get("exit_code", 0) != 0:
        raise HTTPException(status_code=400, detail=result.get("error", "Benchmark failed"))
    return result


@app.post("/api/scan")
async def scan():
    """Scan hardware and models."""
    result = run_omm_command(["scan", "--json"], json_output=True)
    return result


@app.post("/api/doctor")
async def doctor():
    """Run system diagnostics."""
    result = run_omm_command(["doctor", "--json"], json_output=True)
    return result


@app.post("/api/update")
async def update():
    """Update omm."""
    result = run_omm_command(["update", "--json"], json_output=True)
    return result


@app.get("/api/settings")
async def get_settings():
    """Get current settings."""
    config = load_config()
    # Return only safe settings
    safe_keys = [
        "theme", "telemetry_send_policy", "telemetry_endpoint",
        "catalog_manifest_url", "catalog_public_key",
        "model_url", "auto_import", "storage_saved_bytes",
    ]
    return {k: config.get(k) for k in safe_keys if k in config}


@app.post("/api/settings")
async def set_settings(settings: dict[str, Any]):
    """Update settings."""
    config = load_config()
    config.update(settings)
    config_mod.update_config(config)
    return {"status": "ok"}


@app.get("/api/quality-packs")
async def quality_packs():
    """Get available quality packs."""
    pack, _ = load_pack()
    return {
        "packs": [
            {
                "id": pack.get("pack_id"),
                "name": pack.get("pack_id"),
                "description": pack.get("description"),
                "task_types": [item.get("category") for item in pack.get("items", [])],
            }
        ]
    }


# --- CLI Command to start the GUI server ---

def main(host: str = "127.0.0.1", port: int = 8000, open_browser: bool = True):
    """Start the omm GUI server."""
    import uvicorn
    import webbrowser
    import threading
    import time

    url = f"http://{host}:{port}"

    def open_browser_later():
        time.sleep(1.5)
        webbrowser.open(url)

    if open_browser:
        threading.Thread(target=open_browser_later, daemon=True).start()

    print(f"Starting omm GUI at {url}")
    print("Press Ctrl+C to stop")

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="omm GUI Server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    main(args.host, args.port, not args.no_open)