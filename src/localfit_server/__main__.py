from __future__ import annotations

import os


def main() -> None:
    try:
        import uvicorn
    except ModuleNotFoundError as error:
        raise SystemExit(
            "Install the self-hosted server dependencies with: "
            "pip install 'omm-model[server]'"
        ) from error

    uvicorn.run(
        "localfit_server.app:app",
        host=os.getenv("LOCALFIT_HOST", "127.0.0.1"),
        port=int(os.getenv("LOCALFIT_PORT", "8000")),
    )


if __name__ == "__main__":
    main()
