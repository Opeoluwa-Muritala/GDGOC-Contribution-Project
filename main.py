import base64
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from form import FORM_TEMPLATE

GITHUB_API = "https://api.github.com"
TOKEN = os.environ.get("GITHUB_TOKEN", "")
BASE = os.environ.get("GITHUB_BASE_BRANCH", "main")
ORIGIN = os.environ.get("ALLOWED_ORIGIN", "")  # optional: only if the form is hosted elsewhere
PATH_RE = re.compile(r"^[A-Za-z0-9_\-/]+\.mdx$")
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
    """Call the GitHub API. Returns (status, json). Never raises on HTTP errors."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        GITHUB_API + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "contribution-backend",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, None


def expect(status, *ok, step=""):
    if status not in ok:
        print(f"GitHub step failed: {step} -> {status}")  # shows in Vercel logs
        raise HTTPException(502, "Could not create pull request")


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
