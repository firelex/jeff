# offline
import jeff

catalogue = jeff.Catalogue.shipped()

print(len(catalogue.manifests()), "adapters in the catalogue shipped with jeff")
