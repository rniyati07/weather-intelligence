# Documentation

Start with the [root README](../README.md) for setup, commands and an overview.
The documents below go deeper.

| Document | What it covers | Status |
|---|---|---|
| [architecture/system-specification.md](architecture/system-specification.md) | Product behaviour, data flow, contracts and constraints of the **current** system, including the chat flow | Current |
| [architecture/technical-design.md](architecture/technical-design.md) | Components, data design and testing strategy of the deterministic engine | Core still valid; predates chat |
| [architecture/system-architecture-original.md](architecture/system-architecture-original.md) | Original architecture decisions (ADRs) and layering rationale | Historical; the layering still holds, the scope has grown |
| [api/api-and-data-contract.md](api/api-and-data-contract.md) | Weather-intelligence endpoint, schema, enum and error contract | Predates chat; the live `/openapi.json` is authoritative |
| [product/product-requirements.md](product/product-requirements.md) | Original product scope and requirements | Historical |
| [development/implementation-guide.md](development/implementation-guide.md) | Original phase-by-phase build handbook | Historical |
| [archive/e2e-audit-2026-09.md](archive/e2e-audit-2026-09.md) | Forensic end-to-end audit of the system as of 2026-09-15 | Archived; kept as a record, not updated |

Where a document and the code disagree, the code wins.
