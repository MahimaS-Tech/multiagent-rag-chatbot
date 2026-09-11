import argparse
import asyncio
import json
import time
from pathlib import Path
from app.config import Settings
from app.database import Database
from app.repository import Repository
from app.providers import build_provider
from app.schemas import Principal, DocumentInput
from app.security import mint_demo_token
from app.worker import IngestionWorker, HEARTBEAT_FILE


async def operate(args, settings):
    db, provider = Database(settings), None
    try:
        if args.command == "migrate":
            await db.call(db.migrate)
            print("Database migration 1 applied or already present.")
            return
        await db.call(db.check_schema)
        repo = Repository(db)
        provider = build_provider(settings)
        if args.command == "worker":
            await IngestionWorker(db, repo, provider, settings).serve()
        elif args.command == "seed":
            principal = Principal(tenant_id=args.tenant, user_id="demo", roles=frozenset({"editor"}))
            for path in sorted((Path(__file__).parent.parent / "sample_data").glob("*.md")):
                data = DocumentInput(title=path.stem.replace("_", " ").title(), text=path.read_text(), visibility="tenant")
                print(json.dumps(await db.call(repo.submit_document, principal, data, settings.embedding_space)))
            print("Sample documents queued; the worker indexes them.")
    finally:
        if provider:
            await provider.close()
        await db.call(db.close)


def main():
    parser = argparse.ArgumentParser(description="Evidence-first RAG administration")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("migrate", "worker", "worker-health"):
        sub.add_parser(command)
    seed = sub.add_parser("seed"); seed.add_argument("--tenant", default="acme")
    token = sub.add_parser("token")
    token.add_argument("--tenant", default="acme"); token.add_argument("--user", default="demo")
    token.add_argument("--roles", default="user,editor,admin,operator")
    token.add_argument("--seconds", type=int, default=3600)
    args = parser.parse_args()
    if args.command == "worker-health":
        try:
            healthy = time.time() - float(HEARTBEAT_FILE.read_text()) < 30
        except (OSError, ValueError):
            healthy = False
        raise SystemExit(0 if healthy else 1)
    settings = Settings()
    if args.command == "token":
        if not 60 <= args.seconds <= 86400:
            parser.error("Token lifetime must be between 60 seconds and 24 hours")
        print(mint_demo_token(settings, args.tenant, args.user, args.roles.split(","), args.seconds))
    else:
        asyncio.run(operate(args, settings))


if __name__ == "__main__":
    main()
