# Security Policy

## Supported Versions

| Version | Supported          |
|---------|--------------------|
| 1.0.x   | :white_check_mark: |

## Reporting a Vulnerability

Cat is a remote file system service that handles potentially sensitive
source code. We take security seriously.

If you discover a security vulnerability, please do **not** open a public
issue. Instead, report it privately by contacting the maintainers through
GitHub's security advisory feature or by email.

### What to include

- A description of the vulnerability
- Steps to reproduce (if applicable)
- Potential impact
- Any suggested mitigation (optional)

### Response timeline

You can expect an initial acknowledgment within 48 hours, followed by
regular updates as we investigate and develop a fix. We will coordinate
the disclosure timeline with you.

## Security Considerations

- Cat should only be deployed with TLS in production.
- Authentication must be configured for public-facing deployments.
- Project root directories must be outside the server's own configuration
  and state directories.
- Symlink following and recursive deletion are disabled by default.
- Always review the config.example.toml and adjust security settings
  before deployment.
