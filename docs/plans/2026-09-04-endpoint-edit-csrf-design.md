# Endpoint edit CSRF regression design

## Problem

Endpoint edit autosave stores the entire form, including the hidden CSRF token and an entered HMAC secret. Edit pages restore that data automatically. After the authenticated session changes, the restored form token belongs to the old session and overrides the fresh token rendered by Flask. Flask-WTF therefore rejects the update with HTTP 400. Because the form inherits HTMX boosting, the returned error page is not displayed.

## Design

Keep endpoint create/edit as a conventional full-page form by opting it out of inherited HTMX boosting. Its server contract already returns full HTML on validation failure and a redirect on success, so native submission matches that contract and makes errors visible.

Mark ephemeral or sensitive fields as excluded from local autosave. The autosave serializer and restorer will skip those fields and sanitize legacy saved records as they are loaded. Capture named checkbox values explicitly so both checked and unchecked states survive restoration; `FormData` alone omits unchecked controls.

## Verification

- A regression test verifies native form submission and exclusion markers on CSRF and HMAC fields.
- A security regression test verifies that autosave honors exclusion markers and records checkbox state.
- A local-container browser scenario crosses a logout/login boundary, restores an endpoint draft, and successfully updates with the freshly rendered CSRF token.
- Focused and full Python quality gates remain green.
