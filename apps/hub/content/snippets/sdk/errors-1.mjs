// offline
import { Catalogue, InvalidReference, JeffError, UnknownFunction } from "jeff-sdk";

const catalogue = Catalogue.shipped();

for (const ref of ["guard.atempt", "guard.attempt.extra", "guard"]) {
  try {
    catalogue.function(ref);
  } catch (error) {
    if (!(error instanceof UnknownFunction || error instanceof InvalidReference)) throw error;
    console.log(error.type, "-", error.message);
  }
}

// Every Jeff error is a JeffError, so one check catches them all; requestId is null for an error raised in this
// process (a server's error carries the id of the request that failed).
try {
  catalogue.function("guard.atempt");
} catch (error) {
  if (!(error instanceof JeffError)) throw error;
  console.log(error.type, error.requestId);
}
