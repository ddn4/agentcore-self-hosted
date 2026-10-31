#!/usr/bin/env bash
# Redact AWS account IDs and resource IDs from stdin before output is pasted
# into story docs (plan section 3.2). Usage: some-command 2>&1 | scripts/redact.sh
# v0.8 adds: email addresses -> <email>; IPv4 addresses other than 127.0.0.1 / 0.0.0.0 /
# 10.255.255.1 (the test blackhole) -> <ip>; standalone 32-hex strings (the External ID
# shape, openssl rand -hex 16) -> <hex32>.
# v0.10 adds: the SSO role suffix (AWSReservedSSO_<set>_<16 hex>), EKS OIDC issuer IDs and API
# endpoint hostnames (32 upper-case hex), RDS endpoint IDs (<db>.<12 chars>.<region>.rds...).
# v0.11 adds: NLB DNS-name suffixes (<name>-<16 hex>.elb.<region>...), ELB ARN IDs and Route 53
# hosted zone IDs (Z + 12-31 upper-case alphanumerics).
set -euo pipefail
sed -E \
  -e 's/arn:aws([a-z-]*):([a-z0-9-]*):([a-z0-9-]*):[0-9]{12}:/arn:aws\1:\2:\3:<acct>:/g' \
  -e 's/(^|[^0-9A-Za-z])[0-9]{12}([^0-9A-Za-z]|$)/\1<acct>\2/g' \
  -e 's/(^|[^0-9A-Za-z])[0-9]{12}([^0-9A-Za-z]|$)/\1<acct>\2/g' \
  -e 's/(^|[^0-9A-Za-z])(vpc|subnet|sg|eni|igw|nat|rtb|vpce|eipalloc|i)-[0-9a-f]{8,17}([^0-9A-Za-z]|$)/\1<\2-…>\3/g' \
  -e 's/(^|[^0-9A-Za-z])(vpc|subnet|sg|eni|igw|nat|rtb|vpce|eipalloc|i)-[0-9a-f]{8,17}([^0-9A-Za-z]|$)/\1<\2-…>\3/g' \
  -e 's/AWSReservedSSO_([A-Za-z]+)_[0-9a-f]{16}/AWSReservedSSO_\1_<id>/g' \
  -e 's#(oidc\.eks\.[a-z0-9-]+\.amazonaws\.com/id/)[0-9A-F]{32}#\1<oidc-id>#g' \
  -e 's#(^|[^0-9A-Za-z])[0-9A-F]{32}(\.[a-z0-9]+\.[a-z0-9-]+\.eks\.amazonaws\.com)#\1<eks-id>\2#g' \
  -e 's#(\.)[a-z0-9]{12}(\.[a-z0-9-]+\.rds\.amazonaws\.com)#\1<rds-id>\2#g' \
  -e 's/(AKIA|ASIA)[A-Z0-9]{16}/<aws-access-key>/g' \
  -e 's#([a-z0-9-]+)-[0-9a-f]{16}(\.elb\.[a-z0-9-]+\.amazonaws\.com)#\1-<nlb-id>\2#g' \
  -e 's#(loadbalancer/net/[A-Za-z0-9-]+/|targetgroup/[A-Za-z0-9-]+/)[0-9a-f]{16}#\1<elb-id>#g' \
  -e 's/(^|[^0-9A-Za-z])Z[0-9A-Z]{12,31}([^0-9A-Za-z]|$)/\1<zone-id>\2/g' \
  -e 's#(runtime/|runtime-endpoint/)[A-Za-z][A-Za-z0-9_]{0,47}-[A-Za-z0-9]{10}#\1<runtime-id>#g' \
  -e 's#(runtimes/)[A-Za-z][A-Za-z0-9_]{0,47}-[A-Za-z0-9]{10}-#\1<runtime-id>-#g' \
  -e 's#(:secret:[A-Za-z0-9/_+=.@-]+)-[A-Za-z0-9]{6}([^A-Za-z0-9]|$)#\1-<sfx>\2#g' \
  -e 's#(AROA|AIDA)[A-Z0-9]{16,}#<principal-id>#g' \
  -e 's/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/<email>/g' \
  -e 's/127\.0\.0\.1/__KEEP_LOOPBACK__/g; s/0\.0\.0\.0/__KEEP_ANY__/g; s/10\.255\.255\.1/__KEEP_BLACKHOLE__/g' \
  -e 's/(^|[^0-9.])([0-9]{1,3}\.){3}[0-9]{1,3}([^0-9.]|$)/\1<ip>\3/g' \
  -e 's/(^|[^0-9.])([0-9]{1,3}\.){3}[0-9]{1,3}([^0-9.]|$)/\1<ip>\3/g' \
  -e 's/__KEEP_LOOPBACK__/127.0.0.1/g; s/__KEEP_ANY__/0.0.0.0/g; s/__KEEP_BLACKHOLE__/10.255.255.1/g' \
  -e 's/(^|[^0-9A-Fa-f])[0-9a-f]{32}([^0-9A-Fa-f]|$)/\1<hex32>\2/g'
