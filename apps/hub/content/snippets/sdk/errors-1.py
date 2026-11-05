# offline
import jeff

catalogue = jeff.Catalogue.shipped()

for ref in ["guard.atempt", "guard.attempt.extra", "guard"]:
    try:
        catalogue.function(ref)
    except (jeff.UnknownFunction, jeff.InvalidReference) as error:
        print(type(error).__name__, "-", error)

# Every Jeff error is a JeffError, so one except clause catches them all; request_id is None for an error raised in
# this process (a server's error carries the id of the request that failed).
try:
    catalogue.function("guard.atempt")
except jeff.JeffError as error:
    print(type(error).__name__, error.request_id)
