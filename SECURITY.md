# Security Policy

## Supported versions

V5 release candidates receive security fixes. Earlier versions follow their own published support policy.

## Reporting

Do not disclose a suspected vulnerability in a public issue. Contact the repository owner through a private,
verified channel and include the affected version, minimal reproduction, impact and suggested mitigation.
Never include production credentials, private payloads or personal data.

## Deployment boundary

The built-in HTTP server binds locally by default. Production deployments must add trusted TLS termination,
network source restrictions, secret rotation and an independently verified identity provider. Configuration
files contain environment-variable names only; secret values must remain outside source control.
