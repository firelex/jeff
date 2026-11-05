# offline
import jeff

# Both mistakes are refused at once, before any network call or download.
try:
    jeff.Jeff.remote("{{origin}}", token=None, timeout_s=0)
except jeff.InvalidRequest as error:
    print(error)  # timeout_s must be a finite number of seconds above 0; got 0

try:
    jeff.Jeff.local("guard")
except jeff.ManifestError as error:
    print(error)  # Jeff.local() takes a list of adapters, not a single string; ...
