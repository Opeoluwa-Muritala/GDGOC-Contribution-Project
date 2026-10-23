import utils.http_session
import utils.transport
import utils.session
import base64
import logging
import os
import re
import secrets
import sys
import time
from pathlib import Path
from threading import Lock

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)

# ---------- config ----------
GITHUB_API = "https://api.github.com"


def must_env(key: str) -> str:
    v = os.getenv(key, "")
    if not v:
        sys.exit(f"missing required env var {key}")
    return v


TOKEN = must_env("GITHUB_TOKEN")
REPO = must_env("GITHUB_REPO")
ORIGIN = os.getenv("ALLOWED_ORIGIN", "")
BASE = os.getenv("GITHUB_BASE_BRANCH", "main")
TRUST_PROXY = os.getenv("TRUST_PROXY") == "true"
PATH_RE = re.compile(r"^[A-Za-z0-9_\-/]+\.mdx$")

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

FORM_HTML = (
    (Path(__file__).parent / "contribute.html")
    .read_text(encoding="utf-8")
    .replace("__API_URL__", "/submit")
    .replace("__RAW_BASE__", f"https://raw.githubusercontent.com/{REPO}/{BASE}/")
)


# ---------- model ----------
class Edit(BaseModel):
    path: str
    content: str
    summary: str
    name: str = ""
    website: str = ""  # honeypot


# ---------- rate limiter ----------
class RateLimiter:
    def __init__(self, limit: int, window_seconds: float):
        self.limit = limit
        self.window = window_seconds
        self.hits: dict[str, list[float]] = {}
        self.lock = Lock()

    def allow(self, key: str) -> bool:
        with self.lock:
            now = time.time()
            recent = [t for t in self.hits.get(key, []) if now - t < self.window]
            if len(recent) >= self.limit:
                self.hits[key] = recent
                return False
            recent.append(now)
            self.hits[key] = recent
            return True


limiter = RateLimiter(5, 3600)  # 5 submissions per IP per hour


# ---------- helpers ----------
def client_ip(request: Request) -> str:
    if TRUST_PROXY:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def err(msg: str, status: int) -> JSONResponse:
    return JSONResponse({"ok": False, "error": msg}, status_code=status)


def validate(e: Edit) -> str:
    if not PATH_RE.match(e.path) or ".." in e.path:
        return "invalid page path"
    if not e.content.strip() or len(e.content) > 100_000:
        return "content must be 1-100000 characters"
    e.summary = " ".join(e.summary.split())
    if not e.summary or len(e.summary) > 200:
        return "summary must be 1-200 characters"
    e.name = " ".join(e.name.split()) or "Anonymous"
    if len(e.name) > 80:
        return "name too long"
    return ""


# ---------- github ----------
def gh(client: httpx.Client, method: str, path: str, json=None, ok=(200,)) -> httpx.Response:
    resp = client.request(method, GITHUB_API + path, json=json, headers=HEADERS)
    if resp.status_code not in ok:
        raise RuntimeError(f"{method} {path}: unexpected status {resp.status_code}")
    return resp


def open_pr(e: Edit) -> str:
    with httpx.Client(timeout=20) as client:
        # 1. latest commit on base branch
        r = gh(client, "GET", f"/repos/{REPO}/git/ref/heads/{BASE}")
        base_sha = r.json()["object"]["sha"]

        # 2. new branch
        branch = f"contrib/{int(time.time())}-{secrets.token_hex(3)}"
        gh(
            client,
            "POST",
            f"/repos/{REPO}/git/refs",
            json={"ref": f"refs/heads/{branch}", "sha": base_sha},
            ok=(201,),
        )

        try:
            # 3. existing file sha (404 = new page)
            file_path = f"/repos/{REPO}/contents/{e.path}"
            body = {
                "message": e.summary,
                "content": base64.b64encode(e.content.encode("utf-8")).decode(),
                "branch": branch,
            }
            r = client.get(GITHUB_API + file_path, params={"ref": BASE}, headers=HEADERS)
            if r.status_code == 200:
                body["sha"] = r.json()["sha"]
            elif r.status_code != 404:
                raise RuntimeError(f"lookup file: unexpected status {r.status_code}")

            # 4. commit
            gh(client, "PUT", file_path, json=body, ok=(200, 201))

            # 5. pull request
            r = gh(
                client,
                "POST",
                f"/repos/{REPO}/pulls",
                json={
                    "title": e.summary,
                    "head": branch,
                    "base": BASE,
                    "body": f"Submitted via the contribution form by **{e.name}**.\n\nPage: `{e.path}`",
                },
                ok=(201,),
            )
            return r.json()["html_url"]
        except Exception:
            # remove the orphan branch, then re-raise
            try:
                gh(client, "DELETE", f"/repos/{REPO}/git/refs/heads/{branch}", ok=(204,))
            except Exception:
                pass
            raise


# ---------- app ----------
app = FastAPI()

if ORIGIN:  # cross-origin hosting only
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[ORIGIN],
        allow_methods=["POST", "OPTIONS"],
        allow_headers=["Content-Type"],
    )


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def form_page():
    return FORM_HTML


@app.post("/submit")
def submit(e: Edit, request: Request):  # plain def: blocking httpx runs in threadpool
    if int(request.headers.get("content-length") or 0) > 150_000:
        return err("payload too large", 413)
    if not limiter.allow(client_ip(request)):
        return err("too many submissions, try again later", 429)
    if e.website:  # bot: pretend success
        return {"ok": True}
    msg = validate(e)
    if msg:
        return err(msg, 400)
    try:
        return {"ok": True, "pr_url": open_pr(e)}
    except Exception as ex:
        logging.error("open_pr failed: %s", ex)
        return err("could not create pull request", 502)
