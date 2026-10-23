# Contribution Backend (Go)

Serves the contribution form at `/` and opens GitHub pull requests from `POST /submit`.
The form is embedded in the binary (`contribute.html`), so deploy a single file.
Link doc pages to `https://YOUR-HOST/?page=<page-path>`.

## Run
```bash
export $(grep -v '^#' .env.example | xargs)   # edit values first
go run .
```

## Test
```bash
curl -X POST localhost:8080/submit -H "Content-Type: application/json" \
  -d '{"path":"careers/cv-tips.mdx","content":"---\ntitle: \"CV Tips\"\n---\n\nHello","summary":"Test edit","name":"Ada"}'
```

## Build
```bash
go build -o contribution-backend .
```

Standard library only, no dependencies.
