import argparse
import getpass
import json
import re
from uuid import UUID, uuid4

from ats.db import session_factory
from ats.domain import Problem
from ats.legacy_contacts import migrate_contacts
from ats.models import Attempt, Tenant, User
from ats.schemas import valid_timezone
from ats.security import hasher


def bootstrap(factory, tenant_id, name, login, password, timezone="Europe/Moscow"):
    if len(password) < 12:
        raise ValueError("Password must have at least 12 characters")
    with factory.begin() as db:
        if db.get(Tenant, tenant_id):
            raise ValueError("Tenant already exists; credentials were not overwritten")
        db.add(Tenant(id=tenant_id, name=name, timezone=valid_timezone(timezone)))
        db.flush()
        db.add(
            User(
                tenant_id=tenant_id,
                login=login.lower(),
                password_hash=hasher.hash(password),
                role="admin",
            )
        )


def diagnose_multicom(factory, attempt_id):
    from ats.connections import settings_for_tenant

    with factory() as db:
        attempt = db.get(Attempt, attempt_id)
        if attempt is None:
            raise Problem(404, "call_not_found")
        if attempt.route != "multicom_ami":
            raise Problem(409, "multicom_call_required")
        config = settings_for_tenant(db, attempt.tenant_id)
        endpoint = config.ami_trunk
        if config.ami_cli_endpoint_prefix:
            if not re.fullmatch(r"[0-9]{10}", attempt.caller_number or ""):
                raise Problem(409, "caller_number_invalid")
            endpoint = config.ami_cli_endpoint_prefix + attempt.caller_number
        return {
            "call_id": str(attempt.id), "status": attempt.status, "reason": attempt.reason,
            "caller_number": attempt.caller_number, "phone": attempt.phone,
            "external_id": attempt.external_id, "channel": attempt.channel,
            "ami": {
                "host": config.ami_host, "port": config.ami_port, "user": config.ami_user,
                "secret_set": bool(config.ami_secret.get_secret_value()),
                "tech": config.ami_tech, "trunk": config.ami_trunk,
                "cli_endpoint_prefix": config.ami_cli_endpoint_prefix,
                "context": config.ami_context, "answer_context": config.ami_answer_context,
                "selected_endpoint": endpoint, "media_enabled": config.media_enabled,
            },
            "asterisk_commands": [
                "dialplan show " + config.ami_context,
                "dialplan show " + config.ami_answer_context,
                "pjsip show endpoint " + endpoint,
            ],
        }


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("bootstrap")
    init.add_argument("--tenant-id", type=UUID, default=None)
    init.add_argument("--name", required=True)
    init.add_argument("--login", default="admin")
    init.add_argument("--timezone", default="Europe/Moscow")
    migrate = sub.add_parser("import-contacts")
    migrate.add_argument("--sqlite", required=True)
    migrate.add_argument("--tenant-id", type=UUID, required=True)
    migrate.add_argument("--source-id", required=True)
    migrate.add_argument("--apply", action="store_true")
    workspace = sub.add_parser("import-workspace")
    workspace.add_argument("--sqlite", required=True)
    workspace.add_argument("--tenant-id", type=UUID, required=True)
    workspace.add_argument("--source-id", required=True)
    workspace.add_argument("--source-timezone", default="Europe/Moscow")
    workspace.add_argument("--apply", action="store_true")
    diagnostic = sub.add_parser("diagnose-multicom")
    diagnostic.add_argument("call_id", type=UUID)
    diagnostic.add_argument("--env-file")
    args = parser.parse_args()
    if args.command == "diagnose-multicom" and args.env_file:
        from dotenv import load_dotenv

        from ats.config import settings

        load_dotenv(args.env_file)
        settings.cache_clear()
    if args.command == "diagnose-multicom":
        try:
            report = diagnose_multicom(session_factory(), args.call_id)
        except Exception as exc:
            code = exc.code if isinstance(exc, Problem) else type(exc).__name__
            raise SystemExit("Multicom diagnostic failed (" + code + ")") from None
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    factory = session_factory()
    if args.command == "bootstrap":
        tenant_id = args.tenant_id or uuid4()
        password = getpass.getpass("Admin password (min 12 characters): ")
        if getpass.getpass("Repeat password: ") != password:
            raise SystemExit("Passwords differ")
        bootstrap(factory, tenant_id, args.name, args.login, password, args.timezone)
        print(json.dumps({"tenant_id": str(tenant_id), "login": args.login}))
    elif args.command == "import-workspace":
        from ats.legacy_import import migrate_workspace

        print(
            json.dumps(
                migrate_workspace(
                    factory,
                    args.sqlite,
                    args.tenant_id,
                    args.source_id,
                    args.apply,
                    args.source_timezone,
                ),
                ensure_ascii=False,
            )
        )
    else:
        print(
            json.dumps(
                migrate_contacts(factory, args.sqlite, args.tenant_id, args.source_id, args.apply),
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
