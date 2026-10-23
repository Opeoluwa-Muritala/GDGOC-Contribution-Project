package main

import (
	"bytes"
	"crypto/rand"
	"encoding/base64"
	"encoding/hex"
	_ "embed"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"regexp"
	"strings"
	"sync"
	"time"
	"unicode/utf8"
)

const githubAPI = "https://api.github.com"

var (
	token      = mustEnv("GITHUB_TOKEN")
	repo       = mustEnv("GITHUB_REPO")
	origin     = os.Getenv("ALLOWED_ORIGIN") // optional: only needed if the form is hosted elsewhere
	baseBranch = getEnv("GITHUB_BASE_BRANCH", "main")
	trustProxy = os.Getenv("TRUST_PROXY") == "true"
	pathRe     = regexp.MustCompile(`^[A-Za-z0-9_\-/]+\.mdx$`)
	httpClient = &http.Client{Timeout: 20 * time.Second}
	limiter    = newRateLimiter(5, time.Hour) // 5 submissions per IP per hour
)

//go:embed contribute.html
var formTemplate string

// Form served at "/", with the API and raw-file URLs filled in from config.
var formHTML = strings.NewReplacer(
	"__API_URL__", "/submit",
	"__RAW_BASE__", fmt.Sprintf("https://raw.githubusercontent.com/%s/%s/", repo, baseBranch),
).Replace(formTemplate)

type edit struct {
	Path    string `json:"path"`
	Content string `json:"content"`
	Summary string `json:"summary"`
	Name    string `json:"name"`
	Website string `json:"website"` // honeypot
}

func main() {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{"ok": true})
	})
	mux.HandleFunc("/submit", submit)
	mux.HandleFunc("/", formPage)

	port := getEnv("PORT", "8080")
	srv := &http.Server{
		Addr:              ":" + port,
		Handler:           cors(mux),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      45 * time.Second,
	}
	log.Printf("listening on :%s", port)
	log.Fatal(srv.ListenAndServe())
}

// ---------- handlers ----------

func formPage(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		http.NotFound(w, r)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	_, _ = io.WriteString(w, formHTML)
}

func submit(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		httpErr(w, http.StatusMethodNotAllowed, "POST only")
		return
	}
	if !limiter.allow(clientIP(r)) {
		httpErr(w, http.StatusTooManyRequests, "too many submissions, try again later")
		return
	}

	r.Body = http.MaxBytesReader(w, r.Body, 150_000)
	var e edit
	if err := json.NewDecoder(r.Body).Decode(&e); err != nil {
		httpErr(w, http.StatusBadRequest, "invalid JSON")
		return
	}

	if e.Website != "" { // bot: pretend success
		writeJSON(w, http.StatusOK, map[string]any{"ok": true})
		return
	}
	if msg := validate(&e); msg != "" {
		httpErr(w, http.StatusBadRequest, msg)
		return
	}

	prURL, err := openPR(e)
	if err != nil {
		log.Printf("openPR failed: %v", err)
		httpErr(w, http.StatusBadGateway, "could not create pull request")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "pr_url": prURL})
}

func validate(e *edit) string {
	if !pathRe.MatchString(e.Path) || strings.Contains(e.Path, "..") {
		return "invalid page path"
	}
	if strings.TrimSpace(e.Content) == "" || utf8.RuneCountInString(e.Content) > 100_000 {
		return "content must be 1-100000 characters"
	}
	e.Summary = strings.Join(strings.Fields(e.Summary), " ") // collapse newlines
	if e.Summary == "" || utf8.RuneCountInString(e.Summary) > 200 {
		return "summary must be 1-200 characters"
	}
	e.Name = strings.Join(strings.Fields(e.Name), " ")
	if e.Name == "" {
		e.Name = "Anonymous"
	}
	if utf8.RuneCountInString(e.Name) > 80 {
		return "name too long"
	}
	return ""
}

// ---------- GitHub flow ----------

func openPR(e edit) (string, error) {
	// 1. Latest commit on the base branch
	var ref struct {
		Object struct {
			SHA string `json:"sha"`
		} `json:"object"`
	}
	if err := gh("GET", fmt.Sprintf("/repos/%s/git/ref/heads/%s", repo, baseBranch), nil, &ref, 200); err != nil {
		return "", fmt.Errorf("get base ref: %w", err)
	}

	// 2. New branch
	branch := fmt.Sprintf("contrib/%d-%s", time.Now().Unix(), randHex(3))
	err := gh("POST", fmt.Sprintf("/repos/%s/git/refs", repo),
		map[string]string{"ref": "refs/heads/" + branch, "sha": ref.Object.SHA}, nil, 201)
	if err != nil {
		return "", fmt.Errorf("create branch: %w", err)
	}

	// Remove the branch again if anything below fails
	ok := false
	defer func() {
		if !ok {
			_ = gh("DELETE", fmt.Sprintf("/repos/%s/git/refs/heads/%s", repo, branch), nil, nil, 204)
		}
	}()

	// 3. Existing file SHA (needed to update; a 404 means a new page)
	body := map[string]string{
		"message": e.Summary,
		"content": base64.StdEncoding.EncodeToString([]byte(e.Content)),
		"branch":  branch,
	}
	var existing struct {
		SHA string `json:"sha"`
	}
	filePath := fmt.Sprintf("/repos/%s/contents/%s", repo, e.Path)
	status, err := ghStatus("GET", filePath+"?ref="+baseBranch, nil, &existing)
	switch {
	case err != nil:
		return "", fmt.Errorf("lookup file: %w", err)
	case status == 200:
		body["sha"] = existing.SHA
	case status != 404:
		return "", fmt.Errorf("lookup file: unexpected status %d", status)
	}

	// 4. Commit
	if err := gh("PUT", filePath, body, nil, 200, 201); err != nil {
		return "", fmt.Errorf("commit file: %w", err)
	}

	// 5. Pull request
	var pr struct {
		HTMLURL string `json:"html_url"`
	}
	err = gh("POST", fmt.Sprintf("/repos/%s/pulls", repo), map[string]string{
		"title": e.Summary,
		"head":  branch,
		"base":  baseBranch,
		"body":  fmt.Sprintf("Submitted via the contribution form by **%s**.\n\nPage: `%s`", e.Name, e.Path),
	}, &pr, 201)
	if err != nil {
		return "", fmt.Errorf("open PR: %w", err)
	}

	ok = true
	return pr.HTMLURL, nil
}

// gh calls the GitHub API and errors unless the status is in wantStatus.
func gh(method, path string, body, out any, wantStatus ...int) error {
	status, err := ghStatus(method, path, body, out)
	if err != nil {
		return err
	}
	for _, s := range wantStatus {
		if status == s {
			return nil
		}
	}
	return fmt.Errorf("%s %s: unexpected status %d", method, path, status)
}

// ghStatus calls the GitHub API and returns the status code; out is decoded on 2xx.
func ghStatus(method, path string, body, out any) (int, error) {
	var rdr io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return 0, err
		}
		rdr = bytes.NewReader(b)
	}
	req, err := http.NewRequest(method, githubAPI+path, rdr)
	if err != nil {
		return 0, err
	}
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("X-GitHub-Api-Version", "2022-11-28")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}

	resp, err := httpClient.Do(req)
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()

	if out != nil && resp.StatusCode >= 200 && resp.StatusCode < 300 {
		if err := json.NewDecoder(resp.Body).Decode(out); err != nil {
			return resp.StatusCode, err
		}
	} else {
		_, _ = io.Copy(io.Discard, resp.Body)
	}
	return resp.StatusCode, nil
}

// ---------- middleware and helpers ----------

func cors(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if origin != "" { // cross-origin hosting only; same-origin needs no CORS
			w.Header().Set("Access-Control-Allow-Origin", origin)
			w.Header().Set("Access-Control-Allow-Methods", "POST, OPTIONS")
			w.Header().Set("Access-Control-Allow-Headers", "Content-Type")
			w.Header().Set("Vary", "Origin")
			if r.Method == http.MethodOptions {
				w.WriteHeader(http.StatusNoContent)
				return
			}
		}
		next.ServeHTTP(w, r)
	})
}

type rateLimiter struct {
	mu     sync.Mutex
	hits   map[string][]time.Time
	limit  int
	window time.Duration
}

func newRateLimiter(limit int, window time.Duration) *rateLimiter {
	return &rateLimiter{hits: map[string][]time.Time{}, limit: limit, window: window}
}

func (l *rateLimiter) allow(key string) bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	now := time.Now()
	recent := l.hits[key][:0]
	for _, t := range l.hits[key] {
		if now.Sub(t) < l.window {
			recent = append(recent, t)
		}
	}
	if len(recent) >= l.limit {
		l.hits[key] = recent
		return false
	}
	l.hits[key] = append(recent, now)
	return true
}

func clientIP(r *http.Request) string {
	if trustProxy {
		if xff := r.Header.Get("X-Forwarded-For"); xff != "" {
			return strings.TrimSpace(strings.Split(xff, ",")[0])
		}
	}
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		return r.RemoteAddr
	}
	return host
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func httpErr(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]any{"ok": false, "error": msg})
}

func randHex(n int) string {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

func getEnv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func mustEnv(key string) string {
	v := os.Getenv(key)
	if v == "" {
		log.Fatalf("missing required env var %s", key)
	}
	return v
}
