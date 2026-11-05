#!/bin/sh
# Copies the contract's schemas, interfaces.yaml and errors.yaml, and catalogue/, from the repository into
# packages/rust/shipped/, which build.rs embeds in the crate. Run it after changing any of them; tests/shipped.rs
# fails when the copy is out of date.
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
root=$(cd "$here/../.." && pwd)
for item in contract/schemas contract/interfaces.yaml contract/errors.yaml catalogue; do
  [ -e "$root/$item" ] || { echo "$root/$item is missing" >&2; exit 1; }
done
rm -rf "$here/shipped"
mkdir -p "$here/shipped/contract"
cp -R "$root/contract/schemas" "$here/shipped/contract/schemas"
cp "$root/contract/interfaces.yaml" "$root/contract/errors.yaml" "$here/shipped/contract/"
cp -R "$root/catalogue" "$here/shipped/catalogue"
echo "Copied contract/schemas, contract/interfaces.yaml, contract/errors.yaml and catalogue to packages/rust/shipped"
