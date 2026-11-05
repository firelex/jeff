// offline
using Jeff.Sdk;

var catalogue = Catalogue.Shipped();

Console.WriteLine($"{catalogue.Manifests().Count} adapters in the catalogue shipped with Jeff.Sdk");
