# Antigravity Global Rules & Protocols

This file defines the core behavioral rules and default protocols for the Antigravity agent. These rules must be checked and adhered to at the start of every session and during task execution.

## 1. Plan Mode is the Default
- **3+ Step Tasks:** Any task that requires 3 or more steps must start in plan mode.
- **Context First:** Before creating any plan or spec, always query the context first using **`memory_recall`** or **`memory_smart_search`** to retrieve relevant history, and verify codebase structure using the manifest.
- **Protocol:** Every time you enter the planning phase, always use the **`gsd-plan-phase`** (or corresponding **`gsd-spec-phase`** / **`gsd-progress`** planning) skill to structure, specify, and review the plan before execution.
- **Spec Compliance:** Map all technical specifications, data structures, and API contracts using **`openspec`** before changing any code.
- **Dynamic Re-planning:** If execution goes sideways, stops, or encounters unexpected errors, halt immediately and re-plan.
- **Upfront Specs:** Write detailed, technical specifications before changing any source code.

## 2. Subagents Protect the Main Context
- **Delegation:** Offload extensive research, parallel analysis, and code exploration tasks to subagents.
- **Protocol:** For complex research, query audits, or open-ended codebase exploration, use the **`deep-research`** skill pack (or **`search-specialist`** / **`tavily-web`** / **`search_web`** tools) to gather information thoroughly before designing a solution.
- **Focus:** Equip each subagent with exactly one focused task to conserve the main context.
- **Compute Scalability:** Allocate more compute resources/subagents to solve complex, concurrent sub-problems.

## 3. The Self-Improvement Loop
- **Persistent Lessons:** Every correction, mistake, or session refinement must be documented as a rule in `tasks/lessons.md`.
- **Session Setup:** Review `tasks/lessons.md` at the start of every session to inherit all historical learnings.
- **Efficiency:** Compounded micro-rules keep context clean; 100 lines of precise lessons beat 800 lines of repetitive context.

## 4. Verification Before Done
- **No Exceptions:** Never mark a task or phase as complete without executing verification tests and proving it works.
- **Verification Impact:** Double-check and verify code changes 2-3x to multiply the quality of the final output.

## 5. Autonomous Bug Fixing
- **Hands-Free Resolution:** Given a bug or test failure, solve it autonomously without asking for user hand-holding.
- **Evidence-Based:** Point directly to logs, stack traces, compiler errors, or failing tests, formulate a hypothesis, and resolve.

## 6. Codebase Mapping & Token Budgeting (Save Tokens)
- **Manifest First:** Never read entire directories or files recursively. Instead, run **`context-optimizer`** to generate a `CONTEXT_MANIFEST.md` for the project. Always reason from the manifest first.
- **Scoping & Limits:** Restrict file reads to a maximum of 3 files per turn (strict blast radius control).
- **Search Over Scan:** Use **`memory_recall`** / **`memory_smart_search`** or semantic tools to locate specific code blocks rather than bulk-grepping or listing deep directories.
- **HAM Scoping:** Follow directory-specific context files using **`hierarchical-agent-memory`** to avoid loading parent context when working in subdirectories.
- **Memory Logging on Edits:** Every time you modify files or apply updates, proactively use the **`memory_save`** skill to record the exact changes, the files touched, and the rationale so that future agents can retrieve them instantly without file scanning.

---

## Core Principles
1. **Simplicity First:** Make every code modification as small and targeted as possible.
2. **No Laziness:** Identify and fix the true root cause; never use temporary patches or band-aids.
3. **Minimal Impact:** Only touch files and logic that are strictly necessary to achieve the objective.

---

## Domain-Specific Skill Mappings
You are globally equipped with advanced skill packs. You MUST ALWAYS FOLLOW THESE RULES WITHOUT ANY EXCEPTION:
- **Auto-Detection:** Proactively analyze the user's request and match it to the most relevant skill in your library of 300+ local skills (e.g. backend, database, styling, security, planning, memory).
- **Proactive Invocation:** Before executing code changes, read the corresponding `SKILL.md` file of the matched skill and apply its exact design patterns and instructions. Do not wait for the user to specify or request the skill; choose and run it autonomously.

### 1. Planning & Spec Compliance (`gsd` + `openspec` CLI + `grill-me`)
- **Plan Phase:** Always initiate the planning phase by running the **`gsd-plan-phase`** checklist and creating a standard `PLAN.md` specification.
- **Specification Design:** Use the **`openspec` CLI** (globally installed via npm) to manage specs, changes, validation, context stores, and workflows. Run `openspec init` in projects, `openspec spec` to manage specs, `openspec change` for change proposals, `openspec validate` to verify, and `openspec context-store` for local context.
- **Spec Phase:** Use **`gsd-spec-phase`** to clarify WHAT a phase delivers with ambiguity scoring before discuss-phase.
- **Traceability:** Check milestone readiness using **`gsd-progress`**.
- **Interactive Interview / Stress-testing:** Invoke the **`grill-me`** skill when starting a phase or plan where design assumptions need to be stress-tested, or if the user explicitly asks to be grilled on design details.

### 2. Frontend Development & Design (`taste-skill` + `ui-ux-pro-max`)
When designing, modifying, or creating UI components, styles, layouts, or stylesheets:
- **Design Intelligence:** Invoke **`ui-ux-pro-max`** for 50+ styles, 161 color palettes, 57 font pairings, 161 product types, 99 UX guidelines, and 25 chart types across 10 tech stacks. Use for ALL design decisions --- page layouts, component creation, color schemes, typography, spacing, animations, and responsive behavior.
- **Visual Design:** Invoke **`design-taste-frontend`** and **`high-end-visual-design`** to enforce curated color palettes, elegant gradients, and layout structure (Bento grids, fluid containers).
- **Design Tokens & Systems:** Use **`stitch-design-taste`** to verify token styling constraints and consistency.
- **Styling Standards:** Leverage **`industrial-brutalist-ui`** or **`minimalist-ui`** for modern styling guidelines, and **`full-output-enforcement`** to inspect breakpoints and alignment.

### 3. Backend Development
- **Language/Framework Stacks:** Use targeted backend skills (**`dotnet-backend`**, **`django-pro`**, **`fastapi-pro`**, **`nestjs-expert`**, **`hono`**, **`golang-pro`**, **`rust-pro`**, **`typescript-pro`**).
- **Testing workflows:** Use **`laravel-tdd`** to guide unit and integration testing workflows.

### 4. Database Operations & Optimization
- **Optimization:** Use **`database-admin`**, **`neon-postgres`**, **`postgres-best-practices`**, and **`postgresql-optimization`** to design database models, query filters, indices, and transaction boundaries.

### 5. Context, Memory & Knowledge Management (`agentmemory` + context skills)
- **Persistent Memory (agentmemory):** Use **`memory_save`** to save insights/decisions to long-term storage. Use **`memory_recall`** to search past observations/sessions via hybrid BM25+vector+graph search. Use **`agentmemory-handoff`** to resume previous sessions. Use **`agentmemory-recap`** for session rollups. Use **`agentmemory-session-history`** for past session overview. Use **`agentmemory-forget`** to delete specific memories. Use **`agentmemory-commit-context`** to trace code back to the agent session that produced it. Use **`agentmemory-commit-history`** to list agent-linked commits.
- **Context Optimization:** Use **`context-optimizer`** to generate `CONTEXT_MANIFEST.md` files for 5x-27x token reduction. Use **`context-optimization`** for token budgeting, caching, and partitioning strategies. Use **`context-manager`** for vector DB and knowledge graph context engineering.
- **Session Continuity:** Use **`context-management-context-save`** to persist context between sessions. Use **`context-management-context-restore`** to restore context in new sessions. Use **`hierarchical-agent-memory`** for scoped directory-level context files that reduce token spend.
- **Memory Architecture:** Use **`agent-memory-systems`** for designing short-term/long-term/vector store memory architectures. Use **`conversation-memory`** for persistent conversation memory patterns.
- **RAG & Knowledge Graphs:** Use **`lightrag`** for entity-relation knowledge graph building and multi-mode retrieval (local, global, hybrid, mix, naive) when working with document-based RAG systems.

### 6. Execution, Verification & Diagnostics
- **Execution Phase:** Deploy implementation changes under the **`gsd-execute-phase`** pipeline.
- **UAT & Verification:** Perform manual checks via **`gsd-verify-work`** and audit validation issues using **`gsd-validate-phase`** and **`gsd-ship`**.
- **Diagnostics:** Resolve bugs using **`systematic-debugging`**, **`debugger`**, and **`ecc-guide`**. Use **`configure-ecc`** for ECC installation and **`ecc-tools-cost-audit`** for cost/billing audits.
