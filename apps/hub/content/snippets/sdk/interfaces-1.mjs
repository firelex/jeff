// offline
import { Catalogue } from "jeff-sdk";

const catalogue = Catalogue.shipped();

for (const iface of ["classify", "route", "rate", "detect", "is_supported", "equivalent", "requires_escalation"]) {
  const functions = catalogue.implementing(iface).map((fn) => fn.id);

  console.log(`${iface}: ${functions.length} functions, for example ${functions[0]}`);
}
