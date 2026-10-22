#!/bin/bash
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
IBC_JAR=${1:-"$HOME/ibc/IBC.jar"}
OUT_JAR=${2:-"$HERE/dist/IBC-patched.jar"}
JAVA=${JAVA:-"$HOME/Applications/IB Gateway 10.37/.install4j/jre.bundle/Contents/Home/bin/java"}

ECJ_VERSION=3.38.0
ECJ_SHA256=97c566b120009c203a2fc8b291f4a9adbc171cf1ccb70f06f6b4e1828c00ce8e
ECJ_URL="https://repo.maven.apache.org/maven2/org/eclipse/jdt/ecj/${ECJ_VERSION}/ecj-${ECJ_VERSION}.jar"
IBC_323_SHA256=1cbd90e8054807f9b1dbbe4df9f50b3fb7b56b428a8e20d9a8c068140789219c

if [[ ! -f "$IBC_JAR" ]]; then
    echo "IBC jar not found: $IBC_JAR" >&2
    exit 1
fi
if [[ ! -x "$JAVA" ]]; then
    echo "Gateway Java runtime not found: $JAVA" >&2
    exit 1
fi

INPUT_SHA=$(shasum -a 256 "$IBC_JAR" | awk '{print $1}')
if [[ "$INPUT_SHA" != "$IBC_323_SHA256" ]]; then
    echo "unsupported IBC.jar sha256: $INPUT_SHA" >&2
    echo "expected unmodified IBC 3.23.0: $IBC_323_SHA256" >&2
    echo "refusing to overwrite an unknown, upgraded, or already-patched handler" >&2
    exit 1
fi

TMP=$(mktemp -d "${TMPDIR:-/tmp}/ibc-maintenance-reconnect.XXXXXX")
trap 'rm -rf "$TMP"' EXIT

curl -fsSL "$ECJ_URL" -o "$TMP/ecj.jar"
ACTUAL_SHA=$(shasum -a 256 "$TMP/ecj.jar" | awk '{print $1}')
if [[ "$ACTUAL_SHA" != "$ECJ_SHA256" ]]; then
    echo "ecj checksum mismatch: $ACTUAL_SHA" >&2
    exit 1
fi

mkdir -p "$TMP/classes"
"$JAVA" -jar "$TMP/ecj.jar" \
    -source 1.8 -target 1.8 -proc:none \
    -cp "$IBC_JAR" \
    -d "$TMP/classes" \
    "$HERE/src/ibcalpha/ibc/GatewayDialogHandler.java" \
    "$HERE/test/ibcalpha/ibc/GatewayDialogHandlerPolicyTest.java"

"$JAVA" -Xverify:all \
    -cp "$TMP/classes:$IBC_JAR" \
    ibcalpha.ibc.GatewayDialogHandlerPolicyTest

mkdir -p "$(dirname "$OUT_JAR")"
python3 - "$IBC_JAR" "$OUT_JAR" "$TMP/classes/ibcalpha/ibc/GatewayDialogHandler.class" <<'PY'
import pathlib
import shutil
import sys
import tempfile
import zipfile

source = pathlib.Path(sys.argv[1])
output = pathlib.Path(sys.argv[2])
replacement = pathlib.Path(sys.argv[3]).read_bytes()
target = "ibcalpha/ibc/GatewayDialogHandler.class"

if replacement[:4] != b"\xca\xfe\xba\xbe":
    raise SystemExit("compiled replacement is not a Java class")
major = int.from_bytes(replacement[6:8], "big")
if major != 52:
    raise SystemExit(f"replacement class version is {major}, expected Java 8 class version 52")

tmp = pathlib.Path(tempfile.mkstemp(prefix="IBC-patched-", suffix=".jar", dir=output.parent)[1])
try:
    found = False
    with zipfile.ZipFile(source, "r") as zin, zipfile.ZipFile(tmp, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == target:
                data = replacement
                found = True
            zout.writestr(item, data)
    if not found:
        raise SystemExit(f"target class not found in {source}: {target}")
    shutil.move(tmp, output)
finally:
    if tmp.exists():
        tmp.unlink()
PY

"$JAVA" -Xverify:all \
    -cp "$OUT_JAR:$TMP/classes" \
    ibcalpha.ibc.GatewayDialogHandlerPolicyTest

python3 - "$OUT_JAR" <<'PY'
import sys
import zipfile

with zipfile.ZipFile(sys.argv[1]) as z:
    names = z.namelist()
    target = "ibcalpha/ibc/GatewayDialogHandler.class"
    if names.count(target) != 1:
        raise SystemExit(f"expected exactly one {target}, found {names.count(target)}")
    if "Local Gateway maintenance reconnect override active".encode() not in z.read(target):
        raise SystemExit("patched handler marker missing")
print("Patched IBC jar verification: PASS")
PY

echo "Built: $OUT_JAR"
