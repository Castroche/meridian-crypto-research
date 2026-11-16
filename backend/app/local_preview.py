"""Run the local acceptance edition with its isolated archive and no external delivery."""
import argparse
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8115)
    parser.add_argument("--scheduled", action="store_true", help="Run local issue and collection jobs")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    os.environ["MERIDIAN_DATA_DIR"] = str(root / "output" / "acceptance" / "desk")
    os.environ["MERIDIAN_PUBLIC_BASE_URL"] = f"http://127.0.0.1:{args.port}"
    os.environ["MERIDIAN_SCHEDULER_ENABLED"] = str(int(args.scheduled))
    if os.getenv("VERCEL"):
        raise SystemExit("Local preview needs a local environment")
    for name in ("MERIDIAN_WEBHOOK_URLS","MERIDIAN_TELEGRAM_BOT_TOKEN","MERIDIAN_TELEGRAM_CHAT_ID"):
        os.environ.pop(name,None)
    if args.scheduled:
        secrets_file = root / ".env.deploy.local"
        if not os.getenv("DEEPSEEK_API_KEY") and secrets_file.exists():
            for line in secrets_file.read_text("utf-8-sig").splitlines():
                if line.startswith("DEEPSEEK_API_KEY="):
                    os.environ["DEEPSEEK_API_KEY"] = line.partition("=")[2].strip().strip("\"'")
                    break
        if os.getenv("DEEPSEEK_API_KEY"):
            os.environ["MERIDIAN_EDITOR_ENABLED"] = "1"
            os.environ.setdefault("MERIDIAN_ARTICLE_MODEL", "deepseek-v4-pro")
    import uvicorn
    from .main import app
    from .config import settings
    if not settings.admin_token:
        from secrets import token_urlsafe
        origin = f"http://127.0.0.1:{args.port}"
        name = f"meridian_preview_{args.port}"
        secret = token_urlsafe(32)
        app.state.library_preview = (origin, name, secret)

        @app.middleware("http")
        async def preview_session(request, call_next):
            response = await call_next(request)
            if (request.method == "GET" and request.url.path in {"/", "/insights", "/reports", "/terminal"}
                    and str(request.base_url).rstrip("/") == origin and request.client
                    and request.client.host == "127.0.0.1"):
                response.set_cookie(name, secret, httponly=True, samesite="strict", path="/api")
                response.headers["Cache-Control"] = "no-store"
            return response

    uvicorn.run(app, host="127.0.0.1", port=args.port, proxy_headers=False)


if __name__ == "__main__":
    main()

