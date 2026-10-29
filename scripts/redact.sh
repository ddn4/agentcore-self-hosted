#!/usr/bin/env bash
# Redact AWS account IDs and resource IDs from stdin before output is pasted
# into story docs (plan section 3.2). Usage: some-command 2>&1 | scripts/redact.sh
set -euo pipefail
sed -E \
  -e 's/arn:aws([a-z-]*):([a-z0-9-]*):([a-z0-9-]*):[0-9]{12}:/arn:aws\1:\2:\3:<acct>:/g' \
  -e 's/(^|[^0-9A-Za-z])[0-9]{12}([^0-9A-Za-z]|$)/\1<acct>\2/g' \
  -e 's/(^|[^0-9A-Za-z])[0-9]{12}([^0-9A-Za-z]|$)/\1<acct>\2/g' \
  -e 's/(^|[^0-9A-Za-z])(vpc|subnet|sg|eni|igw|nat|rtb|vpce|eipalloc|i)-[0-9a-f]{8,17}([^0-9A-Za-z]|$)/\1<\2-…>\3/g' \
  -e 's/(^|[^0-9A-Za-z])(vpc|subnet|sg|eni|igw|nat|rtb|vpce|eipalloc|i)-[0-9a-f]{8,17}([^0-9A-Za-z]|$)/\1<\2-…>\3/g' \
  -e 's/(AKIA|ASIA)[A-Z0-9]{16}/<aws-access-key>/g' \
  -e 's#(runtime/|runtime-endpoint/)[A-Za-z][A-Za-z0-9_]{0,47}-[A-Za-z0-9]{10}#\1<runtime-id>#g' \
  -e 's#(AROA|AIDA)[A-Z0-9]{16,}#<principal-id>#g'
