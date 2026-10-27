# Website Health Monitor

A small web app, built with **Python 3.12+** and **FastAPI**, that checks whether a public website is online.

Open the app in your browser, type a website address such as `github.com`, and click **Check**. You'll see:

- whether the site is **online** or **offline**
- the **HTTP status code** (200, 404, 503, …)
- the **response time** in milliseconds
- the **error** that occurred, if any

## How it is built: front end + back end

```
 Browser                        FastAPI app                       The website
┌──────────────────┐  POST /check  ┌──────────────────────┐   GET   ┌──────────┐
│ Web page at "/"  │ ────────────► │ Checks the URL is    │ ──────► │ e.g.     │
│ (index.html)     │ ◄──────────── │ safe, then fetches it│ ◄────── │ github   │
│ input box + card │     JSON      │                      │         │          │
└──────────────────┘               └──────────────────────┘         └──────────┘
```

- **Front end:** `app/static/index.html`. One web page with an input box and a result card. It uses a little JavaScript to call the API and display the answer.
- **Back end:** `app/main.py`. A REST API that does the real work and returns JSON. Other tools (scripts, monitoring systems, CI pipelines) can call it directly, without the web page.

## Endpoints

| Method | Path      | Who it's for | What it does                                     |
|--------|-----------|--------------|--------------------------------------------------|
| GET    | `/`       | People       | The web page where you check a website           |
| POST   | `/check`  | Programs     | Checks whether the website you send is online    |
| GET    | `/health` | Machines     | This app's own health (`healthy`), for Docker, load balancers and other tools |
| GET    | `/api`    | Developers   | A welcome message for the API                    |
| GET    | `/docs`   | Developers   | Interactive API documentation, made automatically by FastAPI |

> **Tip:** `/docs` lists every endpoint plus technical details such as the curl command, response headers and schemas. It's a developer tool for testing the API. For everyday use, use the web page at `/`.

### Example: `POST /check`

Request body:

```json
{ "url": "https://example.com" }
```

Response:

```json
{
  "url": "https://example.com",
  "status": "online",
  "status_code": 200,
  "response_time_ms": 338.4,
  "error": null
}
```

When a check fails, `status` is `"offline"` and `error` explains why. For example: `"Request timed out after 5 seconds"`, `"DNS lookup failed for 'nosuchsite.com'"` or `"Connection failed: ..."`.

**What counts as "online"?** Any HTTP response below 500. A `404 Not Found` still means the web server is up and answering. Status 5xx (server errors), timeouts and connection failures count as `"offline"`.

### HTTP status codes returned by `/check`

| Code | Meaning                                                                 |
|------|-------------------------------------------------------------------------|
| 200  | The check ran. Read `status` to see if the site is online or offline.   |
| 400  | The URL points to a non-public destination (blocked for security).      |
| 422  | The input is invalid: missing `url`, malformed, or not `http(s)://`.     |

## Security: why some URLs are blocked

If an API fetches any URL a user sends, attackers can try to make it fetch **internal** addresses. Examples are `http://localhost`, your home router, or the cloud metadata service at `http://169.254.169.254`, which can leak cloud credentials. This attack is called **SSRF** (Server-Side Request Forgery).

This app defends against SSRF in these ways:

1. **Only `http://` and `https://`** URLs are accepted. URLs with embedded passwords are rejected.
2. **Every IP address is checked.** The app looks up the hostname's IP addresses itself and refuses the request if *any* of them is non-public. Non-public includes loopback, private ranges, link-local/metadata, reserved ranges and multicast.
3. **IP pinning.** The app connects to the exact IP address it checked. A malicious DNS server therefore can't give a safe answer first and a private one a moment later (a "DNS rebinding" attack). For HTTPS, the certificate is still verified against the real hostname.
4. **Safe redirects.** The app follows redirects itself, at most 5, and repeats checks 1–3 for every hop.
5. **Proxy variables are ignored.** Settings such as `HTTP_PROXY` can't send requests elsewhere.

## Project structure

```
website-health-monitor/
├── app/
│   ├── __init__.py        # Marks "app" as a Python package
│   ├── main.py            # The FastAPI application (back end)
│   └── static/
│       └── index.html     # The web page (front end)
├── tests/
│   └── test_main.py       # Automated tests (no internet needed)
├── requirements.txt       # Packages needed to run the app
├── requirements-dev.txt   # Extra packages for testing and linting
├── pyproject.toml         # Project metadata + pytest and Ruff settings
├── .gitignore             # Files Git should not track
└── README.md              # This file
```

## Local setup

You need **Python 3.12 or newer**. To check your version:

```bash
python --version
```

### 1. Create and activate a virtual environment

A virtual environment is a private folder of packages for this project only.

**Windows (PowerShell):**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

> If PowerShell says running scripts is disabled, run this once:
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

**macOS / Linux:**

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Your prompt now starts with `(.venv)`.

> **Moved or renamed the project folder?** Virtual environments can't be moved, because they store their own folder path inside them. Delete `.venv` and create it again with the commands above. First stop any server that is still running from it (`Ctrl+C`), or Windows won't let you delete the files.

### 2. Install the dependencies

```bash
python -m pip install -r requirements-dev.txt
```

(To *run* the app only, without the testing tools, `requirements.txt` is enough.)

> **Why `python -m ...`?** This README runs every tool as `python -m pip`, `python -m uvicorn` and so on, instead of plain `pip` or `uvicorn`. Plain commands start small `.exe` launcher files inside `.venv\Scripts`, and Windows security features such as Smart App Control can block those. Running them through `python -m` avoids the problem and works the same on every operating system.

## Running the application

```bash
python -m uvicorn app.main:app --reload
```

- `app.main:app` means "the `app` object inside `app/main.py`".
- `--reload` restarts the server when you save a file (for development only).

Then open **http://127.0.0.1:8000/** in your browser, type a website (for example `github.com`), and click **Check**. If you leave out `https://`, the page adds it for you.

Other useful addresses:

- http://127.0.0.1:8000/health shows the app's own health status
- http://127.0.0.1:8000/docs is the interactive API documentation for developers

You can also call the API directly from the command line, without the web page.

**macOS / Linux / Git Bash:**

```bash
curl -X POST http://127.0.0.1:8000/check \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com"}'
```

**Windows PowerShell:**

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/check `
  -ContentType "application/json" -Body '{"url": "https://example.com"}'
```

Press `Ctrl+C` to stop the server.

## Running the tests

```bash
python -m pytest
```

For more detail, add `-v`:

```bash
python -m pytest -v
```

The tests never use the real internet:

- **DNS lookups** are replaced with a small dictionary (the `fake_dns` fixture).
- **HTTP requests** go to `httpx.MockTransport`, which calls a Python function you write instead of a real website (the `mock_website` fixture).

This makes the tests fast and reliable. They give the same result with or without Wi-Fi, which matters later when they run in a CI pipeline.

## Linting and formatting with Ruff

```bash
python -m ruff check .            # find problems (unused imports, bugs, security issues)
python -m ruff check . --fix      # automatically fix what can be fixed
python -m ruff format .           # format the code consistently
python -m ruff format --check .   # check formatting without changing files
```

The rules Ruff uses are configured in `pyproject.toml` under `[tool.ruff]`. Ruff checks Python files only, not `index.html`.

## Configuration values

These constants are at the top of `app/main.py`:

| Constant                  | Default | Meaning                          |
|---------------------------|---------|----------------------------------|
| `REQUEST_TIMEOUT_SECONDS` | `5.0`   | Timeout for DNS and HTTP steps   |
| `MAX_REDIRECTS`           | `5`     | Redirects followed before giving up |
