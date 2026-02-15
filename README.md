## System Overview
```mermaid
flowchart LR
  U[User] -->|Upload PDF/DOCX| UI[Web UI]
  UI -->|POST /jobs| API[FastAPI Backend]

  API --> JOBS[JobStore\n(in-memory)]
  API --> ART[Artifacts Folder\n/tmp/jobs/<job_id>/artifacts]

  API --> ING[Ingest Service\nPDF/DOCX -> blocks]
  ING -->|contract_text, document_blocks| ART

  ING --> CH[Chunking]
  CH --> EMB[Embeddings Provider]
  EMB -->|vectors| FAISS[FAISS Index]
  CH -->|chunks.json, chunk_meta.json| ART
  FAISS -->|faiss.index| ART

  API --> DA[DeepAgents Orchestrator]
  DA --> PLN[Planner]
  DA --> SUB[Sub-agents\n(area specialists)]
  SUB --> RET[RAG Retrieval\n(FAISS top-k)]
  RET --> LLM[LLM Client\n(Mock / Azure OpenAI)]
  LLM --> SUB
  SUB --> NORM[Normalize + Validate]
  NORM --> SCORE[Quality Scoring]
  SCORE --> EXP[Export CSV/XLSX\n(SharePoint schema)]
  EXP --> ART

  UI <-->|Poll /job-status| API
  UI -->|Download CSV/XLSX| ART
  UI -->|Approve/Reject + Feedback| HITL[HITL Review Loop]
  HITL -->|Rerun w/ feedback| API
```
