"""Bind the existing encrypted amoCRM profile to the telephony tenant, without copying secrets."""

import argparse
import io
import os
import pwd
import re
import stat
import subprocess
from pathlib import Path
from uuid import UUID

from dotenv import dotenv_values, load_dotenv


def environment(original, tenant_id, subdomain):
    updates = {"ATS_AMO_TENANT_ID": str(UUID(str(tenant_id))), "ATS_AMO_URL": f"https://{subdomain}.amocrm.ru",
               "ATS_CRM_ENABLED": "true"}
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", subdomain):
        raise ValueError("Invalid amoCRM subdomain")
    lines = [line for line in original.splitlines() if not any(
        re.match(rf"^\s*(?:export\s+)?{key}\s*=", line) for key in updates
    )]
    return "\n".join(lines + [f'{key}="{value}"' for key, value in updates.items()]) + "\n"


def check_profile(args):
    from sqlalchemy import select

    from ats.adapters import AmoCRM, Rejected, Retryable
    from ats.amo_account import profile_version, store_account
    from ats.connections import decrypt_profile, settings_for_tenant
    from ats.db import session_factory
    from ats.domain import Problem
    from ats.models import ConnectionProfile, Operator, User

    load_dotenv(args.env_file, override=True, interpolate=False)
    with session_factory()() as db:
        tenant_id = UUID(args.check_profile)
        version = profile_version(db, tenant_id)
        row = db.scalar(select(ConnectionProfile).where(ConnectionProfile.tenant_id == tenant_id,
                                                        ConnectionProfile.provider == "amocrm"))
        public = row.public_values if row else {}
        secret = decrypt_profile(row) if row else {}
        config = settings_for_tenant(db, tenant_id)
        if version != profile_version(db, tenant_id):
            raise ValueError("amoCRM settings changed; run the setup again")
        if (not public.get("client_id") or not secret.get("client_secret")
            or not config.amo_access_token.get_secret_value() or public.get("subdomain") != args.subdomain
            or public.get("disabled")):
            raise ValueError("Fill integration ID/secret, access token and subdomain in ATOS Settings first")
        UUID(public["client_id"])
        mapping = public.get("operator_user_map", {})
        if not mapping or len(set(mapping.values())) != len(mapping):
            raise ValueError("Each amoCRM user must map to one ATOS extension")
        extensions = set(db.scalars(select(Operator.extension).join(User, User.id == Operator.user_id).where(
            Operator.tenant_id == tenant_id, User.active, ~User.archived, User.role == "operator",
        )))
        if not set(mapping).issubset(extensions):
            raise ValueError("The mapping refers to missing or inactive ATOS operator extensions")
        crm = AmoCRM(config)
        try:
            account = crm.read("/api/v4/account")
            account_id = store_account(db, tenant_id, account, args.subdomain, version)
            db.commit()
        except (Problem, Rejected, Retryable) as exc:
            raise ValueError(f"amoCRM account verification failed: {exc.code}") from exc
        finally:
            crm.close()
        print(f"Verified amoCRM account ID: {account_id}. Saved to the ATOS profile.")
        print(f"Encrypted amoCRM profile verified: {len(mapping)} mapped operators. No secrets printed.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path("/etc/ats-beta/ats.env"))
    parser.add_argument("--subdomain", default="omkr")
    parser.add_argument("--check-profile", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.check_profile:
            check_profile(args)
            return
        if os.geteuid() != 0:
            parser.error("Run as root with /opt/ats-beta/.venv/bin/python")
        meta = args.env_file.stat()
        if args.env_file.is_symlink() or stat.S_IMODE(meta.st_mode) & 0o007:
            parser.error("The environment file must be private and regular")
        original = args.env_file.read_text()
        values = dotenv_values(stream=io.StringIO(original), interpolate=False)
        tenant_id = str(UUID(values.get("ATS_TELEPHONY_TENANT_ID", "")))
        if values.get("ATS_AMO_TENANT_ID") not in (None, "", tenant_id):
            parser.error("Existing amoCRM binding differs from the telephony tenant")
        updated = environment(original, tenant_id, args.subdomain)
        account = pwd.getpwnam("ats-beta")
        subprocess.run(["runuser", "-u", account.pw_name, "--", str(Path(__file__).parents[1] / ".venv/bin/python"),
                        str(Path(__file__).resolve()), "--env-file", str(args.env_file), "--subdomain", args.subdomain,
                        "--check-profile", tenant_id], check=True)
        import runpy

        write = runpy.run_path(str(Path(__file__).with_name("install-widget-webrtc.py")))["write_private"]
        write(args.env_file, updated, meta.st_uid, meta.st_gid, stat.S_IMODE(meta.st_mode))
        print("Widget CRM binding saved. Restart beta API, inbox and CRM workers to apply. No calls placed.")
    except ValueError as exc:
        # All ValueErrors created here describe public identifiers/configuration fields.
        parser.exit(1, f"Widget configuration failed: {exc}\n")
    except (OSError, KeyError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Widget configuration failed ({type(exc).__name__}). No secrets printed.\n")


if __name__ == "__main__":
    main()
