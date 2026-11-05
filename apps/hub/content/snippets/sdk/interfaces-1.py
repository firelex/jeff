# offline
import jeff

catalogue = jeff.Catalogue.shipped()

for interface in ["classify", "route", "rate", "detect", "is_supported", "equivalent", "requires_escalation"]:
    functions = [function.id for function in catalogue.implementing(interface)]

    print(f"{interface}: {len(functions)} functions, for example {functions[0]}")
