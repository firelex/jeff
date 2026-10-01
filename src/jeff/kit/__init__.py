"""The adapter kit: command-line checks for the training data of a Jeff adapter (jeff-kit).

check-rows validates rows, split holds out whole families, leak-check finds evaluation rows copied into training,
shortcut-report looks for surface features that give the answer away, replay-mix adds a share of rows you supply
from the base model's own kind of data, and evaluate runs the three measurements on one test set."""
