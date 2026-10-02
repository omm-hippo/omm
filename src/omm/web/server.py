"""Loopback-only, same-origin management API and bundled browser interface."""
from __future__ import annotations

from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import secrets
from urllib.parse import parse_qs, urlsplit

from omm.web.jobs import JobConflict, JobManager
from omm.web.service import WebService
from omm import config
from omm.atomic import atomic_write_text

STATIC_ROOT = Path(__file__).with_name("static")
MAX_BODY = 64 * 1024


class WebServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port=0, *, service=None, jobs=None, static_root=STATIC_ROOT):
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("Invalid local port")
        self.service = service if service is not None else WebService()
        self.jobs = jobs if jobs is not None else JobManager()
        self.static_root = Path(static_root).resolve()
        self.session = secrets.token_urlsafe(32)
        self.instance = secrets.token_urlsafe(16)
        try:
            super().__init__(("127.0.0.1", port), Handler)
        except BaseException:
            self.jobs.close()
            raise
        self.cookie_name = f"omm_web_{self.server_port}"
        self.allowed_hosts = {f"127.0.0.1:{self.server_port}", f"localhost:{self.server_port}"}
        self.info_path = self.jobs.root / "server-info.json"
        try:
            atomic_write_text(self.info_path, json.dumps({"url": self.url, "instance": self.instance, "pid": os.getpid()}))
        except BaseException:
            self.server_close()
            raise

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_port}/"

    def server_close(self):
        if hasattr(self, "info_path") and self.info_path.exists() and not self.info_path.is_symlink():
            try:
                if json.loads(self.info_path.read_text()).get("instance") == self.instance:
                    self.info_path.unlink()
            except (OSError, ValueError):
                pass
        self.jobs.close()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    server_version = "OMMLocal"

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *_):
        pass

    def _send(self, status, body, content_type="application/json; charset=utf-8", *, bootstrap=False):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if bootstrap:
            self.send_header("Set-Cookie", f"{self.server.cookie_name}={self.server.session}; HttpOnly; SameSite=Strict; Path=/")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, data):
        self._send(status, json.dumps(data, ensure_ascii=False, allow_nan=False).encode())

    def _guard(self, *, session=True):
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1 or hosts[0] not in self.server.allowed_hosts:
            self._json(403, {"error": "Host is not this local OMM server"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin != "http://" + hosts[0]:
            self._json(403, {"error": "Cross-origin requests are not allowed"})
            return False
        site = self.headers.get("Sec-Fetch-Site")
        if site and site not in {"none", "same-origin"}:
            self._json(403, {"error": "Open OMM directly in a local browser tab"})
            return False
        if session:
            try:
                cookie = SimpleCookie(self.headers.get("Cookie", ""))
                value = cookie[self.server.cookie_name].value
            except (KeyError, ValueError, CookieError):
                value = ""
            if not secrets.compare_digest(value, self.server.session):
                self._json(401, {"error": "Open the local OMM page to start a session"})
                return False
        return True

    def do_OPTIONS(self):
        self._json(403, {"error": "Cross-origin API access is disabled"})

    def do_GET(self):
        parsed = urlsplit(self.path)
        is_root = parsed.path == "/"
        if not self._guard(session=not (is_root or parsed.path == "/api/identity")):
            return
        try:
            if parsed.path == "/api/identity":
                return self._json(200, {"kind": "omm-local-manager", "instance": self.server.instance})
            if parsed.path == "/api/hardware":
                return self._json(200, self.server.service.machine())
            if parsed.path == "/api/wiki":
                from omm import model_wiki
                return self._json(200, model_wiki.catalog())
            if parsed.path == "/api/models":
                return self._json(200, self.server.service.models())
            if parsed.path == "/api/recommendations":
                query = parse_qs(parsed.query, keep_blank_values=True)
                if set(query) - {"profile", "q"} or any(len(values) != 1 for values in query.values()):
                    raise ValueError("Unsupported query parameters")
                return self._json(200, self.server.service.recommendations(query.get("profile", ["balanced"])[0], query.get("q", [""])[0]))
            if parsed.path == "/api/jobs":
                return self._json(200, self.server.jobs.list())
            if parsed.path.startswith("/api/"):
                return self._json(404, {"error": "Unknown API route"})
            if parsed.path != "/" and not parsed.path.startswith("/assets/"):
                return self._json(404, {"error": "Unknown page"})
            relative = "index.html" if is_root else parsed.path.lstrip("/")
            path = (self.server.static_root / relative).resolve()
            if not path.is_relative_to(self.server.static_root) or not path.is_file():
                return self._json(404, {"error": "Web assets are missing. Build the web interface before packaging."})
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            return self._send(200, path.read_bytes(), content_type, bootstrap=is_root)
        except ValueError as error:
            self._json(400, {"error": str(error)})
        except Exception:
            self._json(500, {"error": "Could not read the local OMM state. Retry after checking the model files."})

    def do_POST(self):
        if not self._guard():
            return
        if self.headers.get("X-OMM-Web") != "1":
            return self._json(403, {"error": "Missing same-origin operation header"})
        if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self._json(415, {"error": "Use a bounded JSON request"})
        try:
            length = int(self.headers.get("Content-Length", "-1"))
            if not 0 <= length <= MAX_BODY:
                return self._json(413, {"error": "Request body is too large or has no length"})
            body = json.loads(self.rfile.read(length))
            path = urlsplit(self.path).path
            if path == "/api/catalog/refresh":
                if body != {}:
                    raise ValueError("Catalog refresh accepts no additional fields")
                return self._json(200, self.server.service.refresh_catalog())
            if path == "/api/jobs":
                request = self.server.service.request(body)
                return self._json(202, self.server.jobs.start(request, body.get("request_id")))
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "cancel":
                if body != {}:
                    raise ValueError("Cancellation accepts no additional fields")
                return self._json(200, self.server.jobs.cancel(parts[2]))
            self._json(404, {"error": "Unknown operation route"})
        except JobConflict as error:
            self._json(409, {"error": str(error)})
        except (ValueError, TypeError, UnicodeError) as error:
            self._json(400, {"error": str(error)[:500]})
        except Exception:
            self._json(500, {"error": "The operation could not be started. Check the model and job lists before retrying."})


def running_url():
    root = config.OMM_HOME / "web-jobs"
    path = root / "server-info.json"
    if root.is_symlink() or path.is_symlink():
        return None
    try:
        if path.stat().st_size > 1024:
            return None
        info = json.loads(path.read_text())
        url = urlsplit(info["url"])
        if url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port or url.username or url.password or url.path != "/" or url.query or url.fragment:
            return None
        import requests
        response = requests.get(info["url"] + "api/identity", timeout=2, allow_redirects=False)
        if response.status_code == 200 and response.json() == {"kind": "omm-local-manager", "instance": info["instance"]}:
            return info["url"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    except Exception:
        pass
    return None


def serve(port=0, *, open_browser=False):
    try:
        server = WebServer(port)
    except JobConflict:
        url = running_url()
        if url is None:
            raise ValueError("The local manager is running or shutting down, but its address could not be confirmed.") from None
        print(f"OMM local manager already running: {url}", flush=True)
        if open_browser:
            import webbrowser
            webbrowser.open(url)
        return
    print(f"OMM local manager: {server.url}", flush=True)
    print("Press Ctrl+C to stop. Model operations and files stay on this computer.", flush=True)
    if open_browser:
        import webbrowser
        webbrowser.open(server.url)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
