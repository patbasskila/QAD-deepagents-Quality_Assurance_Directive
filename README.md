## QAD(Quality Assurance Directive) DeepAgents

Agentic Contract Intelligence with Retrieval-Augmented Generation
Upload a contract.
Generate structured quality definition packages.
Review, refine, and re-run with human feedback.

### Executive Summary

QAD DeepAgents is an end-to-end AI system that transforms contract documents (PDF or DOCX) into structured, schema-aligned quality definition outputs using a modular multi-agent architecture.
The project demonstrates:

- Agentic workflow orchestration
- Retrieval-Augmented Generation (RAG)
- Structured LLM output pipelines
- Embedding provider abstraction (remote + local)
- Human-in-the-loop feedback loops
- Artifact-based traceability
- Production-style backend architecture


### User Interface

Below is the contract submission interface used to initiate AI workflows:

The UI supports:

- PDF or DOCX upload
- Optional metadata inputs
- Real-time job polling
- CSV/XLSX export downloads
- Human-in-the-loop approval and re-run cycle
<img width="1171" height="751" alt="image" src="https://github.com/user-attachments/assets/e8942dcc-c2bc-434f-9a23-f80432dbd8fb" />


# System Architecture


## High-Level Overview

```mermaid
flowchart LR
  U[User] -->|Upload PDF or DOCX| UI[Web UI]
  UI -->|POST jobs| API[FastAPI Backend]

  API --> JOBS[JobStore in memory]
  API --> ART[Artifacts folder tmp jobs JOB_ID artifacts]

  API --> ING[Ingest service PDF or DOCX to blocks]
  ING -->|contract_text and document_blocks| ART

  ING --> CH[Chunking]
  CH --> EMB[Embeddings provider]
  EMB -->|vectors| FAISS[FAISS index]
  CH -->|chunks and chunk_meta| ART
  FAISS -->|faiss.index| ART

  API --> DA[DeepAgents orchestrator]
  DA --> PLN[Planner]
  DA --> SUB[Sub agents area specialists]
  SUB --> RET[RAG retrieval FAISS top k]
  RET --> LLM[LLM client Mock or Azure OpenAI]
  LLM --> SUB
  SUB --> NORM[Normalize and validate]
  NORM --> SCORE[Quality scoring]
  SCORE --> EXP[Export CSV or XLSX gold schema]
  EXP --> ART

  UI <-->|Poll job status| API
  UI -->|Download CSV or XLSX| ART
  UI -->|Approve or reject with feedback| HITL[HITL review loop]
  HITL -->|Rerun with feedback| API
```
### Architectural Highlights

- Modular service boundaries
- Isolated job-level artifact storage
- Deterministic orchestration flow
- Feedback-aware regeneration
- Pluggable embedding providers
- LLM abstraction (mock or Azure OpenAI)

## End-to-End Execution Flow

```mermaid
sequenceDiagram
  autonumber
  participant U as User
  participant UI as Web UI
  participant API as FastAPI Backend
  participant ING as Ingest Service
  participant RAG as Chunk Embed FAISS
  participant DA as DeepAgents
  participant LLM as LLM Mock or Azure
  participant EXP as Export Service
  participant ART as Artifacts Storage

  U->>UI: Upload contract PDF or DOCX
  UI->>API: Submit job with file and run config
  API->>ART: Create job folder and metadata
  API->>ING: Extract blocks and text
  ING->>ART: Save contract text and ingest report

  API->>RAG: Chunk text embed and build FAISS
  RAG->>ART: Save chunks metadata and index

  API->>DA: Start orchestration
  DA->>DA: Planner builds area plan
  DA->>ART: Save planner output and plan

  loop For each area
    DA->>RAG: Retrieve top k evidence
    DA->>LLM: Generate draft checks
    LLM-->>DA: Return structured JSON
  end

  DA->>ART: Save merged drafts and normalized checks
  DA->>ART: Save quality summary

  DA->>EXP: Generate CSV or XLSX export
  EXP->>ART: Save final output files

  UI->>API: Poll job status until complete
  API-->>UI: Return status and download links
  UI-->>U: Display results and allow download
```


## Human In The Loop Review Cycle
The system is not purely autonomous. It incorporates a structured feedback loop.
### Review & Regeneration Cycle
```mermaid
flowchart TD
  DONE[Job completed CSV or XLSX ready] --> REVIEW[User reviews output in UI]

  REVIEW -->|Approve| APPROVED[Mark job approved]
  REVIEW -->|Reject with feedback| REJ[Store rejection and feedback]

  REJ --> RERUN[Create new job referencing previous job]
  RERUN --> FB[Inject feedback into next run]
  FB --> RUN[Re run full pipeline Ingest RAG DeepAgents Export]

  RUN --> DONE2[New CSV or XLSX generated]
  DONE2 --> REVIEW
```

### Why This Matters

- Enables controlled AI deployment
- Supports human validation in regulated workflows
- Creates an iterative refinement loop
- Enforce applied AI governance design


## Embeddings Provider Architecture
The system supports both remote and local embedding strategies.

```mermaid
flowchart LR
  CH[Chunks] --> E[Embeddings Client]

  subgraph Provider Options
    E -->|HuggingFace mode| HF[Hugging Face Inference API]
    E -->|Local mode| LOC[Local transformer model]
  end

  HF --> V[Vector embeddings]
  LOC --> V

  V --> F[FAISS index]
  F --> RET[Retrieve top k evidence]
  RET --> DA[DeepAgents drafting]
```
### Embedding Modes

Remote (Default)
- Lightweight
- No large model downloads
- Ideal for public deployments

Local (Optional)
- Fully offline capable
- Requires additional dependencies
- Suitable for restricted environments


## Job Artifacts Layout
Each run generates a full artifact trail for observability and debugging.
```mermaid
flowchart TB
  ROOT[Job artifacts folder] --> A1[Contract text file]
  ROOT --> A2[Document blocks file]
  ROOT --> A3[Chunks file]
  ROOT --> A4[Chunk metadata file]
  ROOT --> A5[FAISS index file]
  ROOT --> A6[Ingest report file]

  ROOT --> AG[Agents folder]
  AG --> P1[Planner raw output]
  AG --> P2[Planner plan output]
  AG --> D1[Merged drafts output]
  AG --> N1[Normalized checks output]
  AG --> Q1[Quality summary output]
  AG --> E1[Export summary output]

  ROOT --> OUT1[QAD checks output]
  ROOT --> OUT2[QAD definition CSV]
  ROOT --> OUT3[QAD definition XLSX]
  ROOT --> REV[Review record optional]
  ROOT --> RC[Rerun context optional]
```
### This artifact-first design enables:
- Deterministic debugging
- Run reproducibility
- Prompt iteration workflows
- Structured evaluation pipelines

## Running the Project
### Create Virtual Environment
python -m venv .venv

Activate:

### Windows
.\.venv\Scripts\Activate.ps1
### Mac/Linux
source .venv/bin/activate

## Install Dependencies
pip install -r requirements.txt

Optional local embeddings:

pip install -r requirements-local.txt

## Configure Environment
Copy example:

cp .env.example .env

Edit:

EMBEDDINGS_PROVIDER=huggingface

HF_TOKEN=optional

## Start Server
uvicorn app.main:app --reload

Open:
http://127.0.0.1:8000


## Tech Stack
- FastAPI
- FAISS
- Hugging Face Inference API
- sentence-transformers (optional local)
- Uvicorn
- HTML/CSS frontend


