HookWise Tasks

## Status: V2 Implementation Complete
All 100 UX Improvement Recommendations have been implemented.
- Dashboard & Overview (Search shortcut, Sparklines, Filters, Clone, Stats, Clear Logs, Status Indicator, Back to Top, Pin, View Persistence, Dark/Light Mode, Bulk Export, Reordering, Onboarding, Context Menus)
- Configuration & Forms (Test Connection, Auto-fill, Templates, JSON Schema/Validator, Live Preview, Selectors, Syntax Highlighting, Tooltips, Validation, Maintenance Picker, Auto-save, Undo/Redo, Breadcrumbs, Save & Another, Drafts, Advanced Toggle)
- History & Logs (Date Range, Ticket Search, CSV Export, Bulk Replay, Diff View, Pretty Print, Retry Count, Source IP, Infinite Scroll, Compare, Rule Matched, Live Tail, Headers)
- Security & Access (Multi-user, RBAC, Audit Logs, 2FA with TOTP/QR, IP Whitelist, Session Mgmt, Rate Limiting, Master API Keys, Health Alerts, Backup/Restore, HTTPS, CSP)
- Visual & Interactive (Micro-interactions, Skeleton Loading, Keyboard Nav, Confetti, Custom Scrollbars, Favicon Health, Glassmorphism, Background Patterns, Responsive, Transitions, Custom Error Pages, Feedback, Pull-to-Refresh, Top Loading Bar)

---

## Future Roadmap & Recommendations

### Category: Intelligent Automation
- [x] **Local AI Infrastructure:** Added Ollama service to Docker Compose for CPU-based LLM support.
- [x] **Automated RCA Notes:** Suggest potential root causes and troubleshooting steps in ticket internal notes.
- [ ] **Semantic Deduplication:** Use LLM reasoning to group related alerts even if titles differ.
- [ ] **Smart Rule Builder:** Natural language interface to generate JSONPath/Regex routing rules.

### Category: Advanced Integration
- [ ] **Two-Way PSA Sync:** Sync ticket status from ConnectWise back to HookWise via CW Webhooks.
- [ ] **Asset Mapping:** Link alerts to specific CW Configurations (assets) using payload identifiers.
- [ ] **Multi-Destination Routing:** Route a single alert to CW, Slack, and PagerDuty simultaneously.

### Category: Enterprise Management
- [ ] **Granular RBAC:** Specific permissions for Viewers, Editors, and Security Admins.
- [ ] **Multi-Tenancy (Teams):** Partition endpoints and logs by department or organization.
- [ ] **External Vault Store:** Support for HashiCorp Vault/AWS Secrets Manager for API credentials.

### Category: Analytics & Reporting
- [ ] **Executive Summary Reports:** Automated weekly email reports with alert trends and SLA metrics.
- [ ] **Latency Monitoring:** Visualize and alert on PSA API processing time spikes.

### Category: UX & Productivity
- [ ] **Command Palette (Ctrl+K):** Quick navigation and action bar for power users.
- [x] **Step-through Debugger:** Interactive sandbox for testing JSONPath/Regex against live payloads.
- [ ] **Mobile PWA Enhancements:** Add native push notifications for critical alert failures.
