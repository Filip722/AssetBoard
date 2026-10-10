# Security policy

## Supported versions

During pre-alpha, security fixes are made against the current default branch. There are no stable
release lines yet.

## Reporting a vulnerability

Please report suspected vulnerabilities privately through GitHub's **Report a vulnerability**
control in the repository's Security tab (GitHub Security Advisories). Do not open a public issue
for an undisclosed vulnerability. Include the affected commit or version, impact, and steps to
reproduce when safe to do so. The maintainers will coordinate a response and disclosure there.

AssetBoard has no authentication until M1. Until then, only expose the app to trusted users and
networks; see [deployment guidance](docs/deployment.md). Missing authentication before M1 is a
known limitation, not a vulnerability report.
