#!/bin/sh
# Wire git auth from GITHUB_TOKEN for clone/fetch/push over https to github.com,
# WITHOUT writing the token to disk or a process argument. Git reads the
# Authorization header from the environment (GIT_CONFIG_*), so the token never
# lands in .git/config or a command line. Mirrors the Homebase vault worker.
set -e

if [ -n "$GITHUB_TOKEN" ]; then
  basic=$(printf 'x-access-token:%s' "$GITHUB_TOKEN" | base64 | tr -d '\n')
  export GIT_CONFIG_COUNT=1
  export GIT_CONFIG_KEY_0="http.https://github.com/.extraheader"
  export GIT_CONFIG_VALUE_0="AUTHORIZATION: basic ${basic}"
  unset GITHUB_TOKEN
fi

exec "$@"
