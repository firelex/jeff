#!/usr/bin/env bash
# Build, check and publish the Jeff SDKs and the Python engine to their public registries. Run it on the Mac, from a
# clean checkout of main that is on origin/main:
#
#     bash scripts/make-gpg-key.sh            # once: the Maven Central signing key (and MAVEN_GPG_PASSPHRASE)
#     bash scripts/publish_sdks.sh build      # build every package into dist/sdks (signs the Java bundle)
#     bash scripts/publish_sdks.sh check      # everything that must hold before anything is published
#     bash scripts/publish_sdks.sh all        # check, then publish every package, in the order below
#
# or one registry at a time, each after its own checks:
#
#     bash scripts/publish_sdks.sh python     # PyPI: jeff-sdk, then jeff-runtime
#     bash scripts/publish_sdks.sh node       # npm: jeff-sdk
#     bash scripts/publish_sdks.sh rust       # crates.io: jeff-sdk
#     bash scripts/publish_sdks.sh dotnet     # NuGet: Jeff.Sdk
#     bash scripts/publish_sdks.sh java       # Maven Central: ai.jeffhub:jeff-sdk
#
# A version can never be published twice on any of these registries (nor replaced, nor, on most, deleted), so `check`
# finds every mistake first and each publish refuses rather than guess:
# - the working tree has no change to a tracked file and no untracked file in a package's folder, and HEAD is on
#   origin/main (after a fetch);
# - every build is in dist/sdks, made from HEAD with a clean tree;
# - every package has the same version (and the engine pins that version of jeff-sdk), and no registry has it yet;
# - every package declares the same licence and ships a LICENSE file identical to the repository's;
# - every token is in the Keychain and the registries that can say so accept it;
# - the Java bundle is signed by the release key, whose public key is on the key servers Maven Central reads;
# - no package holds the name of a private adapter or anything that looks like a secret (scan_sdk_artifacts.py).
#
# Tokens come from the login Keychain (generic passwords, found by service name): PYPI_TOKEN, NPM_TOKEN, CARGO_TOKEN,
# NUGET_API_KEY, MAVEN_CENTRAL_USERNAME, MAVEN_CENTRAL_PASSWORD and MAVEN_GPG_PASSPHRASE. A token is never printed and
# never in a command's arguments: it reaches a tool in an environment variable set for that one command, or in a
# header file in a private temporary folder (mode 600) that is deleted on exit.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/dist/sdks"
JAVA_HOME_PATH="/opt/homebrew/opt/openjdk@27/libexec/openjdk.jdk/Contents/Home"
RUSTUP_BIN="/opt/homebrew/opt/rustup/bin"
TWINE="twine==7.0.0"
SIGNING_EMAIL="mstrasser@scissero.com"
USER_AGENT="jeff-publish-sdks (mstrasser@scissero.com)"
NPM_OWNER="teamsuzie"
PACKAGE_DIRS=(packages/python runtime/python packages/node packages/rust packages/dotnet packages/java)
TOKENS=(PYPI_TOKEN NPM_TOKEN CARGO_TOKEN NUGET_API_KEY MAVEN_CENTRAL_USERNAME MAVEN_CENTRAL_PASSWORD MAVEN_GPG_PASSPHRASE)

fail() { echo "publish_sdks: $*" >&2; exit 1; }
step() { echo "== $*"; }

# --- helpers ---------------------------------------------------------------------------------------------------------

WORK="$(mktemp -d -t jeff-publish)"
chmod 700 "$WORK"
trap 'rm -rf "$WORK"' EXIT

# The value of a Keychain item, into the named shell variable (never echoed).
secret() {
  local name="$1" var="$2" value
  value="$(security find-generic-password -s "$name" -w 2>/dev/null)" || fail "the Keychain has no item $name"
  [ -n "$value" ] || fail "the Keychain item $name is empty"
  printf -v "$var" '%s' "$value"
}

# An HTTP status code for a URL; anything but the two answers expected is an error.
status_of() {
  curl -sS -o /dev/null -w '%{http_code}' -A "$USER_AGENT" --max-time 60 "$1" || fail "cannot reach $1"
}

# Refuses unless a version is absent from a registry: 404 is absent, 200 is already published, anything else is
# an error (the registry could not say).
must_be_absent() {
  local what="$1" url="$2" code
  code="$(status_of "$url")"
  case "$code" in
    404) echo "   $what: not published yet" ;;
    200) fail "$what is already published ($url); a version can never be published twice, so raise the version" ;;
    *) fail "$what: $url answered $code, so whether it is published is unknown" ;;
  esac
}

# A header file with one header line, readable only by this user; curl reads it with -H @file.
header_file() {
  local file="$WORK/$1" line="$2"
  (umask 077 && printf '%s\n' "$line" > "$file")
  echo "$file"
}

value_of() {  # value_of FILE REGEX: the first capture group of the first matching line
  local file="$1" regex="$2" value
  value="$(sed -n -E "s#$regex#\\1#p" "$ROOT/$file" | head -1)"
  [ -n "$value" ] || fail "no version found in $file"
  echo "$value"
}

# --- versions --------------------------------------------------------------------------------------------------------

read_versions() {
  V_PY="$(value_of packages/python/pyproject.toml '^version = "([^"]+)"')"
  V_RT="$(value_of runtime/python/pyproject.toml '^version = "([^"]+)"')"
  V_RT_PIN="$(value_of runtime/python/pyproject.toml '^  "jeff-sdk==([^"]+)",')"
  V_NODE="$(cd "$ROOT/packages/node" && node -p 'require("./package.json").version')"
  V_RUST="$(value_of packages/rust/Cargo.toml '^version = "([^"]+)"')"
  V_DOTNET="$(value_of packages/dotnet/src/Jeff.Sdk/Jeff.Sdk.csproj '^ *<Version>([^<]+)</Version>')"
  V_JAVA="$(value_of packages/java/build.gradle.kts '^version = "([^"]+)"')"
  V_HUB_JAVA="$(value_of apps/hub/content/sdk-languages.yaml '^  package: ai\.jeffhub:jeff-sdk:(.+)$')"
  VERSION="$V_PY"
  local name value bad=""
  for name in V_RT V_RT_PIN V_NODE V_RUST V_DOTNET V_JAVA V_HUB_JAVA; do
    value="${!name}"
    [ "$value" = "$VERSION" ] || bad="$bad $name=$value"
  done
  [ -z "$bad" ] || fail "the versions disagree: packages/python has $VERSION, but$bad"
  [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "version $VERSION is not MAJOR.MINOR.PATCH"
}

# The files a build makes, for version $VERSION.
artifacts() {
  echo "$OUT/python/jeff_sdk-$VERSION-py3-none-any.whl"
  echo "$OUT/python/jeff_sdk-$VERSION.tar.gz"
  echo "$OUT/python/jeff_runtime-$VERSION-py3-none-any.whl"
  echo "$OUT/python/jeff_runtime-$VERSION.tar.gz"
  echo "$OUT/node/jeff-sdk-$VERSION.tgz"
  echo "$OUT/rust/jeff-sdk-$VERSION.crate"
  echo "$OUT/nuget/Jeff.Sdk.$VERSION.nupkg"
  echo "$OUT/java/jeff-sdk-$VERSION-bundle.zip"
}

# --- the tree --------------------------------------------------------------------------------------------------------

tree_problems() {  # prints what makes the tree unfit to publish from; nothing when it is fit
  (cd "$ROOT" && git status --porcelain --untracked-files=no)
  (cd "$ROOT" && git status --porcelain --untracked-files=all -- "${PACKAGE_DIRS[@]}" contract catalogue | grep '^??' || true)
}

require_publishable_tree() {
  local problems
  problems="$(tree_problems)"
  [ -z "$problems" ] || fail "the working tree is not clean; commit or remove these first:
$problems"
  (cd "$ROOT" && git fetch --quiet origin main) || fail "cannot fetch origin main"
  (cd "$ROOT" && git merge-base --is-ancestor HEAD origin/main) \
    || fail "HEAD ($(cd "$ROOT" && git rev-parse --short HEAD)) is not on origin/main; push it first"
  echo "   tree: clean, $(cd "$ROOT" && git rev-parse --short HEAD) is on origin/main"
}

require_builds() {
  read_versions
  [ -f "$OUT/BUILD" ] || fail "$OUT/BUILD is missing; run: bash scripts/publish_sdks.sh build"
  local commit dirty version head
  commit="$(sed -n 's/^commit=//p' "$OUT/BUILD")"
  dirty="$(sed -n 's/^dirty=//p' "$OUT/BUILD")"
  version="$(sed -n 's/^version=//p' "$OUT/BUILD")"
  head="$(cd "$ROOT" && git rev-parse HEAD)"
  [ "$commit" = "$head" ] || fail "the builds are of ${commit:0:7}, not HEAD (${head:0:7}); run the build again"
  [ "$dirty" = "no" ] || fail "the builds were made from a tree with changes; run the build again from a clean tree"
  [ "$version" = "$VERSION" ] || fail "the builds are version $version, the packages say $VERSION; run the build again"
  local file
  while read -r file; do
    [ -f "$file" ] || fail "$file is missing; run the build again"
  done < <(artifacts)
  echo "   builds: all present, version $VERSION, from ${commit:0:7}"
}

# --- the licence -----------------------------------------------------------------------------------------------------

# Every package declares the same licence (an SPDX id) and ships the LICENSE file, in its folder and in what is built.
check_licence() {
  local ids=() id file
  id="$(sed -n -E 's#^license = "([^"]+)"$#\1#p' "$ROOT/packages/python/pyproject.toml")"; ids+=("packages/python=$id")
  id="$(sed -n -E 's#^license = "([^"]+)"$#\1#p' "$ROOT/runtime/python/pyproject.toml")"; ids+=("runtime/python=$id")
  id="$(cd "$ROOT/packages/node" && node -p 'require("./package.json").license || ""')"; ids+=("packages/node=$id")
  id="$(sed -n -E 's#^license = "([^"]+)"$#\1#p' "$ROOT/packages/rust/Cargo.toml")"; ids+=("packages/rust=$id")
  id="$(sed -n -E 's#^ *<PackageLicenseExpression>([^<]+)</PackageLicenseExpression>#\1#p' \
    "$ROOT/packages/dotnet/src/Jeff.Sdk/Jeff.Sdk.csproj")"; ids+=("packages/dotnet=$id")
  id="$(sed -n -E 's#^ *name.set\("([A-Za-z0-9.-]+) License"\)#\1#p' "$ROOT/packages/java/build.gradle.kts")"
  ids+=("packages/java=$id")
  LICENCE="${ids[0]#*=}"
  [ -n "$LICENCE" ] || fail "packages/python declares no licence; the owner decides it before anything is published"
  local entry
  for entry in "${ids[@]}"; do
    [ "${entry#*=}" = "$LICENCE" ] || fail "the licences disagree: packages/python declares $LICENCE, ${entry%%=*} '${entry#*=}'"
  done
  [ -f "$ROOT/LICENSE" ] || fail "the repository has no LICENSE"
  for file in "${PACKAGE_DIRS[@]}"; do
    [ -f "$ROOT/$file/LICENSE" ] || fail "$file/LICENSE is missing"
    cmp -s "$ROOT/LICENSE" "$ROOT/$file/LICENSE" || fail "$file/LICENSE differs from the repository's LICENSE"
  done
  echo "   licence: every package declares $LICENCE and has its LICENSE"
}

check_licence_in_builds() {
  # Each listing is read whole before it is searched: `list | grep -q` stops reading at the first match, the lister
  # then dies of SIGPIPE, and with pipefail that reads as "not found" even when the file is there.
  listed() {  # listed PATTERN COMMAND...: true when the command's whole output has a line matching PATTERN
    local pattern=$1 out; shift
    out="$("$@")" || fail "could not list $*"
    grep -q -- "$pattern" <<<"$out"
  }
  listed "^jeff_sdk-$VERSION.dist-info/licenses/LICENSE$" unzip -Z1 "$OUT/python/jeff_sdk-$VERSION-py3-none-any.whl" \
    || fail "the jeff-sdk wheel has no LICENSE"
  listed "^jeff_runtime-$VERSION.dist-info/licenses/LICENSE$" unzip -Z1 "$OUT/python/jeff_runtime-$VERSION-py3-none-any.whl" \
    || fail "the jeff-runtime wheel has no LICENSE"
  listed '^package/LICENSE$' tar tzf "$OUT/node/jeff-sdk-$VERSION.tgz" || fail "the npm package has no LICENSE"
  listed "^jeff-sdk-$VERSION/LICENSE$" tar tzf "$OUT/rust/jeff-sdk-$VERSION.crate" || fail "the crate has no LICENSE"
  listed '^LICENSE$' unzip -Z1 "$OUT/nuget/Jeff.Sdk.$VERSION.nupkg" || fail "the NuGet package has no LICENSE"
  listed "<name>$LICENCE License</name>" unzip -p "$OUT/java/jeff-sdk-$VERSION-bundle.zip" "ai/jeffhub/jeff-sdk/$VERSION/jeff-sdk-$VERSION.pom" \
    || fail "the Java POM does not name the $LICENCE licence"
  echo "   licence: every build carries it"
}

# --- build -----------------------------------------------------------------------------------------------------------

cmd_build() {
  read_versions
  local head dirty="no"
  head="$(cd "$ROOT" && git rev-parse HEAD)"
  [ -z "$(tree_problems)" ] || dirty="yes"
  [ -d "$JAVA_HOME_PATH" ] || fail "no JDK at $JAVA_HOME_PATH (brew install openjdk@27)"
  [ -x "$RUSTUP_BIN/cargo" ] || fail "no cargo at $RUSTUP_BIN (brew install rustup)"
  local passphrase
  secret MAVEN_GPG_PASSPHRASE passphrase
  rm -rf "$OUT"
  mkdir -p "$OUT"/{python,node,rust,nuget,java}

  step "Python: jeff-sdk and jeff-runtime $VERSION"
  (cd "$ROOT" && nice -n 15 uv build --package jeff-sdk --out-dir "$OUT/python" -q \
    && nice -n 15 uv build --package jeff-runtime --out-dir "$OUT/python" -q) || fail "uv build failed"
  (cd "$ROOT" && nice -n 15 uvx --quiet --from "$TWINE" twine check --strict "$OUT"/python/*) || fail "twine check failed"

  step "Node: jeff-sdk $VERSION"
  (cd "$ROOT/packages/node" && nice -n 15 npm pack --silent --pack-destination "$OUT/node" >/dev/null) || fail "npm pack failed"

  step "Rust: jeff-sdk $VERSION"
  local allow=()
  [ "$dirty" = "no" ] || allow=(--allow-dirty)
  (cd "$ROOT/packages/rust" && PATH="$RUSTUP_BIN:$PATH" nice -n 15 cargo package -j 2 --locked ${allow[@]+"${allow[@]}"} -q) \
    || fail "cargo package failed"
  cp "$ROOT/packages/rust/target/package/jeff-sdk-$VERSION.crate" "$OUT/rust/"

  step "C#: Jeff.Sdk $VERSION"
  # (the folder is nuget, not dotnet: dotnet pack 10 misreads an --output path that ends in /dotnet)
  (cd "$ROOT/packages/dotnet" && nice -n 15 dotnet pack src/Jeff.Sdk/Jeff.Sdk.csproj -c Release \
    --output "$OUT/nuget" -m:1 -nologo --verbosity quiet) || fail "dotnet pack failed"

  step "Java: ai.jeffhub:jeff-sdk $VERSION"
  (cd "$ROOT/packages/java" && JAVA_HOME="$JAVA_HOME_PATH" nice -n 15 ./gradlew --no-daemon --max-workers=1 -q \
    publishMavenJavaPublicationToStagingRepository) || fail "the Gradle publish to the staging folder failed"
  printf '%s\n' "$passphrase" | bash "$ROOT/scripts/maven_bundle.sh" "$ROOT/packages/java/build/staging-deploy" \
    "$OUT/java/jeff-sdk-$VERSION-bundle.zip"
  unset passphrase

  printf 'commit=%s\ndirty=%s\nversion=%s\n' "$head" "$dirty" "$VERSION" > "$OUT/BUILD"
  local file
  while read -r file; do
    [ -f "$file" ] || fail "the build did not make $file"
  done < <(artifacts)
  echo "Built version $VERSION from ${head:0:7} (tree with changes: $dirty) into $OUT"
}

# --- checks ----------------------------------------------------------------------------------------------------------

check_tokens() {
  local name
  for name in "${TOKENS[@]}"; do
    security find-generic-password -s "$name" >/dev/null 2>&1 || fail "the Keychain has no item $name"
  done
  echo "   tokens: all ${#TOKENS[@]} are in the Keychain"

  local token code
  secret NPM_TOKEN token
  printf '//registry.npmjs.org/:_authToken=${NPM_TOKEN}\n' > "$WORK/npmrc"
  chmod 600 "$WORK/npmrc"
  local who
  who="$(NPM_TOKEN="$token" npm whoami --userconfig "$WORK/npmrc" --registry https://registry.npmjs.org/ 2>/dev/null)" \
    || fail "npm does not accept NPM_TOKEN"
  [ "$who" = "$NPM_OWNER" ] || fail "NPM_TOKEN belongs to $who, not $NPM_OWNER (who owns jeff-sdk on npm)"
  echo "   npm: the token is $NPM_OWNER's"

  secret CARGO_TOKEN token
  # /me answers 200 to a full token, and 403 "can only be performed on the crates.io website" to a valid token scoped
  # to publishing (publish-new, publish-update), which is what a release uses; a wrong token gets another 403.
  code="$(curl -sS -o "$WORK/cargo-me.json" -w '%{http_code}' -A "$USER_AGENT" --max-time 60 \
    -H @"$(header_file cargo-header "Authorization: $token")" https://crates.io/api/v1/me)" || fail "cannot reach crates.io"
  if [ "$code" = "200" ]; then
    echo "   crates.io: the token is accepted"
  elif [ "$code" = "403" ] && grep -q 'can only be performed on the crates.io website' "$WORK/cargo-me.json"; then
    echo "   crates.io: the token is accepted (scoped to publishing)"
  else
    fail "crates.io does not accept CARGO_TOKEN (it answered $code: $(head -c 200 "$WORK/cargo-me.json"))"
  fi

  local user password
  secret MAVEN_CENTRAL_USERNAME user
  secret MAVEN_CENTRAL_PASSWORD password
  central_header "$user" "$password"
  unset user password
  code="$(curl -sS -o "$WORK/published.json" -w '%{http_code}' --max-time 60 -H @"$CENTRAL_HEADER" \
    "https://central.sonatype.com/api/v1/publisher/published?namespace=ai.jeffhub&name=jeff-sdk&version=$VERSION")" \
    || fail "cannot reach central.sonatype.com"
  [ "$code" = "200" ] || fail "the Central Portal does not accept MAVEN_CENTRAL_USERNAME and MAVEN_CENTRAL_PASSWORD (it answered $code)"
  [ "$(jq -r .published "$WORK/published.json")" = "false" ] || fail "ai.jeffhub:jeff-sdk:$VERSION is already on Maven Central"
  echo "   Maven Central: the token is accepted"
  unset token
  # PyPI and NuGet have no way to test a token without publishing: a wrong one fails the upload, before anything is
  # published.
}

CENTRAL_HEADER=""
central_header() {  # the Central Portal's header: Bearer base64(username:password)
  local encoded
  encoded="$(printf '%s:%s' "$1" "$2" | base64 | tr -d '\n')"
  CENTRAL_HEADER="$(header_file central-header "Authorization: Bearer $encoded")"
}

check_registries() {
  must_be_absent "PyPI jeff-sdk $VERSION" "https://pypi.org/pypi/jeff-sdk/$VERSION/json"
  must_be_absent "PyPI jeff-runtime $VERSION" "https://pypi.org/pypi/jeff-runtime/$VERSION/json"
  must_be_absent "npm jeff-sdk $VERSION" "https://registry.npmjs.org/jeff-sdk/$VERSION"
  must_be_absent "crates.io jeff-sdk $VERSION" "https://crates.io/api/v1/crates/jeff-sdk/$VERSION"
  must_be_absent "NuGet Jeff.Sdk $VERSION" "https://api.nuget.org/v3-flatcontainer/jeff.sdk/$VERSION/jeff.sdk.nuspec"
  must_be_absent "Maven Central ai.jeffhub:jeff-sdk $VERSION" \
    "https://repo1.maven.org/maven2/ai/jeffhub/jeff-sdk/$VERSION/jeff-sdk-$VERSION.pom"
}

check_signing() {
  local primaries count key
  primaries="$(gpg --batch --list-secret-keys --with-colons "<$SIGNING_EMAIL>" 2>/dev/null \
    | awk -F: '$1 == "sec" { want = 1; next } $1 == "fpr" && want { print $10; want = 0 }')"
  count="$(printf '%s' "$primaries" | grep -c . || true)"
  [ "$count" -eq 1 ] || fail "expected one secret key for <$SIGNING_EMAIL>, found $count (run scripts/make-gpg-key.sh)"
  key="$primaries"
  local code
  code="$(status_of "https://keys.openpgp.org/vks/v1/by-fingerprint/$key")"
  [ "$code" = "200" ] || fail "keys.openpgp.org does not have the public key $key (it answered $code)"
  code="$(status_of "https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x$key")"
  [ "$code" = "200" ] || fail "keyserver.ubuntu.com does not have the public key $key (it answered $code)"
  rm -rf "$WORK/bundle" && mkdir "$WORK/bundle"
  (cd "$WORK/bundle" && unzip -q "$OUT/java/jeff-sdk-$VERSION-bundle.zip") || fail "cannot unpack the Java bundle"
  local folder="$WORK/bundle/ai/jeffhub/jeff-sdk/$VERSION" asc signer
  for asc in "$folder"/*.asc; do
    signer="$(gpg --batch --status-fd 1 --verify "$asc" "${asc%.asc}" 2>/dev/null | awk '$2 == "VALIDSIG" { print $3 }')"
    [ "$signer" = "$key" ] || fail "$(basename "$asc") is not a valid signature by the release key $key"
  done
  echo "   Java bundle: $(ls "$folder"/*.asc | wc -l | tr -d ' ') files signed by $key, which both key servers have"
}

cmd_check() {
  step "Check"
  require_publishable_tree
  require_builds
  check_licence
  check_licence_in_builds
  check_registries
  check_tokens
  check_signing
  local files=()
  while read -r file; do files+=("$file"); done < <(artifacts)
  (cd "$ROOT" && nice -n 15 uv run --no-sync python scripts/scan_sdk_artifacts.py "${files[@]}") \
    || fail "the packages hold private names or secrets"
  echo "Everything checks: version $VERSION is ready to publish"
}

# --- publish ---------------------------------------------------------------------------------------------------------

before_publish() {
  require_publishable_tree
  require_builds
  check_licence
  check_licence_in_builds
}

cmd_python() {
  step "PyPI"
  before_publish
  must_be_absent "PyPI jeff-sdk $VERSION" "https://pypi.org/pypi/jeff-sdk/$VERSION/json"
  must_be_absent "PyPI jeff-runtime $VERSION" "https://pypi.org/pypi/jeff-runtime/$VERSION/json"
  local token
  secret PYPI_TOKEN token
  # jeff-sdk first: jeff-runtime requires it.
  (cd "$ROOT" && TWINE_USERNAME=__token__ TWINE_PASSWORD="$token" TWINE_NON_INTERACTIVE=1 uvx --quiet --from "$TWINE" \
    twine upload "$OUT/python/jeff_sdk-$VERSION-py3-none-any.whl" "$OUT/python/jeff_sdk-$VERSION.tar.gz") \
    || fail "the upload of jeff-sdk to PyPI failed"
  echo "Published jeff-sdk $VERSION: https://pypi.org/project/jeff-sdk/$VERSION/"
  (cd "$ROOT" && TWINE_USERNAME=__token__ TWINE_PASSWORD="$token" TWINE_NON_INTERACTIVE=1 uvx --quiet --from "$TWINE" \
    twine upload "$OUT/python/jeff_runtime-$VERSION-py3-none-any.whl" "$OUT/python/jeff_runtime-$VERSION.tar.gz") \
    || fail "the upload of jeff-runtime to PyPI failed (jeff-sdk $VERSION is already published)"
  unset token
  echo "Published jeff-runtime $VERSION: https://pypi.org/project/jeff-runtime/$VERSION/"
}

cmd_node() {
  step "npm"
  before_publish
  must_be_absent "npm jeff-sdk $VERSION" "https://registry.npmjs.org/jeff-sdk/$VERSION"
  local token
  secret NPM_TOKEN token
  # npm reads ${NPM_TOKEN} from the environment when it reads this file, so the file never holds the token.
  printf '//registry.npmjs.org/:_authToken=${NPM_TOKEN}\n' > "$WORK/npmrc"
  chmod 600 "$WORK/npmrc"
  NPM_TOKEN="$token" npm publish "$OUT/node/jeff-sdk-$VERSION.tgz" --userconfig "$WORK/npmrc" \
    --registry https://registry.npmjs.org/ --access public --tag latest || fail "npm publish failed"
  unset token
  echo "Published jeff-sdk $VERSION: https://www.npmjs.com/package/jeff-sdk/v/$VERSION"
}

cmd_rust() {
  step "crates.io"
  before_publish
  must_be_absent "crates.io jeff-sdk $VERSION" "https://crates.io/api/v1/crates/jeff-sdk/$VERSION"
  local token
  secret CARGO_TOKEN token
  # cargo publish packages the crate again from this clean tree (the same files as the build's .crate) and uploads it.
  (cd "$ROOT/packages/rust" && CARGO_REGISTRY_TOKEN="$token" PATH="$RUSTUP_BIN:$PATH" nice -n 15 \
    cargo publish -j 2 --locked) || fail "cargo publish failed"
  unset token
  echo "Published jeff-sdk $VERSION: https://crates.io/crates/jeff-sdk/$VERSION"
}

cmd_dotnet() {
  step "NuGet"
  before_publish
  must_be_absent "NuGet Jeff.Sdk $VERSION" "https://api.nuget.org/v3-flatcontainer/jeff.sdk/$VERSION/jeff.sdk.nuspec"
  local token header code
  secret NUGET_API_KEY token
  # NuGet's push endpoint, with the key in a header file (dotnet nuget push takes it only as an argument).
  header="$(header_file nuget-header "X-NuGet-ApiKey: $token")"
  unset token
  # nuget.org refuses a push without a client version of 4.1.0 or higher ("A client version '4.1.0' or higher is
  # required"), and gives the reason only in the status line, which -D keeps for the failure message.
  code="$(curl -sS -o "$WORK/nuget-answer" -D "$WORK/nuget-headers" -w '%{http_code}' --max-time 600 -X PUT -H @"$header" \
    -H "X-NuGet-Client-Version: 6.12.0" -A "$USER_AGENT" -F "package=@$OUT/nuget/Jeff.Sdk.$VERSION.nupkg" https://www.nuget.org/api/v2/package)" \
    || fail "cannot reach nuget.org"
  case "$code" in
    201|202) ;;
    *) fail "NuGet refused the package: $(head -1 "$WORK/nuget-headers" | tr -d '\r') $(head -c 300 "$WORK/nuget-answer")" ;;
  esac
  echo "Published Jeff.Sdk $VERSION: https://www.nuget.org/packages/Jeff.Sdk/$VERSION (NuGet indexes it within minutes)"
}

cmd_java() {
  step "Maven Central"
  before_publish
  must_be_absent "Maven Central ai.jeffhub:jeff-sdk $VERSION" \
    "https://repo1.maven.org/maven2/ai/jeffhub/jeff-sdk/$VERSION/jeff-sdk-$VERSION.pom"
  local user password code id state
  secret MAVEN_CENTRAL_USERNAME user
  secret MAVEN_CENTRAL_PASSWORD password
  central_header "$user" "$password"
  unset user password
  code="$(curl -sS -o "$WORK/central-id" -w '%{http_code}' --max-time 600 -X POST -H @"$CENTRAL_HEADER" \
    -F "bundle=@$OUT/java/jeff-sdk-$VERSION-bundle.zip" \
    "https://central.sonatype.com/api/v1/publisher/upload?name=ai.jeffhub:jeff-sdk:$VERSION&publishingType=AUTOMATIC")" \
    || fail "cannot reach central.sonatype.com"
  [ "$code" = "201" ] || fail "the Central Portal refused the bundle (HTTP $code): $(cat "$WORK/central-id")"
  id="$(cat "$WORK/central-id")"
  echo "   uploaded as deployment $id; waiting for Central to validate and publish it"
  local waited=0
  while :; do
    code="$(curl -sS -o "$WORK/central-status" -w '%{http_code}' --max-time 60 -X POST -H @"$CENTRAL_HEADER" \
      "https://central.sonatype.com/api/v1/publisher/status?id=$id")" || fail "cannot reach central.sonatype.com"
    [ "$code" = "200" ] || fail "the Central Portal's status of deployment $id answered HTTP $code"
    state="$(jq -r .deploymentState "$WORK/central-status")"
    case "$state" in
      PUBLISHED) break ;;
      FAILED) fail "Central refused deployment $id: $(jq -c .errors "$WORK/central-status")" ;;
      PENDING|VALIDATING|VALIDATED|PUBLISHING) ;;
      *) fail "deployment $id is in a state this script does not know: $state" ;;
    esac
    [ "$waited" -lt 3600 ] || fail "deployment $id is still $state after an hour; see https://central.sonatype.com/publishing/deployments"
    sleep 30
    waited=$((waited + 30))
  done
  echo "Published ai.jeffhub:jeff-sdk $VERSION: https://central.sonatype.com/artifact/ai.jeffhub/jeff-sdk/$VERSION"
}

cmd_all() {
  cmd_check
  cmd_python
  cmd_node
  cmd_rust
  cmd_dotnet
  cmd_java
  echo "Published every package, version $VERSION"
}

case "${1:-}" in
  build) cmd_build ;;
  check) cmd_check ;;
  python) cmd_python ;;
  node) cmd_node ;;
  rust) cmd_rust ;;
  dotnet) cmd_dotnet ;;
  java) cmd_java ;;
  all) cmd_all ;;
  *) fail "usage: bash scripts/publish_sdks.sh build|check|python|node|rust|dotnet|java|all" ;;
esac
