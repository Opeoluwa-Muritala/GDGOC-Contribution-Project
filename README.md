# GDG on Campus Contribution Form

FastAPI form for proposing edits to the GDG on Campus Unilorin Resource Hub. Each accepted submission creates a branch, commits the edited Markdown/MDX file, and opens a GitHub pull request.

## Local setup

1. Copy `.env.example` to `.env` and fill in the values.
2. Give the fine-grained GitHub token access to the target repository with:
   - **Contents:** Read and write
   - **Pull requests:** Read and write
3. Install and run:

```bash
python -m pip install -r requirements.txt
python main.py
```

Open `http://127.0.0.1:8000`.

## Deployment

Set `GITHUB_TOKEN`, `GITHUB_REPO`, and `GITHUB_BASE_BRANCH` as secret environment variables on the hosting platform. Do not deploy the local `.env` file.

Use this startup command if the platform does not run `main.py` directly:

```text
uvicorn main:app --host 0.0.0.0 --port $PORT
```

The health endpoint is `/healthz`.
