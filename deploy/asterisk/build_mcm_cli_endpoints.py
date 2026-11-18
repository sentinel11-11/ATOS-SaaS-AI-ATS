#!/usr/bin/env python3
"""Build reviewed PJSIP CLI endpoint aliases without changing a live Asterisk installation."""

import argparse
import ipaddress
import re
from pathlib import Path


def normalize_number(raw: str) -> str:
    value = raw.strip()
    if re.fullmatch(r"[0-9]{10}", value):
        return value
    if re.fullmatch(r"(?:\\+7|7|8)[0-9]{10}", value):
        return value[-10:]
    raise ValueError("Expected a Russian DID in 10-digit, 7, +7 or 8 format")


def render(numbers: list[str], host: str = "95.128.224.47") -> str:
    if not 1 <= len(numbers) <= 100:
        raise ValueError("Expected between 1 and 100 verified numbers")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not re.fullmatch(r"[a-zA-Z0-9.-]{1,253}", host):
            raise ValueError("Invalid SIP host") from None
    if len(set(numbers)) != len(numbers):
        raise ValueError("Duplicate DIDs")
    blocks = []
    for number in numbers:
        if not re.fullmatch(r"[0-9]{10}", number):
            raise ValueError("Invalid DID")
        blocks.append(
            "\\n".join(
                (
                    f"[mcm-cli-{number}]",
                    "type=endpoint",
                    "transport=transport-udp",
                    "context=from-mcm",
                    "disallow=all",
                    "allow=alaw,ulaw",
                    "aors=mcm-aor",
                    "outbound_auth=mcm-auth",
                    f"from_domain={host}",
                    f"from_user={number}",
                    f'callerid="ATOS" <{number}>',
                    "direct_media=no",
                    "rtp_symmetric=yes",
                    "force_rport=yes",
                )
            )
        )
    return "\\n\\n".join(blocks) + "\\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--numbers-file", required=True, type=Path)
    parser.add_argument("--from-domain", default="95.128.224.47")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    lines = [
        line.strip()
        for line in args.numbers_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    numbers = [normalize_number(line) for line in lines]
    result = render(numbers, args.from_domain)
    if args.output:
        args.output.write_text(result, encoding="utf-8")
    else:
        print(result, end="")


if __name__ == "__main__":
    main()
