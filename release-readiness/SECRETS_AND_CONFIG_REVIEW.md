# Secrets and Config Review

## Current Security Defaults

- SECRET_KEY must not be default.
- SECRET_KEY must meet minimum length.
- AUTH_ENFORCE=false must not be allowed on non-loopback.
- API_HOST defaults to loopback.

## Required Production Inputs

- production SECRET_KEY source
- auth enforcement mode
- allowed host/bind strategy
- TLS/reverse proxy strategy
- CI/CD secret storage

## Not Yet Approved

Production secrets/config are not approved by this handoff package.
