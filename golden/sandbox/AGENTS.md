# AI-DLC Core Workflow (golden-set sandbox install)

This project follows AI-DLC. Work in phases; keep changes minimal and reviewable.

## INCEPTION
Clarify WHAT and WHY before writing code. Read-only: inspect and analyze, do
not modify source. Produce a concise scope/plan. When AI-DLC docs are requested,
write them under `aidlc-docs/`.

## CONSTRUCTION
Decide HOW and implement it. Make the smallest change that satisfies the task.
Do not touch unrelated files. Keep the existing test suite green.

Detailed rules live in `.aidlc-rule-details/`.
