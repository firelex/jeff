# Training data sources

Jeff's weights were trained on the sources below. **We release the model weights and code, not the training data.**
Each source keeps its own licence; some are share-alike (CC BY-SA). Licences are shown where our code records them;
for the others, see the source. From v1.2, every training question is checked against all our evaluation sets (the
benchmark panel, JevBench, the long-document test, the voice test, both long-list tests, the chess tests, and the
development and calibration files) by exact state, by option texts unique to one test item, by near-duplicate
passages and by upstream record (the same contract, document or source sentence); overlapping questions are removed.
v1.2's training mix has 284,747 questions.

The LoRA adapters released with v1.2 have their own data; each adapter's model card lists its sources, licences and
how its data was made.

## Public datasets converted into decisions

| Dataset | Source | Licence |
|---|---|---|
| boolq | [google/boolq](https://huggingface.co/datasets/google/boolq) | see source |
| squad2 | [rajpurkar/squad_v2](https://huggingface.co/datasets/rajpurkar/squad_v2) | see source |
| paws | [google-research-datasets/paws](https://huggingface.co/datasets/google-research-datasets/paws) | see source |
| civil_comments | [google/civil_comments](https://huggingface.co/datasets/google/civil_comments) | see source |
| aegis2 | [nvidia/Aegis-AI-Content-Safety-Dataset-2.0](https://huggingface.co/datasets/nvidia/Aegis-AI-Content-Safety-Dataset-2.0) | see source |
| pubmedqa | [qiaojin/PubMedQA](https://huggingface.co/datasets/qiaojin/PubMedQA) | see source |
| vitaminc | https://github.com/TalSchuster/talschuster.github.io/raw/master/static/vitaminc.zip | see source |
| multinli | https://cims.nyu.edu/~sbowman/multinli/multinli_1.0.zip | see source |
| massive | https://amazon-massive-nlu-dataset.s3.amazonaws.com/amazon-massive-dataset-1.1.tar.gz | see source |
| snli | [stanfordnlp/snli](https://huggingface.co/datasets/stanfordnlp/snli) | CC BY-SA-4.0 |
| commonsense_qa | [tau/commonsense_qa](https://huggingface.co/datasets/tau/commonsense_qa) | MIT |
| openbookqa | [allenai/openbookqa](https://huggingface.co/datasets/allenai/openbookqa) | not stated by the source |
| arc_challenge | [allenai/ai2_arc](https://huggingface.co/datasets/allenai/ai2_arc) | CC BY-SA-4.0 |
| arc_easy | [allenai/ai2_arc](https://huggingface.co/datasets/allenai/ai2_arc) | CC BY-SA-4.0 |
| social_iqa | [allenai/social_i_qa](https://huggingface.co/datasets/allenai/social_i_qa) | CC BY-4.0 |
| cosmos_qa | [allenai/cosmos_qa](https://huggingface.co/datasets/allenai/cosmos_qa) | CC BY-4.0 |
| quartz | [allenai/quartz](https://huggingface.co/datasets/allenai/quartz) | CC BY-4.0 |
| qasc | [allenai/qasc](https://huggingface.co/datasets/allenai/qasc) | CC BY-4.0 |
| truthful_qa | [truthfulqa/truthful_qa](https://huggingface.co/datasets/truthfulqa/truthful_qa) | APACHE-2.0 |
| twitter_financial | [zeroshot/twitter-financial-news-sentiment](https://huggingface.co/datasets/zeroshot/twitter-financial-news-sentiment) | MIT |
| liar2 | [chengxuphd/liar2](https://huggingface.co/datasets/chengxuphd/liar2) | APACHE-2.0 |
| halueval_qa | [pminervini/HaluEval](https://huggingface.co/datasets/pminervini/HaluEval) | APACHE-2.0 |
| halueval_dialogue | [pminervini/HaluEval](https://huggingface.co/datasets/pminervini/HaluEval) | APACHE-2.0 |
| halueval_summarization | [pminervini/HaluEval](https://huggingface.co/datasets/pminervini/HaluEval) | APACHE-2.0 |
| wikibio | [potsawee/wiki_bio_gpt3_hallucination](https://huggingface.co/datasets/potsawee/wiki_bio_gpt3_hallucination) | CC BY-SA-3.0 |
| ragtruth_train | [wandb/RAGTruth-processed](https://huggingface.co/datasets/wandb/RAGTruth-processed) | MIT |
| winogrande_train | [allenai/winogrande](https://huggingface.co/datasets/allenai/winogrande) | CC BY |


## Long-list questions (v1.1)

32,250 questions with 20 to 254 options, so the models learn to answer past the 26th option. Built by
[`src/jeff/longlists.py`](../src/jeff/longlists.py):

| Part | Rows | Source | Licence |
|---|---|---|---|
| Made-up lists (destinations, contacts, products, orders, meeting slots, teams, settings) | 20,000 | built in code; all names invented | own |
| MASSIVE intents, all 60 as options | 6,000 | [Amazon MASSIVE 1.1](https://amazon-massive-nlu-dataset.s3.amazonaws.com/amazon-massive-dataset-1.1.tar.gz), English training split | CC BY 4.0 |
| CLINC150 intents, all 150 as options (out-of-scope messages answered with "None of these") | 6,250 | [clinc/clinc_oos](https://huggingface.co/datasets/clinc/clinc_oos) "plus" training split, revision 155b9c710419136e17307b80d0a13e68cd46b4ec | CC BY 3.0 |

Only training splits are used; the test splits of MASSIVE and CLINC150 are never trained on. Results on those two data
sets are therefore not zero-shot for v1.1 and v1.2. In v1.2 the made-up lists are cut to 5,001 questions; the MASSIVE
and CLINC150 lists are all kept.

## Long documents

Long real documents with human labels, as decision rows: ContractNLI (non-disclosure agreements, CC BY 4.0), ConditionalQA (UK government guidance pages; release under BSD-2, pages under the Open Government Licence) and CUAD (commercial contracts annotated by lawyers for clause types, CC BY 4.0).

**Correction:** v1.0 and v1.1 were also trained on MAUD (merger agreements annotated by lawyers, CC BY 4.0), which this
list did not name. MAUD splits its data by question rather than by contract, so its contracts were also in our
long-document test. v1.2 removes every MAUD question (4,309).

## Voice commands (v1.0 and v1.1 only)

Removed from the v1.2 base mix (5,703 questions) and moved into the `nav` adapter. Built in code from Amazon MASSIVE (English voice-assistant commands) and the CMU Pronouncing Dictionary, by swapping
words for others that sound the same, to imitate speech-recognition errors.

## Built in code

- 10,000 probability questions (dice, cards, raffles and so on) with exact probabilities as targets.
- Object-tracking and date-arithmetic questions, generated with exact answers.

## Synthetic questions

About 31,000 questions written and checked by an open-weight teacher model (Qwen3.8-Flash-Next) served locally. No
closed model was used as a teacher for the base models; a closed model was used only to spot-check the quality of a
sample. Some public data sets above contain text their authors generated with closed models (for example the model
responses in RAGTruth). In v1.2, synthetic yes/no families are balanced 50/50, and families where the correct answer
was given away by its length or letter were re-balanced.
