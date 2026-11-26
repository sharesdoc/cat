# Third Party Notices

This project uses third-party components. Their licenses and notices
are listed below.

## Direct Dependencies

### MCP Python SDK

- Source: https://github.com/modelcontextprotocol/python-sdk
- Version: 2118f14f (v2.3.0 release candidate)
- License: MIT
- SPDX: MIT
- Copyright: (c) 2024 Anthropic, PBC
- Used for: MCP protocol implementation (direct dependency)
- Scope: Direct runtime dependency as declared in pyproject.toml

### Pydantic

- Source: https://github.com/pydantic/pydantic
- License: MIT
- SPDX: MIT
- Copyright: (c) 2017 to present Pydantic Services Inc. and individual contributors
- Used for: Data validation and schema definition (direct dependency)
- Scope: Direct runtime dependency as declared in pyproject.toml

## Development Dependencies

### pytest

- Source: https://github.com/pytest-dev/pytest
- License: MIT
- SPDX: MIT
- Used for: Testing framework

### pytest-asyncio

- Source: https://github.com/pytest-dev/pytest-asyncio
- License: Apache-2.0
- SPDX: Apache-2.0
- Used for: Async test support

### Ruff

- Source: https://github.com/astral-sh/ruff
- License: MIT
- SPDX: MIT
- Used for: Python linter and formatter

## Reference Projects

The following open-source projects were studied for design reference.
Cat does not incorporate their source code unless explicitly stated.
Each entry documents the fixed commit, license, and scope of reference.

### 1. MCP Official Python SDK

- Source: https://github.com/modelcontextprotocol/python-sdk
- Fixed commit: 2118f14f8a19bc158d8a1cf90af58d85d187f849
- License: MIT
- SPDX: MIT
- Copyright: (c) 2024 Anthropic, PBC
- Reference scope: **Direct dependency** — used as runtime dependency via pip. SDK interfaces (MCPServer, TokenVerifier, StreamableHTTP) are consumed as published library, not copied or modified.
- Notice: Full MIT license text reproduced above in Direct Dependencies section.

### 2. MCP Official Reference Filesystem Server

- Source: https://github.com/modelcontextprotocol/servers
- Fixed commit: c799c8552b0f27b7a33a7b9233773da144dddcf5
- License: MIT (code contributed prior to Apache-2.0 license transition)
- SPDX: MIT
- Copyright: (c) 2024-2025 Model Context Protocol a Series of LF Projects, LLC
- Reference scope: **Behavioral reference only** — studied tool patterns (validatePath, buildTree, pinned-directory, directory budget). No source code copied.
- Note: The upstream project is transitioning from MIT to Apache-2.0. Contributions at this commit that were originally MIT-licensed remain MIT.

### 3. j0hanz/filesystem-mcp

- Source: https://github.com/j0hanz/filesystem-mcp
- Fixed commit: df56b1932872f47bf56eb4329be3d781ab9050f4
- License: MIT
- SPDX: MIT
- Copyright: (c) 2026 j0hanz
- Reference scope: **Behavioral reference only** — studied rewriteFile, applyEdits, patchContent, UndoJournal, paginate, readByMode. Cat does not adopt fuzzy matching, multi-file batch operations, or non-persistent undo semantics.

### 4. tkgstrator/local-mcp

- Source: https://github.com/tkgstrator/local-mcp
- Fixed commit: 2a895a74981d82be327eb38e78dda1fc4eee3dc6
- License: MIT
- SPDX: MIT
- Copyright: (c) 2026 tkgstrator
- Reference scope: **Behavioral reference only** — studied OAuth device flow, SQLite session persistence (Store::open, PersistentSessions), segmented file read, file interactions. Cat does not port ad-hoc shared passwords or arbitrary shell execution.

### 5. Muvon/octofs

- Source: https://github.com/Muvon/octofs
- Fixed commit: d004e37902fb980d19a53261416dc9a822ac0e8a
- License: Apache-2.0
- SPDX: Apache-2.0
- Reference scope: **Behavioral reference only** — studied resolve_path, lock_key_for, acquire_file_lock, line anchors, HistoryStore patterns. Cat does not adopt arbitrary shell or remote path execution in V1.
- Notice: The following Apache-2.0 terms apply solely to any influence taken from this project. No source code has been copied.

### 6. njbrake/filesystem-mcp-server

- Source: https://github.com/njbrake/filesystem-mcp-server
- Fixed commit: a4b5b34f07a5386138825d7843bf4a2c578c8d5c
- License: Apache-2.0
- SPDX: Apache-2.0
- Reference scope: **Behavioral reference only** — studied Python project structure, Docker packaging, tool registration patterns, basic CRUD patterns. This project is not adopted as a complete security layer.
- Notice: The following Apache-2.0 terms apply solely to any influence taken from this project. No source code has been copied.

### 7. supercorp-ai/supergateway

- Source: https://github.com/supercorp-ai/supergateway
- Fixed commit: ffdf9b1d63a8e6086ed919da7039207b3f0ed3f5
- License: MIT
- SPDX: MIT
- Copyright: (c) 2024 Supercorp
- Reference scope: **Behavioral reference only** — studied Streamable HTTP gateway behavior and session patterns for integration testing. No source code copied.

### 8. oraios/serena

- Source: https://github.com/oraios/serena
- Fixed commit: 23c06ff715409b2e9dc9be7ca26b7b2fbf356b7c
- License: MIT (SolidLSP component) / GPL-3.0-or-later (Serena Application)
- SPDX: MIT / GPL-3.0-or-later
- Copyright: (c) 2025-2026 Jain & Panchenko IT-Berater Partnerschaft (Oraios AI) and contributors
- Reference scope: **Not adopted for Cat** — SolidLSP (MIT) was evaluated as potential infrastructure reference. The Serena Application (GPL-3.0-or-later) is not compatible with Cat's MIT license and its source code is not used in any form.
- Notice: The Serena application is licensed under GPL-3.0-or-later. Only the MIT-licensed SolidLSP component was evaluated for separately reusable patterns. No GPL-covered code is incorporated into Cat.

### 9. desktop-commander/remote-desktop-commander

- Source: https://github.com/desktop-commander/remote-desktop-commander
- Fixed commit: 71d6444c59c36f2cba2f13e8f72785383a8528b5
- License: **Proprietary / Not Open Source** — All Rights Reserved
- Copyright: (c) 2026 SIA Desktop Commander
- Reference scope: **Not adopted** — the service implementation is proprietary (not open source). Only the public README and interaction patterns were reviewed for UX reference. No source code used.
- Notice: This repository contains documentation, manifests, and brand assets only. The Remote Desktop Commander service itself is proprietary and not open source. The separate open-source local server at wonderwhy-er/DesktopCommanderMCP is MIT-licensed and is not referenced by this project.

### 10. BurntSushi/ripgrep

- Source: https://github.com/BurntSushi/ripgrep
- Fixed commit: 3e85c6c148fdce38a99ceeb9d2780dc7b03b4a4f
- License: MIT
- SPDX: MIT
- Copyright: (c) 2015 Andrew Gallant
- Reference scope: **Behavioral reference only** — studied CLI search patterns, JSON output format, ignore file handling (rg --ignore-file), and path limit testing. Cat invokes ripgrep as an external CLI tool when available, not as a library dependency.
- MIT License text:

```
The MIT License (MIT)

Copyright (c) 2015 Andrew Gallant

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
```

---

## Direct Dependency License Summary

Per pyproject.toml, the runtime direct dependencies of Cat and their licenses are:

| Dependency | License | SPDX |
|---|---|---|
| mcp (>=2.3.0rc1, <3.0.0) | MIT | MIT |
| pydantic (>=2.0) | MIT | MIT |

All direct dependencies use permissive (MIT) licenses compatible with Cat's MIT license.

---

## Additional Notes

This file will be updated as new dependencies are added. Each direct
dependency's license will be documented here. If you believe any
component is missing from this list, please open an issue.
