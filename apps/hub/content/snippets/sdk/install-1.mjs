// offline
import { Catalogue } from "jeff-sdk";

const catalogue = Catalogue.shipped();

console.log(catalogue.manifests().length, "adapters in the catalogue shipped with jeff-sdk");
