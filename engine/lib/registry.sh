# registry.sh – read `engine/models.toml` from a bash script, and fail loudly when the read fails.
#
# Sourced by `engine/infra/model-store/download-candidates.sh` and
# `engine/infra/model-serving/speaches-models.sh`, both of which act on a list the registry
# declares and both of which would otherwise report a truncated list as a complete one.
#
#   . "$(dirname "$(readlink -f "$0")")/../../lib/registry.sh"
#   registry_read weights          # rows land in the REGISTRY_ROWS array
#
# WHY THIS IS NOT `mapfile < <(python3 …)`. `mapfile` reports on itself, not on the process
# substitution feeding it, so a reader that raises halfway through exits non-zero and mapfile
# still succeeds — and CPython flushes what it had already buffered during interpreter
# shutdown, so the rows before the exception DO arrive. One malformed key in one row therefore
# deletes every row below it from the desired state, and the caller prints "everything matches"
# and exits 0. The queries below end with a TERMINATOR that only a completed run emits, and
# `registry_read` requires it.
#
# FIELDS ARE SEPARATED BY \x1f (US), never by a tab. Tab is an IFS *whitespace* character even
# when IFS is set to exactly a tab, so consecutive tabs collapse into one and a row with an
# empty middle field reads back shifted by one – silently, and with the wrong value in the
# wrong variable. \x1f is not whitespace, so an empty field stays an empty field.
#
# Split a row with:  IFS=$'\x1f' read -r a b c <<< "$row"

REGISTRY_SEP=$'\x1f'
_REGISTRY_TERMINATOR='###COMPLETE'

# The registry this script will read, resolved once at source time so a caller can name it in a
# message without re-deriving the path and getting a different answer.
REGISTRY_PATH="${HATCH_MODELS_TOML:-$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../models.toml}"

# registry_read <query>
# Fills REGISTRY_ROWS. Returns 1 and prints why on any failure; the caller decides the exit code.
registry_read() {
  local query="$1" lib raw
  lib=$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")
  local registry="$REGISTRY_PATH"

  if ! raw=$(python3 "$lib/registry-query.py" "$registry" "$query"); then
    echo "registry.sh: reading '$query' from $registry failed" >&2
    return 1
  fi
  # Split first, then require the LAST LINE to be the terminator, and take the rows as
  # everything before it. Stripping the terminator as a suffix instead gets the empty-result
  # case wrong: with no rows there is no newline before it, the suffix does not match, and the
  # terminator itself survives as a row that every caller then treats as a real one.
  local all=()
  mapfile -t all <<< "$raw"
  local last=$(( ${#all[@]} - 1 ))
  if [ "$last" -lt 0 ] || [ "${all[$last]}" != "$_REGISTRY_TERMINATOR" ]; then
    echo "registry.sh: '$query' from $registry ended without $_REGISTRY_TERMINATOR – the read was" >&2
    echo "             cut short, so rows are missing and the result must not be acted on." >&2
    return 1
  fi
  REGISTRY_ROWS=("${all[@]:0:$last}")
  if [ "${#REGISTRY_ROWS[@]}" -eq 0 ]; then
    echo "registry.sh: '$query' is empty in $registry" >&2
    return 1
  fi
  return 0
}
