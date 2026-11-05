#!/bin/sh
# Copies the contract's schemas, interfaces.yaml and errors.yaml, and catalogue/, from the repository into
# packages/dotnet/shipped/, which the Jeff.Sdk assembly embeds. Run it after changing either; ShippedTests fails when
# the copy is out of date.
set -eu
root=$(cd "$(dirname "$0")/../../.." && pwd)
shipped="$root/packages/dotnet/shipped"
for item in contract/schemas contract/interfaces.yaml contract/errors.yaml catalogue; do
  if [ ! -e "$root/$item" ]; then echo "$root/$item is missing" >&2; exit 1; fi
done
rm -rf "$shipped"
mkdir -p "$shipped/contract"
cp -R "$root/contract/schemas" "$shipped/contract/schemas"
cp "$root/contract/interfaces.yaml" "$root/contract/errors.yaml" "$shipped/contract/"
cp -R "$root/catalogue" "$shipped/catalogue"
echo "Copied contract/schemas, contract/interfaces.yaml, contract/errors.yaml and catalogue to packages/dotnet/shipped"
