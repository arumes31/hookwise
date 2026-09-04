# ConnectWise Configuration Auto-Link Design

Date: 2026-09-04  
Status: Approved for implementation planning

## Summary

HookWise will optionally associate a ConnectWise PSA configuration (asset/device) with a ticket created from a webhook. The option is configured per endpoint and is disabled by default for both new and existing endpoints.

The feature uses deterministic, company-scoped matching. It never makes an automatic association from a fuzzy or ambiguous match. For example, if `192.168.100.229` uniquely identifies the active configuration `DEXTER` inside the company actually assigned to the ticket, HookWise attaches `DEXTER` to that ticket.

## Goals

- Add a per-endpoint `auto_link_configuration_enabled` Boolean, defaulting to false.
- Match configurations using exact device identifiers found in the structured webhook payload or explicit endpoint mappings.
- Scope every lookup to the company actually assigned to the ConnectWise ticket.
- Attach exactly one unique, active, non-conflicting match.
- Make retries idempotent and prevent association failures from creating duplicate tickets.
- Record a bounded, useful outcome for support and troubleshooting.

## Non-goals

- Creating or updating ConnectWise configurations.
- Fuzzy, substring, semantic, or LLM-based asset matching.
- Using the webhook transport/source IP as a device identifier.
- Linking on timeout-generated tickets, UP/closure events, or dry runs in the initial release.
- Automatically attaching multiple possible configurations.

## User Interface and Persistence

Add a switch to the endpoint form:

> Automatically link matching configuration

Helper text should explain that HookWise searches only the assigned company's active configurations, skips ambiguous matches, and that attaching a configuration may affect ConnectWise SLA or agreement selection.

The Boolean must be represented in the database model, migration, endpoint create/edit/clone flows, `to_dict()`, backup/export allowlist, restore/import behavior, and tests. The migration must use a database server default of false so upgrades cannot silently enable the feature.

Existing endpoint field-mapping JSON will accept optional configuration hints:

```json
{
  "configuration_id": "$.device.connectwise_id",
  "configuration_device_id": "$.device.id",
  "configuration_serial": "$.device.serial",
  "configuration_mac": "$.device.mac",
  "configuration_tag": "$.device.asset_tag",
  "configuration_ip": "$.device.ip",
  "configuration_name": "$.device.hostname"
}
```

Mappings are optional. A bounded common-key extractor also recognizes explicitly labelled fields such as `ip`, `ip_address`, `mac`, `mac_address`, `serial`, `serial_number`, `asset_tag`, `device_id`, and `hostname`. It must not treat arbitrary strings or integers as device identifiers.

## Processing Flow

1. Validate and route the webhook normally.
2. Resolve the effective ConnectWise company before ticket deduplication.
3. Scope the Redis deduplication key and ConnectWise open-ticket query by that company.
4. Create the ticket or recover its existing ID through the current ticket-operation guard.
5. If auto-linking is enabled and the event is eligible, extract and normalize bounded match candidates.
6. Query active configurations belonging to the ticket's returned numeric `company.id`.
7. Re-check the returned configuration's company and fields locally.
8. Select a match only under the rules below.
9. Check whether the ticket/configuration association already exists.
10. POST the association only when absent.
11. Record the outcome without rerunning ticket creation.

ConnectWise exposes configuration association as a post-create nested resource:

```http
GET /company/configurations
POST /service/tickets/{ticketId}/configurations

{"id": 137}
```

Configurations are not part of the ticket-create schema, so the association cannot be included in the initial ticket POST.

## Matching Policy

Candidate values are normalized before comparison:

- IPv4 and IPv6: parse to canonical address form; accept private addresses but reject unspecified, loopback, multicast, and invalid values.
- MAC addresses: remove separators, normalize case, and reject zero/broadcast addresses.
- Serial, tag, device ID, and name: trim and compare exact case-insensitively.
- Configuration ID: accept only a value supplied by an explicitly configured mapping, then verify its company.

Identity strength, from highest to lowest:

1. Explicit ConnectWise configuration ID.
2. Device identifier, serial number, MAC address, or asset tag.
3. IP address.
4. Exact hostname/configuration name.

Model, manufacturer, operating system, default gateway, and last-login user may corroborate a candidate but can never identify one on their own.

Selection rules:

- A single exact IP match is sufficient only when it is the sole active match in the assigned company (and site when reliably known).
- A name match must be exact and unique.
- Multiple identifiers agreeing on one configuration increase confidence.
- Strong identifiers pointing to different configurations are a conflict; attach nothing.
- Zero matches or multiple surviving matches are safe skips.
- No fuzzy fallback is permitted.

## Existing-Ticket Deduplication

The current webhook flow deduplicates by endpoint and summary before it resolves the company, and its ConnectWise ticket search does not include company. Identical summaries routed to different customers can therefore reuse the wrong ticket.

Company resolution must move before deduplication. The effective company identifier must be included in both the Redis key and the ConnectWise open-ticket conditions. Only after this correction may configuration linking run for a reused/deduplicated ticket. Association remains idempotent, so enriching either a new or correctly reused ticket is safe.

## Failure and Retry Semantics

- `disabled`, `no_identifiers`, `no_company`, `no_match`, `ambiguous`, and `conflict` are successful no-op outcomes.
- Authorization and permanent validation failures are logged as `lookup_error` or `attach_error`; the already-created ticket remains successful.
- Transient lookup/association failures are recorded for an operator-visible replay; they do not fail or retry the successful ticket-creation operation.
- Before retrying an association after a timeout, HookWise checks the existing ticket configurations. An already-present association is success.
- The association POST must never cause the ticket-create operation to run again.
- Raw identifiers and complete payloads are not added to metrics or routine logs. Logs may include request, endpoint, ticket, company, configuration IDs, match kind, and bounded error details.

## API Cost and Caching

Do not call ConnectWise when the option is disabled or no valid candidate exists. The normal successful path is one bounded configuration GET plus one association POST. Request only the fields needed for matching and cap results so ambiguity is detected without downloading an entire inventory.

A short two-to-five-minute Redis cache may store company-and-candidate lookup results under the existing ConnectWise cache namespace. Negative results receive a short TTL. Returned values must still be validated before association.

## Security and Permissions

All ConnectWise condition values must be parsed or escaped for the conditions language; URL encoding alone is insufficient. Payload traversal, candidate count, string length, result count, and pagination are bounded.

The API member is expected to require Configuration Inquire access and Service Ticket Edit access. ConnectWise does not publicly specify exact least privilege for every association operation, so these permissions and the request body must be smoke-tested against the target tenant and API version.

## Acceptance Criteria

- The option is false after fresh install, upgrade, clone, import without the field, and restore from an older backup.
- Disabled endpoints make no configuration API calls.
- A ticket containing `192.168.100.229` for DEXTER's company attaches DEXTER when it is the sole active exact match.
- The same address at a different company is never considered.
- Duplicate IPs, conflicting identifiers, inactive configurations, and generic names produce no association and an observable reason.
- Explicit field mappings override discovery without bypassing company verification.
- A retried webhook does not create another ticket or duplicate the association.
- New and reused tickets are both supported only after company-scoped deduplication is in place.
- Association failure cannot turn a successfully created ticket into a failed ticket-creation operation.

## Verification Plan

- Unit tests for extraction, normalization, ranking, conflict handling, and bounded input.
- Client tests for query escaping, company scoping, pagination limits, association GET/POST, and error classification.
- Endpoint/model/migration/backup tests for the default-disabled setting.
- Task tests for disabled, unique, absent, ambiguous, conflicting, already-attached, transient-failure, retry, and reused-ticket paths.
- A non-production tenant smoke test: create a ticket for DEXTER's company, find configuration ID 137 by exact company and IP, attach it, read the association back, and confirm it in the PSA UI.

## References

- [ConnectWise PSA REST explorer (login required)](https://developer.connectwise.com/Products/ConnectWise_PSA/REST)
- [ConnectWise service-ticket Configurations documentation](https://docs.connectwise.com/ConnectWise_Documentation/060/010/010/001?psa=1)
- [ConnectWise Meraki webhook configuration-association example](https://docs.connectwise.com/ConnectWise_Unite/500/040?psa=1)
- [Community mirror of ConnectWise OpenAPI 2025.16 association endpoints](https://github.com/covenanttechnologysolutions/connectwise-rest/blob/master/generator/manage-json/manage.json#L185100-L185431)
- [Older generated ConnectWise ticket API reference](https://vc3.github.io/connectwise-rest-api/classes/_api_api_.ticketsapi.html#serviceTicketsIdConfigurationsPost)
