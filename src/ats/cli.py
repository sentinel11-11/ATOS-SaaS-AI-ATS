import argparse
import getpass
import json
from uuid import UUID, uuid4

from ats.db import session_factory
from ats.legacy_contacts import migrate_contacts
from ats.models import Tenant, User
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
    args = parser.parse_args()
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
