#!/bin/sh
# Run Tideline straight from the source tree.
cd "$(dirname "$(readlink -f "$0")")" && exec python -m tideline "$@"
