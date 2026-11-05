#!/usr/bin/env bash
# Create the GPG key that signs the Java SDK for Maven Central. Run it once, on the Mac, by hand:
#
#     bash scripts/make-gpg-key.sh
#
# What it does, stopping at the first problem:
# 1. Refuses when a secret key for the signing identity already exists (there must be exactly one; delete the old one
#    first if it is really to be replaced) or when the Keychain already holds MAVEN_GPG_PASSPHRASE.
# 2. Makes a random passphrase and stores it in the login Keychain as the generic password MAVEN_GPG_PASSPHRASE
#    (service and account both MAVEN_GPG_PASSPHRASE). It is never printed and never on a command line: it goes to
#    `security` on its standard input and to gpg on a file descriptor.
# 3. Creates an RSA 4096 signing key (primary key, sign only, no expiry) for the identity below in your default GnuPG
#    home (~/.gnupg, or $GNUPGHOME if you set it). RSA, because every verifier of Maven Central signatures reads it.
# 4. Uploads the public key to keys.openpgp.org and keyserver.ubuntu.com, which Maven Central reads. keys.openpgp.org
#    then emails mstrasser@scissero.com a link: click it, or that server publishes the key without its name and email.
# 5. Prints the key's fingerprint.
#
# Keep a backup of the secret key somewhere safe (gpg --export-secret-keys --armor <fingerprint>); without it, a later
# release must be signed with a new key.

set -euo pipefail

IDENTITY="Casperaki Multimedia Solutions Ltd. <mstrasser@scissero.com>"
EMAIL="mstrasser@scissero.com"
ITEM="MAVEN_GPG_PASSPHRASE"
KEYSERVERS=("hkps://keys.openpgp.org" "hkps://keyserver.ubuntu.com")

fail() { echo "make-gpg-key: $*" >&2; exit 1; }

[ "$(uname)" = "Darwin" ] || fail "run this on the Mac: the passphrase goes into the macOS Keychain"
command -v gpg >/dev/null || fail "gpg is not installed (brew install gnupg)"
command -v security >/dev/null || fail "the macOS security command is missing"

if gpg --batch --list-secret-keys --with-colons "<$EMAIL>" >/dev/null 2>&1; then
  fail "a secret key for <$EMAIL> already exists (gpg --list-secret-keys \"<$EMAIL>\"); refusing to make a second"
fi
if security find-generic-password -s "$ITEM" >/dev/null 2>&1; then
  fail "the Keychain already holds $ITEM; refusing to replace it (delete it first if the old key is gone)"
fi

# 32 random bytes as hex: no character the Keychain command line or gpg could misread.
passphrase="$(openssl rand -hex 32)"
[ "${#passphrase}" -eq 64 ] || fail "openssl did not produce a passphrase"

# `security -i` reads its command from standard input, so the passphrase is never in a process's arguments.
printf 'add-generic-password -U -s %s -a %s -l "%s (signs the Jeff Java SDK for Maven Central)" -w %s\n' \
  "$ITEM" "$ITEM" "$ITEM" "$passphrase" | security -i || fail "could not store $ITEM in the Keychain"
stored="$(security find-generic-password -s "$ITEM" -w)"
[ "$stored" = "$passphrase" ] || fail "the Keychain's $ITEM does not match the passphrase just made; delete it and run again"
unset stored

gpg --batch --pinentry-mode loopback --passphrase-fd 3 \
  --quick-generate-key "$IDENTITY" rsa4096 sign never 3< <(printf '%s' "$passphrase") \
  || fail "gpg could not create the key"
unset passphrase

fingerprint="$(gpg --batch --list-secret-keys --with-colons "<$EMAIL>" | awk -F: '$1 == "fpr" { print $10; exit }')"
[ -n "$fingerprint" ] || fail "the new key's fingerprint could not be read"

for server in "${KEYSERVERS[@]}"; do
  gpg --batch --keyserver "$server" --send-keys "$fingerprint" || fail "could not upload the public key to $server"
  echo "Uploaded the public key to $server"
done

echo
echo "Signing key: $IDENTITY"
echo "Fingerprint: $fingerprint"
echo "Passphrase:  in the Keychain as $ITEM (not shown)"
echo
echo "Next: open the link keys.openpgp.org emails to $EMAIL, so it publishes the key with its identity."
echo "Back up the secret key: gpg --export-secret-keys --armor $fingerprint > <somewhere safe>"
