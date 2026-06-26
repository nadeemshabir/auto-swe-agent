# Claude's Role in the Autonomous SWE Agent Project

This document outlines how Claude, the AI assistant, is being used to guide and accelerate the development of the autonomous SWE agent.

## Project Guidance

Claude is acting as a knowledgeable senior engineer providing end-to-end guidance on the 8-week project:

- Breaking down the high-level project brief into actionable weekly milestones
- Explaining key concepts like the ReAct loop, RAG, sandbox security, observability 
- Recommending specific libraries, tools, and design patterns for each component
- Providing code samples and scaffolding to jumpstart development
- Answering conceptual and debugging questions throughout the project

## Code Generation

While the core development is done by the human engineer (Nadeem), Claude assists by generating code snippets and templates on demand:

- Initial versions of tricky components like the Dockerfile, K8s manifests, GitHub Actions CI
- Idiomatic usage samples for new libraries like tree-sitter, ChromaDB, Celery
- Starter implementations for complex logic like the budget controller and context assembler
- Adaptations of reference code to fit the project's specific structure and needs

Nadeem then reviews, refines, and integrates these snippets into the production codebase.

## Design & Architecture Advice

Claude provides input on key design and architectural decisions:

- Choosing between PaddleOCR vs Claude Vision vs a hybrid approach for invoice extraction
- Defining the schema and API contract between the backend and frontend 
- Recommending Prometheus metrics and Grafana dashboard designs
- Advising on Docker security settings and Kubernetes resource constraints
- Reviewing and critiquing design docs and ADRs (Architecture Decision Records)

## Pair Programming & Code Review

In pair programming sessions, Claude and Nadeem work together to:

- Reason through hairy issues like cross-group fallback matching in the reconciliation stage
- Catch bugs like bill number normalization breaking deduplication
- Refactor complex code into clean abstractions like the `Config` dataclass 
- Optimize performance bottlenecks identified via profiling
- Brainstorm edge cases and recommend defensive coding practices

Claude also conducts lightweight code reviews, offering feedback on modularity, readability, error handling, and adherence to language idioms and style guides.

## Documentation & Knowledge Sharing 

Claude assists with writing and refining documentation:

- README.md overviews and "Getting Started" guides 
- Architecture diagrams and component specs
- Code comments and docstrings
- Tutorials and cookbooks illustrating key workflows
- Evaluation methodologies and benchmark results

This helps make the codebase more understandable and maintainable for Nadeem and future contributors.

