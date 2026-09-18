# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Hookwise primarily serves MSP and service-desk operators working at desktop workstations. Administrators also use the same application to manage identities, routing, integrations, and operational settings. Mobile remains supported, but desktop task efficiency is the primary design target.

## Product Purpose

Hookwise receives monitoring and security webhooks, routes them through tenant- and company-aware rules, and turns them into reliable ConnectWise Manage ticket activity. Success means operators can understand system health, trace delivery outcomes, and maintain routing configuration quickly and with confidence.

## Positioning

Hookwise combines durable webhook delivery, company-aware ticket deduplication, tenant mapping, routing controls, auditability, and optional local AI analysis in one self-hosted ConnectWise bridge.

## Operating Context

Users work in a dense operational console across endpoint overview, endpoint configuration, webhook history, audit logs, TenantMap, identity management, settings, maintenance, documentation, authentication, and system/error states. They routinely scan tables, investigate failed deliveries, edit configuration, and monitor service health.

## Capabilities and Constraints

- Flask and Jinja server-rendered application enhanced with HTMX and vanilla JavaScript.
- Desktop is the primary usage environment; responsive layouts must remain functional on smaller screens.
- Authentication supports local credentials, two-factor setup, role-based access, and optional Microsoft Entra ID.
- Existing product behavior, terminology, security boundaries, and data density must remain intact during the visual redesign.
- The redesign covers the complete application, including login, setup, documentation, empty/error states, and authenticated operational views.

## Brand Commitments

The Hookwise name, hook-based identity mark, and product terminology remain. The current black-and-green console is not binding: the visual language may change substantially. It remains the approved fallback if the replacement direction does not meet expectations.

## Evidence on Hand

The repository contains the complete application templates, shared shell, self-hosted fonts, icon sprite, logos, operational UI, tests, and product documentation. No external marketing claims, testimonials, or additional brand assets should be invented.

## Product Principles

- Make operational state understandable at a glance.
- Keep frequent desktop workflows fast, dense, and predictable.
- Reveal complexity progressively without hiding diagnostic detail.
- Communicate risk, failure, and recovery with unambiguous language and state design.
- Preserve keyboard access, responsive fallback behavior, and reduced-motion preferences.

## Accessibility & Inclusion

The complete application must retain visible keyboard focus, semantic structure, screen-reader names, sufficient contrast, reduced-motion behavior, zoom resilience, and usable responsive layouts.
