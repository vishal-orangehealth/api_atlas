#!/bin/sh
set -e
exec atlas serve --host 0.0.0.0 --port "${PORT:-7801}" --no-open
