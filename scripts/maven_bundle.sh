#!/usr/bin/env bash
# Sign the Java SDK's release files and pack them as the bundle zip the Maven Central Portal takes.
#
#     bash scripts/maven_bundle.sh STAGING OUT.zip < passphrase
#
# STAGING is the Maven repository folder Gradle writes (packages/java/build/staging-deploy, from
# `./gradlew publishMavenJavaPublicationToStagingRepository`): ai/jeffhub/jeff-sdk/<version>/ with the jar, the sources
# and javadoc jars, the POM and the Gradle module file, each with its .md5, .sha1, .sha256 and .sha512.
#
# The key's passphrase is read from standard input (one line), never from the command line; scripts/publish_sdks.sh
# pipes it from the Keychain. The signing key is the one secret key in gpg's keyring (GNUPGHOME, or ~/.gnupg) for
# <mstrasser@scissero.com>: none, or more than one, is an error. Each file gets a detached ASCII signature (.asc), which
# is then verified. The zip holds the version folder only (maven-metadata.xml is left out; Central makes its own).

set -euo pipefail

EMAIL="mstrasser@scissero.com"
GROUP_PATH="ai/jeffhub/jeff-sdk"

fail() { echo "maven_bundle: $*" >&2; exit 1; }

[ "$#" -eq 2 ] || fail "usage: bash scripts/maven_bundle.sh STAGING OUT.zip < passphrase"
staging="$1"
out="$2"
[ -d "$staging/$GROUP_PATH" ] || fail "$staging/$GROUP_PATH is missing; run the Gradle publish to the staging folder first"
case "$out" in /*) ;; *) out="$PWD/$out" ;; esac
[ ! -e "$out" ] || fail "$out exists; remove it first"

versions=("$staging/$GROUP_PATH"/*/)
[ "${#versions[@]}" -eq 1 ] && [ -d "${versions[0]}" ] || fail "$staging/$GROUP_PATH must hold exactly one version folder"
version="$(basename "${versions[0]}")"
folder="$staging/$GROUP_PATH/$version"

IFS= read -r passphrase || true
[ -n "$passphrase" ] || fail "no passphrase on standard input"

# --with-colons prints a fpr line for the primary key and each subkey; the primary's follows its sec line.
primaries="$(gpg --batch --list-secret-keys --with-colons "<$EMAIL>" 2>/dev/null \
  | awk -F: '$1 == "sec" { want = 1; next } $1 == "fpr" && want { print $10; want = 0 }')"
count="$(printf '%s' "$primaries" | grep -c . || true)"
[ "$count" -eq 1 ] || fail "expected one secret key for <$EMAIL> in gpg's keyring, found $count"
key="$primaries"

expected=(
  "jeff-sdk-$version.jar" "jeff-sdk-$version-sources.jar" "jeff-sdk-$version-javadoc.jar"
  "jeff-sdk-$version.pom" "jeff-sdk-$version.module"
)
for name in "${expected[@]}"; do
  [ -f "$folder/$name" ] || fail "$folder/$name is missing"
  for sum in md5 sha1; do
    [ -f "$folder/$name.$sum" ] || fail "$folder/$name.$sum is missing"
  done
  rm -f "$folder/$name.asc"
  gpg --batch --yes --quiet --pinentry-mode loopback --passphrase-fd 3 --local-user "$key" \
    --armor --detach-sign --output "$folder/$name.asc" "$folder/$name" 3< <(printf '%s' "$passphrase") \
    || fail "gpg could not sign $name"
  gpg --batch --quiet --verify "$folder/$name.asc" "$folder/$name" 2>/dev/null || fail "the signature of $name does not verify"
done
unset passphrase

unexpected="$(cd "$folder" && ls | grep -v -E "^jeff-sdk-$version(-sources|-javadoc)?\.(jar|pom|module)(\.(md5|sha1|sha256|sha512|asc))?$" || true)"
[ -z "$unexpected" ] || fail "$folder holds files the release does not: $unexpected"

mkdir -p "$(dirname "$out")"
(cd "$staging" && zip -q -X -r "$out" "$GROUP_PATH/$version") || fail "could not write $out"
echo "Signed with key $key and wrote $out ($(unzip -Z1 "$out" | grep -c -v '/$') files)"
