# PoC Code for B02 Motivation

This directory contains Proof-of-Concept code for the B02 Motivation experiment
(see `docx/prompt/B02_Motivation_Prompt.md`).

Goal: validate whether the "Rich State" fields described in Section 5.3 of
the prompt can actually be extracted from a serving engine (vLLM / llama.cpp).

## Subdirectories

- `state_extraction/` - PoC for extracting state from serving engines.

## Conventions

- All Python code lives here under `poc/`, never outside `~/B02`.
- Heavy artifacts (Python cache, venv, logs) are gitignored.
- Models are downloaded to `\~/.cache/huggingface/` (outside this repo).
