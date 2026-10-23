#!/usr/bin/env bash
set -Eeuo pipefail

# Allow only the provider's SIP signalling peer. RTP remains a separate,
# explicitly configured range; this rule never opens SIP to the internet.
provider_ip="${MULTICOM_SIP_IP:-95.128.224.47}"
local_port="${MULTICOM_SIP_PORT:-5060}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "run as root" >&2
  exit 1
fi

if command -v nft >/dev/null 2>&1 && nft list ruleset >/dev/null 2>&1; then
  nft list table inet ats_multicom >/dev/null 2>&1 || nft add table inet ats_multicom
  nft list chain inet ats_multicom input >/dev/null 2>&1 || \
    nft 'add chain inet ats_multicom input { type filter hook input priority -100; policy accept; }'
  nft list chain inet ats_multicom input | grep -Fq "ip saddr ${provider_ip} udp sport ${local_port} udp dport ${local_port} accept" || \
    nft add rule inet ats_multicom input ip saddr "${provider_ip}" udp sport "${local_port}" udp dport "${local_port}" accept
  echo "nftables rule installed; persist it in the host firewall configuration"
elif command -v iptables >/dev/null 2>&1; then
  iptables -C INPUT -p udp -s "${provider_ip}" --sport "${local_port}" --dport "${local_port}" -j ACCEPT 2>/dev/null || \
    iptables -I INPUT -p udp -s "${provider_ip}" --sport "${local_port}" --dport "${local_port}" -j ACCEPT
  echo "iptables rule installed; persist it with the host firewall's save mechanism"
else
  echo "No local firewall manager found. Add this exact rule in the hosting firewall:" >&2
  echo "allow UDP source ${provider_ip}:${local_port} -> destination ${local_port}" >&2
  exit 2
fi

echo "Verify with: tcpdump -ni any -nn 'src host ${provider_ip} and udp src port ${local_port} and udp dst port ${local_port}'"
