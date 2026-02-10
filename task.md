HookWise Tasks
 Researching Current Implementation

 Core Functionality & Routing

 1. Support for mapping incoming JSON fields to arbitrary ConnectWise ticket fields via JSONPath.

 2. Implement regex-based routing rules for incoming payloads.

 4. Create a "Test Endpoint" button to send a dummy payload from the UI.

 7. Allow "maintenance windows" where webhooks are received but not forwarded (or queued).

 9. Add conditional logic (If severity == 'high' -> set Priority 1, else Priority 3). [Handled via Regex Routing]

 12. Add a retry mechanism with exponential backoff for failed ConnectWise API calls.

 13. Store a history of received webhooks (Payload Log) for debugging.

 14. Allow re-playing failed webhooks from the Payload Log.

 ConnectWise Integration

 23. Support for updating existing tickets instead of creating new ones (deduplication).
24. Fetch and cache ConnectWise Service Boards/Priorities dynamically in the UI.
34. Fetch Company List from ConnectWise to provide a dropdown in UI.
 37. Implement logic to check if a duplicate open ticket already exists.
 40. Dashboard widget showing "Tickets Created Today/Closed" vs "Failed Attempts".
 Security & Authentication

 41. Implement IP Whitelisting per endpoint.
 43. Rotate Bearer tokens automatically or on-demand.
 46. Audit log for all changes made to Endpoint configurations.
 47. Encrypt sensitive fields in the database.
 49. Implement basic auth for the Web GUI.
 50. Add API rate limiting for the Web GUI.
 User Interface (Web GUI)

 52. Real-time live feed of incoming webhooks using Socket.IO.
 55. Implement pagination for the Payload Log.
 56. Add a "Copy to Clipboard" button.
 57. Dashboard overview with charts.
 58. Syntax highlighting for JSON payload viewers.
60. Add tooltips for ConnectWise fields.
 62. Status indicators for external service health.
 63. Add custom favicon and branding.
65. "Toast" notifications.
 Observability, Logging & Monitoring

 66. Expose detailed Prometheus metrics.
 70. Add a /readyz endpoint.
 71. Alerting mechanism for queue size.
 72. Visualize Celery task progress.
 73. Log retention policies.
 74. Correlation IDs traced.
 75. "Debug Mode" toggle in UI.
 Performance & Reliability

 76. Replace sqlite with postgres container.
 77. Implement a "Dead Letter Queue".
 78. Optimize Docker image size.
 79. Use connection pooling.
 80. Batch ticket creation requests.
 81. Graceful shutdown handling.
 82. Add database migration system (Alembic).
 83. Cache ConnectWise API token response.
 84. Optimize static asset loading.
 Deployment & Infrastructure

 94. Add a .env.example generator script.
 100. Update docker files and readme

 101. remove migration from sqllite