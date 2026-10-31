"""Where the AgentCore ENIs talked to, from the VPC flow logs (plan v0.12; AWS Q2, runbook 1.5).

    uv run python scripts/flow_report.py [--since MINUTES]          (make flow-report SINCE=60)

1. Finds the AgentCore ENIs: every ENI in the project VPC that carries ``sg-agentcore-worker``
   (that group is attached to nothing else; AgentCore creates the ENIs through its
   service-linked role).
2. Classifies every other address the flows mention: the internal NLB ``temporal-frontend``
   (its ENIs), each interface endpoint (by service name), the S3 gateway endpoint's managed
   prefix list, the rest of the VPC CIDR, or "outside the VPC".
3. Reads the flow log streams of those ENIs (one stream per ENI) and prints, for traffic the
   ENIs **sent**, one line per (destination kind, port, action) with flow and byte counts.

Inputs come from ``.generated/foundation.json`` (``make render-config``). Output carries no
IP addresses or resource IDs: kinds, ports and counts only (it is pasted into story docs).
Read-only AWS calls only.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
REGION = "us-east-1"


def aws(*args: str) -> Any:
    out = subprocess.run(
        ["aws", "--region", REGION, "--output", "json", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return json.loads(out) if out.strip() else None


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--since", type=int, default=60, help="minutes of flow logs to read")
    args = parser.parse_args()
    net = json.loads((ROOT / ".generated/foundation.json").read_text())["10-network"]
    vpc, sg = net["vpc_id"], net["security_group_ids"]["agentcore_worker"]
    cidr = ipaddress.ip_network(net["vpc_cidr_block"])
    log_group = str(net["flow_log_destination_arn"]).split(":log-group:")[-1].removesuffix(":*")

    enis = aws(
        "ec2",
        "describe-network-interfaces",
        "--filters",
        f"Name=vpc-id,Values={vpc}",
        "--query",
        "NetworkInterfaces[].{id: NetworkInterfaceId, ip: PrivateIpAddress, type: InterfaceType, "
        "desc: Description, groups: Groups[].GroupId, requester: RequesterId}",
    )
    kinds: dict[str, str] = {}
    agentcore: list[dict[str, Any]] = []
    endpoints = {
        e["VpcEndpointId"]: e["ServiceName"].rsplit(".", 1)[-1]
        for e in aws("ec2", "describe-vpc-endpoints", "--filters", f"Name=vpc-id,Values={vpc}")[
            "VpcEndpoints"
        ]
    }
    for eni in enis:
        desc = eni.get("desc") or ""
        if sg in (eni.get("groups") or []):
            agentcore.append(eni)
            kinds[eni["ip"]] = "AgentCore ENI"
        elif desc.startswith("ELB net/temporal-frontend/"):
            kinds[eni["ip"]] = "NLB temporal-frontend"
        elif eni.get("type") == "vpc_endpoint":
            vpce = next((v for v in endpoints if v in desc), "")
            kinds[eni["ip"]] = f"endpoint {endpoints.get(vpce, '?')}"
        elif eni.get("type") == "nat_gateway":
            kinds[eni["ip"]] = "NAT gateway"
    prefix_cidrs = [
        ipaddress.ip_network(e["Cidr"])
        for e in aws(
            "ec2", "get-managed-prefix-list-entries", "--prefix-list-id", net["s3_prefix_list_id"]
        )["Entries"]
    ]

    def kind_of(ip: str) -> str:
        if ip in kinds:
            return kinds[ip]
        addr = ipaddress.ip_address(ip)
        if any(addr in n for n in prefix_cidrs):
            return "S3 (gateway endpoint prefix list)"
        if addr in cidr:
            return "other address in the VPC"
        return "outside the VPC"

    print(f"flow-report: last {args.since} min, flow log group of the project VPC")
    print(f"  AgentCore ENIs (carry sg-agentcore-worker): {len(agentcore)}")
    for eni in agentcore:
        print(f"    type {eni.get('type')}, description {eni.get('desc')!r}")
    if not agentcore:
        return 1
    start = int((time.time() - args.since * 60) * 1000)
    sent: Counter[tuple[str, str, str]] = Counter()
    sent_bytes: Counter[tuple[str, str, str]] = Counter()
    received: Counter[tuple[str, str, str]] = Counter()
    for eni in agentcore:
        token: str | None = None
        while True:
            cmd = [
                "logs",
                "filter-log-events",
                "--log-group-name",
                log_group,
                "--log-stream-name-prefix",
                eni["id"],
                "--start-time",
                str(start),
            ]
            if token:
                cmd += ["--next-token", token]
            page = aws(*cmd)
            for ev in page.get("events", []):
                f = ev["message"].split()
                # default format: version account eni src dst srcport dstport proto
                # packets bytes start end action status
                if len(f) < 14 or f[13] != "OK":
                    continue
                src, dst, dport, nbytes, action = f[3], f[4], f[6], f[9], f[12]
                if src == eni["ip"]:
                    key = (kind_of(dst), dport, action)
                    sent[key] += 1
                    sent_bytes[key] += int(nbytes)
                elif dst == eni["ip"]:
                    received[(kind_of(src), f[5], action)] += 1
            token = page.get("nextToken")
            if not token:
                break
    print("  sent by the AgentCore ENIs (destination, port, action: flow records, bytes):")
    for (kind, port, action), n in sorted(sent.items(), key=lambda kv: (-kv[1], kv[0])):
        nbytes_total = sent_bytes[(kind, port, action)]
        print(f"    {kind:38} {port:>5} {action:6} {n:5} flows {nbytes_total:>10} B")
    if not sent:
        print("    (none)")
    replies = sum(n for (k, _p, _a), n in received.items() if k != "outside the VPC")
    unsolicited = sum(n for (k, _p, _a), n in received.items() if k == "outside the VPC")
    print(f"  received: {replies} flow records from the VPC side, {unsolicited} from outside it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
