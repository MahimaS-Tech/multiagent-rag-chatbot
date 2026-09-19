"""Create development configuration without installing any Python dependencies."""
import argparse
import getpass
import os
from pathlib import Path
import secrets


def write_private(path, content):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(content)
    os.chmod(path, 0o600)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Create a new configuration with a real model API key")
    parser.add_argument("--enable-live", action="store_true", help="Change only PROVIDER and OPENAI_API_KEY in an existing .env")
    args = parser.parse_args()
    path = Path(__file__).resolve().parent.parent / ".env"
    if args.enable_live and not path.exists():
        raise SystemExit("Create .env first with configure.py, or use --live for a new configuration.")
    if path.exists() and not args.enable_live:
        raise SystemExit(".env already exists; it was not overwritten. Use --enable-live to change only model settings.")
    live = args.live or args.enable_live
    key = getpass.getpass("OpenAI API key (hidden input; live requests may incur charges): ").strip() if live else ""
    if live and (not key or any(character in key for character in "\r\n'\"")):
        raise SystemExit("A nonempty, single-line API key is required.")
    if args.enable_live:
        lines = [line for line in path.read_text().splitlines()
                 if not line.startswith(("PROVIDER=", "OPENAI_API_KEY="))]
        write_private(path, "\n".join(lines + ["PROVIDER=openai", "OPENAI_API_KEY=" + key]) + "\n")
        print("Updated only model settings. Restart API and worker, then re-ingest documents for the new vector space.")
        return
    pg_password, redis_password = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    values = {
        "ENVIRONMENT":"development", "PROVIDER":"openai" if live else "mock", "ORCHESTRATOR":"native",
        "JWT_SECRET":secrets.token_urlsafe(48), "JWT_ISSUER":"rag-chatbot-demo", "JWT_AUDIENCE":"rag-chatbot",
        "POSTGRES_PASSWORD":pg_password, "REDIS_PASSWORD":redis_password,
        "DATABASE_URL":f"postgresql+psycopg://chatbot:{pg_password}@postgres:5432/chatbot",
        "REDIS_URL":f"redis://:{redis_password}@redis:6379/0", "OPENAI_API_KEY":key,
        "AUTO_MIGRATE":"false", "REDACT_PII":"true"}
    write_private(path, "".join(f"{name}={value}\n" for name,value in values.items()))
    print("Created .env with random development secrets. Do not commit or share this file.")


if __name__ == "__main__":
    main()
