#!/usr/bin/env bash
# Run each build step's tests separately and print a per-step result table.
# The table is also written to $GITHUB_STEP_SUMMARY when run in GitHub Actions.
# Exit code: 1 if any built step fails, 0 otherwise. Steps whose test file
# doesn't exist yet are reported as "not built yet" and don't fail the run.

set -u

STEPS=(
  "0|Project setup|tests/test_smoke.py"
  "1|Parsing|tests/test_parsing.py"
  "2|LM clients|tests/test_lm_clients.py"
  "3|Wire protocol|tests/test_comms.py"
  "4|LMHandler|tests/test_lm_handler.py"
  "5|Custom tools|tests/test_tools.py"
  "6|Exec script|tests/test_exec_script.py"
  "7|LMProxyHandler|tests/test_proxy.py"
  "8|ContainerManager|tests/test_container_manager.py"
  "9|Client|tests/test_client.py"
  "10|End-to-end|tests/test_e2e.py"
)

summary="| Step | Component | Tests | Result |\n|---|---|---|---|\n"
failed=0

for entry in "${STEPS[@]}"; do
  IFS="|" read -r num name file <<< "$entry"
  if [ ! -f "$file" ]; then
    summary+="| $num | $name | \`$file\` | – not built yet |\n"
    continue
  fi
  echo "::group::Step $num · $name ($file)"
  if python -m pytest -v -rs "$file"; then
    result="✅ passed"
  else
    result="❌ failed"
    failed=1
  fi
  echo "::endgroup::"
  summary+="| $num | $name | \`$file\` | $result |\n"
done

printf "\n%b" "$summary"
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  printf "## Tests by build step\n\n%b" "$summary" >> "$GITHUB_STEP_SUMMARY"
fi
exit $failed
