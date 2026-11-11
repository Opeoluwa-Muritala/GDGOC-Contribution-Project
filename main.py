import base64
import os
import re
import secrets
import threading
import time
import urllib.parse
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from form import FORM_TEMPLATE

GITHUB_API = "https://api.github.com"
LOGO_PATH = Path(__file__).with_name("gdg-logo-transparent.png")


def load_local_env():
    """Load local development settings without overriding deployment variables."""
    env_file = Path(__file__).with_name(".env")
    if not env_file.is_file():
        return
    for raw_line in env_file.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if value[:1] == value[-1:] and value[:1] in {'"', "'"}:
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


load_local_env()

TOKEN = os.environ.get("GITHUB_TOKEN", "")
BASE = os.environ.get("GITHUB_BASE_BRANCH", "main")
ORIGIN = os.environ.get("ALLOWED_ORIGIN", "")  # optional: only if the form is hosted elsewhere
PATH_RE = re.compile(r"^[A-Za-z0-9_\-/]+\.mdx?$")
MAX_BODY = 150_000

# Accepts "owner/name" or a full URL like https://github.com/owner/name(.git)
REPO = os.environ.get("GITHUB_REPO", "").strip().rstrip("/")
REPO = re.sub(r"^(https?://)?(www\.)?github\.com/", "", REPO)
REPO = re.sub(r"\.git$", "", REPO)

# Vercel sits behind a proxy, so trust X-Forwarded-For there
TRUST_PROXY = os.environ.get("TRUST_PROXY", "true" if os.environ.get("VERCEL") else "false") == "true"

# Form page with the API and raw-file URLs filled in from config
FORM_HTML = FORM_TEMPLATE.replace("__API_URL__", "/submit").replace(
    "__RAW_BASE__", f"https://raw.githubusercontent.com/{REPO}/{BASE}/"
)

app = FastAPI()

if ORIGIN:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[ORIGIN],
        allow_methods=["POST"],
        allow_headers=["Content-Type"],
    )


# Errors use {"ok": false, "error": "..."} so the form can show them
@app.exception_handler(StarletteHTTPException)
async def http_error(_, exc):
    return JSONResponse({"ok": False, "error": str(exc.detail)}, status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def validation_error(_, exc):
    return JSONResponse({"ok": False, "error": "Invalid input, check the field lengths"}, status_code=400)


# ---------- rate limiting (5 submissions per IP per hour) ----------
# Best effort on Vercel: memory is per serverless instance and resets on cold starts.

RATE_LIMIT, RATE_WINDOW = 5, 3600
_hits = {}
_lock = threading.Lock()


def allow(key):
    now = time.time()
    with _lock:
        recent = [t for t in _hits.get(key, []) if now - t < RATE_WINDOW]
        if len(recent) >= RATE_LIMIT:
            _hits[key] = recent
            return False
        recent.append(now)
        _hits[key] = recent
        return True


def client_ip(request: Request):
    if TRUST_PROXY:
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ---------- GitHub ----------

def gh(method, path, body=None):
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "contribution-backend",
    }
    try:
        response = httpx.request(
            method,
            GITHUB_API + path,
            json=body,
            headers=headers,
            timeout=20,
            follow_redirects=False,
        )
        try:
            payload = response.json() if response.content else None
        except ValueError:
            payload = None
        return response.status_code, payload
    except httpx.RequestError as exc:
        print(f"GitHub network error: {type(exc).__name__}: {exc}")
        return 0, None


def expect(status, *ok, step="", resp=None):
    if status not in ok:
        msg = resp.get("message") if isinstance(resp, dict) else ""
        print(f"GitHub step failed: {step} -> {status} {msg} repo={REPO} base={BASE}")
        if status == 0:
            raise HTTPException(503, "GitHub is temporarily unreachable")
        raise HTTPException(502, f"Could not create pull request ({step})")
    
# ---------- routes ----------

class Edit(BaseModel):
    path: str
    content: str = Field(max_length=100_000)
    summary: str = Field(max_length=200)
    name: str = Field(default="Anonymous", max_length=80)
    website: str = ""  # honeypot


@app.get("/", response_class=HTMLResponse)
def form_page():
    return FORM_HTML


@app.get("/gdg-logo-transparent.png", include_in_schema=False)
def gdg_logo():
    return FileResponse(
        LOGO_PATH,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/healthz")
def healthz():
    return {"ok": True, "configured": bool(TOKEN and REPO)}


@app.post("/submit")
def submit(edit: Edit, request: Request):
    if not allow(client_ip(request)):
        raise HTTPException(429, "Too many submissions, try again later")
    if int(request.headers.get("content-length") or 0) > MAX_BODY:
        raise HTTPException(413, "Request too large")

    if edit.website:
        return {"ok": True}  # bot: pretend success

    if not (TOKEN and REPO):
        raise HTTPException(500, "Server is not configured")
    if not PATH_RE.match(edit.path) or ".." in edit.path:
        raise HTTPException(400, "Invalid page path")
    if not edit.content.strip():
        raise HTTPException(400, "Content is empty")

    summary = " ".join(edit.summary.split())
    name = " ".join(edit.name.split()) or "Anonymous"
    if not summary:
        raise HTTPException(400, "Summary is required")

    # 1. Latest commit on the base branch
    status, ref = gh("GET", f"/repos/{REPO}/git/ref/heads/{BASE}")
    expect(status, 200, step="get base ref")
    base_sha = ref["object"]["sha"]

    # 2. New branch
    branch = f"contrib/{int(time.time())}-{secrets.token_hex(3)}"
    status, _ = gh(
        "POST",
        f"/repos/{REPO}/git/refs",
        {"ref": f"refs/heads/{branch}", "sha": base_sha},
    )
    expect(status, 201, step="create branch")

    try:
        # 3. Existing file SHA (needed to update; 404 means a new page)
        file_path = f"/repos/{REPO}/contents/{urllib.parse.quote(edit.path)}"
        status, existing = gh("GET", f"{file_path}?ref={urllib.parse.quote(BASE)}")
        body = {
            "message": summary,
            "content": base64.b64encode(edit.content.encode()).decode(),
            "branch": branch,
        }
        if status == 200:
            body["sha"] = existing["sha"]
        else:
            expect(status, 404, step="lookup file")

        # 4. Commit
        status, _ = gh("PUT", file_path, body)
        expect(status, 200, 201, step="commit file")

        # 5. Pull request
        status, pr = gh(
            "POST",
            f"/repos/{REPO}/pulls",
            {
                "title": summary,
                "head": branch,
                "base": BASE,
                "body": f"Submitted via the contribution form by **{name}**.\n\nPage: `{edit.path}`",
            },
        )
        expect(status, 201, step="open PR")
    except Exception:
        gh("DELETE", f"/repos/{REPO}/git/refs/heads/{branch}")  # clean up
        raise

    return {"ok": True, "pr_url": pr["html_url"]}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
