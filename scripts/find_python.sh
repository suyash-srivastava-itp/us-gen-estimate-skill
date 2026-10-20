#!/bin/sh
# Finds a Python 3.8 or newer interpreter, checks numpy, and prints where this skill lives.
# Use on macOS, Linux and Git Bash on Windows. On Windows PowerShell use find_python.ps1.
#
#   sh <skill>/scripts/find_python.sh [interpreter]
#
# With no argument it tries python3, then python, then the Windows launcher (py -3).
# With an argument, only that interpreter is tried, for example a virtual environment's python.
#
# Output, one value per line:
#   PYTHON=<absolute path of the interpreter>
#   PYTHON_VERSION=3.11
#   NUMPY=2.4.6                      or NUMPY=missing, with INSTALL=... and NOTE=...
#   SKILL_DIR=<absolute path of this skill's folder>
# Exit status: 0 ready, 2 Python found but numpy missing, 1 no Python 3.8 or newer.

here=$(cd "$(dirname "$0")" && pwd)
skill=$(cd "$here/.." && pwd)
probe="$here/python_probe.py"
found=""
pyver=""
npv=""

# check COMMAND...: runs the probe with that interpreter and keeps it if it is Python 3.8 or newer.
check() {
    out=$("$@" "$probe" 2>/dev/null) || return 1
    ver=$(printf '%s\n' "$out" | sed -n 2p)
    major=${ver%% *}
    minor=${ver#* }
    [ "$major" -eq 3 ] 2>/dev/null && [ "$minor" -ge 8 ] 2>/dev/null || return 1
    found=$(printf '%s\n' "$out" | sed -n 1p)
    pyver="$major.$minor"
    npv=$(printf '%s\n' "$out" | sed -n 3p)
    return 0
}

if [ -n "$1" ]; then
    check "$1"
else
    check python3 || check python || check py -3
fi

if [ -z "$found" ]; then
    echo "NO_PYTHON=1"
    case "$(uname -s)" in
        Darwin) echo "HINT=Install Python 3.8 or newer: brew install python, or from https://www.python.org/downloads/" ;;
        Linux)  echo "HINT=Install Python 3.8 or newer: sudo apt install python3 python3-pip (or your distribution's equivalent)" ;;
        *)      echo "HINT=Install Python 3.8 or newer from https://www.python.org/downloads/ and tick 'Add python.exe to PATH'" ;;
    esac
    exit 1
fi

# Git Bash reports /c/... paths, which Windows Python does not read; give it C:/... instead.
if command -v cygpath >/dev/null 2>&1; then
    skill=$(cygpath -m "$skill")
fi

echo "PYTHON=$found"
echo "PYTHON_VERSION=$pyver"
if [ "$npv" = "missing" ]; then
    echo "NUMPY=missing"
    printf '%s\n' "INSTALL=\"$found\" -m pip install -r \"$skill/requirements.txt\""
    echo "NOTE=Ask the person before installing. If pip reports externally-managed-environment, create a virtual environment and run this script with its python."
    status=2
else
    echo "NUMPY=$npv"
    status=0
fi
echo "SKILL_DIR=$skill"
exit $status
