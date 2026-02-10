# HookWise Tasks & Roadmap

## Core Functionality & Routing
1. Support for mapping incoming JSON fields to arbitrary ConnectWise ticket fields via JSONPath.
2. Implement regex-based routing rules for incoming payloads.
4. Create a "Test Endpoint" button to send a dummy payload from the UI.
7. Allow "maintenance windows" where webhooks are received but not forwarded (or queued).
9. Add conditional logic (If severity == 'high' -> set Priority 1, else Priority 3).
12. Add a retry mechanism with exponential backoff for failed ConnectWise API calls.
13. Store a history of received webhooks (Payload Log) for debugging.
14. Allow re-playing failed webhooks from the Payload Log.

## ConnectWise Integration
23. Support for updating existing tickets instead of creating new ones (deduplication).
24. Fetch and cache ConnectWise Service Boards/Priorities dynamically in the UI.
34. Fetch Company List from ConnectWise to provide a dropdown in UI (instead of just ID parsing).
37. Implement logic to check if a duplicate open ticket already exists before creating a new one.
40. Dashboard widget showing "Tickets Created Today/Closed" vs "Failed Attempts".

## Security & Authentication
41. Implement IP Whitelisting per endpoint.
43. Rotate Bearer tokens automatically or on-demand.
46. Audit log for all changes made to Endpoint configurations.
47. Encrypt sensitive fields in the database (API Keys, tokens) using a stronger key management strategy.
49. Implement basic auth for the Web GUI.
50. Add API rate limiting for the Web GUI to prevent brute force.

## User Interface (Web GUI)
52. Real-time live feed of incoming webhooks using Socket.IO (Visualizer).
55. Implement pagination for the Payload Log / History view.
56. Add a "Copy to Clipboard" button for all ID/Token fields.
57. Dashboard overview with charts (Requests per hour, Success/Fail ratio).
58. Syntax highlighting for JSON payload viewers.
60. Add tooltips for all ConnectWise specific fields explaining what they do.
62. Status indicators (Green/Red) for external service health (Redis, CW API).
63. Add custom favicon and branding options.
65. "Toast" notifications for success/error actions in the UI.

## Observability, Logging & Monitoring
66. Expose detailed Prometheus metrics for queue depth and worker latency.
70. Add a /readyz endpoint checking DB and Redis connections.
71. Alerting mechanism if the queue size exceeds a threshold.
72. Visualize Celery task progress in the UI.
73. Log retention policies (auto-cleanup old logs/history after X days).
74. Correlation IDs traced from Webhook -> Worker -> ConnectWise API.
75. "Debug Mode" toggle in UI to increase log verbosity temporarily.

## Performance & Reliability
76. replace sqlite with progre container
77. Implement a "Dead Letter Queue" for permanently failed tasks.
78. optimize Docker image size (currently using standard python, use Alpine/Slim?).
79. Use connection pooling for Redis and SQLite/Postgres.
80. Batch ticket creation requests if high volume (if CW API supports batching).
81. Graceful shutdown handling for workers to prevent data loss.
82. Add database migration system (Alembic) for future schema changes.
83. Cache ConnectWise API token response (if using temporary tokens).
84. Optimize static asset loading (caching headers, minification).

## Deployment & Infrastructure
94. Add a `.env.example` generator script.
