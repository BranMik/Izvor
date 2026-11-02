# RAG Instructions

## Operating Instructions
- To add a document to an agent-specific RAG system, create a folder named `[agent_id]` inside the `rag_knowledge` folder (if it doesn't exist yet) and place the file there. The file will be added to the vector database automatically on system restart.
- A document will be removed from the vector database after its file deletion and a system restart.
- Any modifications to existing documents will also be synced to the vector database upon system restart.
- **Supported file formats:** .md, .txt, .pdf, .csv (and other standard text-based formats).
- RAG capability is accessible to an agent only if the `knowledge_base_rag` skill is added to their skills in the `config/settings/agent_settings.json` file.

## Security
- For a document in the RAG system to be accessible to an agent, it has to be either in that agent's sub-folder inside the `/rag_knowledge/` folder, or in the `/rag_knowledge/shared/` folder. The agent also has to have the `knowledge_base_rag` skill added.
- **CRITICAL:** If committing your codebase to a public branch or repository, make sure the files from your RAG system are not sent upstream. Your `.gitignore` is by default set up to ignore all except the 3 default files in the `rag_knowledge` folder, but always double-check this, especially if `.gitignore` was modified.
- **PII Masking:** All Personally Identifiable Information (PII) such as names, emails, phone numbers, and addresses within documents are automatically masked. This is achieved through a pre-processing pipeline that utilizes **Microsoft Presidio** for NER-based detection, alongside a **high-performance regex engine** for deterministic PII and secrets. These tools detect and replace sensitive data with generic placeholder tokens (e.g., `[REDACTED_NAME]`, `[REDACTED_EMAIL]`) to ensure data privacy and compliance. Masking can be configured to happen either at document ingestion or at retrieval based on system settings (`pii_masking` in configuration).