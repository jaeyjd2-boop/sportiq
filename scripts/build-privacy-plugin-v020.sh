#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source_file="wordpress/sportiq-account-privacy/sportiq-account-privacy.php"
test -f "$source_file"
grep -q 'Version: 0.2.0' "$source_file"
php -l "$source_file"
mkdir -p dist
tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT
mkdir -p "$tmpdir/sportiq-account-privacy"
cp "$source_file" "$tmpdir/sportiq-account-privacy/sportiq-account-privacy.php"
(
 cd "$tmpdir"
 zip -q -r "$OLDPWD/dist/sportiq-account-privacy-v020.zip" sportiq-account-privacy
)
sha256sum dist/sportiq-account-privacy-v020.zip
echo "Created dist/sportiq-account-privacy-v020.zip"
