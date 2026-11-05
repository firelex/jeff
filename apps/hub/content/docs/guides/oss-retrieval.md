---
title: Retrieval
---
# Retrieval

The retrieval adapters make the decisions in a search-and-answer pipeline, one step at a time: which source to
search, whether a passage answers the question, whether the passages provide enough evidence, which passage answers
the question best, and whether the written answer is supported by its sources. The first part of this page follows one customer question to a home
energy installer through each step: "How long is the warranty on an Aster 2 heat pump installed after March 2026?"
The second part works with documents: what type a document is, which section a passage comes from, what kind of
clause a contract provision is, and whether a value extracted from a document is right.

Each example below uses an input written for this page and shows the answer returned by the live API. To run one yourself, set `JEFF_API_KEY` as in the [Quick start](/docs/guides/quickstart).

## Choose where to search

[Search source selection](/catalogue/jeff.retrieval.query-route) routes a query to one, several or none of the sources in a menu of
document collections, databases and tools. You pass your sources as the labels:

<!-- example: oss-query-route -->

`warranty_terms` is the source to search. Had the query needed no lookup, or more than one source, `none` or
`several` would say so.

## Check that a passage answers the question

[Passage relevance](/catalogue/jeff.retrieval.passage-relevant) checks, for search and retrieval, whether a passage
answers a question for the right subject and period: yes, partly or no. Here the search returned a passage about the
older model:

<!-- example: oss-passage-relevant -->

`no` means the passage does not answer the question: it is about the Aster 1, not the Aster 2. Exclude it from the material used to
write the answer.

## Check that the passages are enough

[Evidence sufficiency check](/catalogue/jeff.retrieval.evidence-sufficient) checks, for search and retrieval, whether
a set of passages is enough to answer a question: sufficient, insufficient or conflicting. Here are three passages
the search found:

<!-- example: oss-evidence-sufficient -->

`sufficient` means the passages contain the answer (the second one does), so there is no need to search further.
`conflicting` would mean they disagree, and `insufficient` that more searching is needed.

## Pick the passage that answers it

[Passage selection and answer verification](/catalogue/jeff.retrieval.ground) picks the passage that answers a
question, and checks whether an answer is supported by its sources. Those are two functions. The first picks the
passage, from the passages you pass as labels:

<!-- example: oss-ground-rerank -->

`p2` is the key of the passage that answers the question. If none of them did, `none` would say so.

## Check that the answer is supported

The second function of the same adapter checks the answer the pipeline wrote against its sources:

<!-- example: oss-ground -->

`supported` means everything the answer says is in the sources, including the yearly service condition, so it can
be shown to the customer.

## Document classification

[Document classification](/catalogue/jeff.retrieval.doc-type) sorts a document into one of the types on your list (each with a
description), or other. Here a supplier's document arrives in an installer's shared inbox:

<!-- example: oss-doc-type -->

`quote` means the document offers a price before any order; the mention of an invoice to follow does not make it
one.

## Document section classification

[Document section classification](/catalogue/jeff.retrieval.section-id) identifies the section of a document that a passage comes from, using
a list of section types and descriptions, or returns none. Here is a passage from an installation services agreement:

<!-- example: oss-section-id -->

`termination` is the section: the passage is about ending the agreement, even though it also mentions unpaid
invoices.

## Contract clause classification

[Contract clause classification](/catalogue/jeff.legal.legal-clauses) labels a single contract provision with its clause type,
such as governing law, notices or confidentiality. The clause types are the function's own, so there are no labels to
pass:

<!-- example: oss-legal-clauses -->

`governing_laws` is the clause type: the provision names the law that governs the contract.

## Extracted value validation

[Extracted value validation](/catalogue/jeff.retrieval.extract-check) checks a value extracted from a document (a field
and its value) and says whether it is correct, the wrong value, the wrong field, not in the document, or ambiguous.
Here an extraction step read the invoice's total as 420.00:

<!-- example: oss-extract-check -->

`wrong_field` means the value is in the document but belongs to another field: 420.00 is the VAT, not the total.
Extract the field again before storing a value.
