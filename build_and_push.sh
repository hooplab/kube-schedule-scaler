#!/bin/bash

set -o nounset
set -o errexit
trap 'echo "Aborting due to errexit on line $LINENO. Exit code: $?" >&2' ERR
set -o errtrace
set -o pipefail

docker build -t hoopla/kube-schedule-scaler:latest .
docker push hoopla/kube-schedule-scaler:latest